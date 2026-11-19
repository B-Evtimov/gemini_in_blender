bl_info = {
    "name": "Gemini AI Assistant Pro",
    "author": "Boris Evtimov",
    "version": (2, 3, 0),
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar (N) > Gemini AI",
    "description": "Turns natural language into Blender Python with Google Gemini - "
                   "non-blocking, with auto-correction, chat history and undo",
    "doc_url": "https://github.com/B-Evtimov/GeminiInBlender",
    "tracker_url": "https://github.com/B-Evtimov/GeminiInBlender/issues",
    "category": "Object",
}

import http.client
import json
import linecache
import math
import os
import queue
import re
import ssl
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import bpy
from bpy.app.handlers import persistent
from bpy.props import CollectionProperty, IntProperty, StringProperty

ADDON_VERSION = ".".join(str(v) for v in bl_info["version"])
USER_AGENT = f"GeminiInBlender/{ADDON_VERSION} (Blender {bpy.app.version_string})"

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
ENV_KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
CODE_TEXT_NAME = "gemini_last_code.py"

MAX_HISTORY_TURNS = 6      # successful turns sent back to Gemini as conversation context
MAX_STORED_HISTORY = 50    # history entries kept in the .blend
MAX_SHOWN_HISTORY = 8      # history entries drawn in the panel
MAX_SCENE_OBJECTS = 60     # objects described in the scene context
MAX_ERROR_CHARS = 3000     # traceback size sent back to Gemini
HTTP_RETRIES = 2           # extra attempts after HTTP 429/5xx or a dropped connection
BACKOFF_SECONDS = (3.0, 8.0)
MAX_RETRY_DELAY = 30.0     # never wait longer than this for a "retry after" hint
MODAL_WAIT_LIMIT = 15.0    # max seconds to hold an answer while a modal tool is running
POLL_INTERVAL = 0.25

SYSTEM_PROMPT = """\
You are an expert Blender {version} Python (bpy) developer working inside the user's live Blender session.
Reply with ONLY Python code: no markdown fences and no prose outside of # comments.

How your code runs:
- It is executed with exec() on Blender's main thread, with a 3D Viewport context when one is open.
- `bpy`, `bmesh`, `math`, `mathutils` and `context` (= bpy.context) are already defined; other imports are allowed.
- Every reply runs in a fresh namespace. Variables from earlier replies do NOT exist any more:
  look objects up again by name, e.g. bpy.data.objects["Cube"].
- The scene context in each message describes the scene right now. "It"/"this" usually means the
  active object or the object the previous request worked on.

Rules:
- Use the Blender {version} Python API (current operator names, node and socket names, render engine ids).
- Prefer the bpy.data API over bpy.ops.
- Work with the user's existing scene. Never delete, clear or reset existing objects or data unless
  the request explicitly asks for it.
- Use the exact object names from the scene context.
- Never open, save, reload or close files, quit Blender, run undo/redo, start threads or access the network.
- If the request is ambiguous, make a sensible choice and mention it in a short # comment.
"""


# =============================================================================
# Gemini REST client - runs on worker threads, must NOT touch bpy
# =============================================================================

class GeminiError(Exception):
    """An error whose message can be shown to the user as-is."""


_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
_ssl_state = {"context": None}   # switches to certifi's CA bundle if the system store fails


def _certifi_context():
    try:
        import certifi   # bundled with Blender
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _is_cert_error(exc):
    reason = getattr(exc, "reason", exc)
    return (isinstance(reason, ssl.SSLCertVerificationError)
            or "CERTIFICATE_VERIFY_FAILED" in str(reason))


def _urlopen(req, timeout):
    context = _ssl_state["context"]
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=context)
    except urllib.error.URLError as exc:
        if context is not None or isinstance(exc, urllib.error.HTTPError) or not _is_cert_error(exc):
            raise
        fallback = _certifi_context()
        if fallback is None:
            raise
        _ssl_state["context"] = fallback
        return urllib.request.urlopen(req, timeout=timeout, context=fallback)


def _error_details(exc):
    """(message, retry_after_seconds) from an HTTPError returned by the Gemini API."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    message = raw.strip()[:300] or str(exc.reason or "")
    retry_after = None
    try:
        error = json.loads(raw).get("error") or {}
        message = error.get("message") or message
        for detail in error.get("details") or []:
            delay = detail.get("retryDelay") if isinstance(detail, dict) else None
            if isinstance(delay, str) and delay.endswith("s"):
                retry_after = float(delay[:-1])
    except Exception:
        pass
    if retry_after is None and exc.headers is not None:
        try:
            retry_after = float(exc.headers.get("Retry-After"))
        except (TypeError, ValueError):
            pass
    return message, retry_after


def _http_error_message(code, message):
    if code == 400 and "api key" in message.lower():
        return "Invalid API key - check it in the add-on preferences"
    if code in (401, 403):
        return f"Access denied (HTTP {code}): {message}"
    if code == 404:
        return (f"Model not found (HTTP 404): {message} - pick another model in the add-on "
                f"preferences (Test Connection lists the available ones)")
    if code == 429:
        return f"Rate limit or quota exceeded (HTTP 429): {message}"
    return f"HTTP {code}: {message}"


def _request_json(url, api_key, payload=None, timeout=60.0, cancel=None, on_status=None):
    """GET (payload=None) or POST JSON with retries for transient errors."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"x-goog-api-key": api_key, "User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"

    attempt = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise GeminiError("Cancelled")
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
        retry_after = None
        try:
            with _urlopen(req, timeout) as resp:
                body = resp.read()
            try:
                return json.loads(body.decode("utf-8"))
            except ValueError:
                raise GeminiError("Gemini returned an invalid (non-JSON) response") from None
        except urllib.error.HTTPError as exc:
            message, retry_after = _error_details(exc)
            if (exc.code not in _TRANSIENT_HTTP or attempt >= HTTP_RETRIES
                    or (retry_after or 0.0) > MAX_RETRY_DELAY):
                raise GeminiError(_http_error_message(exc.code, message)) from None
            problem = f"HTTP {exc.code}"
        except TimeoutError:
            # The model may just be slow - re-sending would only double the wait.
            raise GeminiError(f"No answer within {timeout:.0f} s - try again or raise the "
                              f"timeout in the add-on preferences") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise GeminiError(f"No answer within {timeout:.0f} s - try again or raise the "
                                  f"timeout in the add-on preferences") from None
            if _is_cert_error(exc):
                raise GeminiError(f"SSL certificate verification failed: {reason}") from None
            if attempt >= HTTP_RETRIES:
                raise GeminiError(f"Network error: {reason}") from None
            problem = "connection problem"

        delay = retry_after if retry_after is not None else BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        attempt += 1
        if on_status is not None:
            on_status(f"Gemini is busy ({problem}) - retrying in {max(1, math.ceil(delay))} s "
                      f"({attempt}/{HTTP_RETRIES})...")
        if cancel is not None:
            if cancel.wait(delay):
                raise GeminiError("Cancelled")
        else:
            time.sleep(delay)


def _model_path(model):
    model = (model or "").strip()
    if model.startswith("models/"):
        model = model[len("models/"):]
    return urllib.parse.quote(model or DEFAULT_MODEL, safe="-._~")


def _extract_text(data):
    if not isinstance(data, dict):
        raise GeminiError("Unexpected response from Gemini")
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise GeminiError(f"Gemini returned no answer ({reason})")
    candidate = candidates[0]
    finish = candidate.get("finishReason") or ""
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    if finish == "MAX_TOKENS":
        raise GeminiError("The answer was cut off (MAX_TOKENS) - ask for less at once or split the task")
    if not text.strip():
        raise GeminiError(f"Gemini returned an empty answer (finish reason: {finish or 'unknown'})")
    return text


def generate_code(api_key, model, payload, timeout, cancel=None, on_status=None):
    url = f"{API_ROOT}/models/{_model_path(model)}:generateContent"
    return _extract_text(_request_json(url, api_key, payload, timeout, cancel, on_status))


_NON_TEXT_MODEL = re.compile(
    r"tts|image|live|audio|embedding|transcribe|robotics|computer-use|translate|veo|imagen|omni|lyria|aqa",
    re.I)


def _model_sort_key(name):
    match = re.match(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(match.group(1)) if match else 0.0
    return ("latest" not in name, -version, name)


def list_models(api_key, timeout=20.0):
    """Names of the Gemini text models this key can use with generateContent."""
    names, token = set(), ""
    for _page in range(20):
        url = f"{API_ROOT}/models?pageSize=1000"
        if token:
            url += "&pageToken=" + urllib.parse.quote(token, safe="")
        data = _request_json(url, api_key, None, timeout)
        for model in data.get("models") or []:
            name = str(model.get("name", "")).split("/", 1)[-1]
            if ("generateContent" in (model.get("supportedGenerationMethods") or [])
                    and name.startswith("gemini") and not _NON_TEXT_MODEL.search(name)):
                names.add(name)
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return sorted(names, key=_model_sort_key)


_FENCE = re.compile(r"```[ \t]*([\w+-]*)[^\n]*\n(.*?)(?:```|\Z)", re.S)


def clean_code(raw):
    """Python source from a model answer, with or without markdown fences."""
    blocks = _FENCE.findall(raw)
    if not blocks:
        return raw.strip()
    python = [body for lang, body in blocks if lang.lower() in ("", "python", "py", "python3")]
    return "\n\n".join(body.strip("\n") for body in (python or [b for _, b in blocks])).strip()


# =============================================================================
# Settings helpers
# =============================================================================

def get_prefs(context=None):
    addon = (context or bpy.context).preferences.addons.get(__name__)
    return addon.preferences if addon is not None else None


def env_api_key():
    for name in ENV_KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def get_api_key(prefs):
    key = prefs.api_key.strip() if prefs is not None else ""
    return key or env_api_key()


def get_model(prefs):
    model = prefs.model.strip() if prefs is not None else ""
    return model or DEFAULT_MODEL


def online_problem():
    if getattr(bpy.app, "online_access", True):
        return None
    if getattr(bpy.app, "online_access_override", False):
        return "Blender was started in offline mode (--offline-mode); Gemini needs internet access"
    return "Online access is disabled: enable Edit > Preferences > System > Network > Allow Online Access"


def setup_problem(context):
    problem = online_problem()
    if problem:
        return problem
    if not get_api_key(get_prefs(context)):
        return "No API key: paste it in Edit > Preferences > Add-ons > Gemini AI Assistant Pro"
    return None


# =============================================================================
# Scene context and request building (main thread)
# =============================================================================

def _rounded(values, digits=3):
    return [round(v, digits) for v in values]


def _describe_object(obj, view_layer):
    info = {"name": obj.name, "type": obj.type}
    try:
        info["location"] = _rounded(obj.location)
        info["rotation_deg"] = _rounded((math.degrees(a) for a in obj.rotation_euler), 1)
        info["scale"] = _rounded(obj.scale)
        info["dimensions"] = _rounded(obj.dimensions)
        if obj.parent is not None:
            info["parent"] = obj.parent.name
        if obj.data is not None:
            info["data"] = obj.data.name
        materials = [slot.material.name for slot in obj.material_slots if slot.material]
        if materials:
            info["materials"] = materials
        modifiers = [f"{mod.name} ({mod.type})" for mod in obj.modifiers]
        if modifiers:
            info["modifiers"] = modifiers
        if obj.users_collection:
            info["collection"] = obj.users_collection[0].name
        if obj.hide_get(view_layer=view_layer):
            info["hidden"] = True
    except Exception:   # exotic object types or objects outside the view layer
        pass
    return info


def get_scene_context(context):
    scene = context.scene
    view_layer = context.view_layer
    active = view_layer.objects.active
    objects = list(scene.objects)

    selected = []
    for obj in view_layer.objects:
        try:
            if obj.select_get(view_layer=view_layer):
                selected.append(obj)
        except RuntimeError:
            pass

    # Active and selected objects first, so the size limit never cuts them off.
    first = ([active] if active is not None else []) + [o for o in selected if o != active]
    first_ptrs = {o.as_pointer() for o in first}
    ordered = first + [o for o in objects if o.as_pointer() not in first_ptrs]

    data = {
        "blender_version": bpy.app.version_string,
        "mode": context.mode,
        "scene": scene.name,
        "active_object": active.name if active is not None else None,
        "selected_objects": [o.name for o in selected][:50],
        "object_count": len(objects),
        "objects": [_describe_object(o, view_layer) for o in ordered[:MAX_SCENE_OBJECTS]],
        "frame": {"current": scene.frame_current, "start": scene.frame_start,
                  "end": scene.frame_end, "fps": scene.render.fps},
        "render_engine": scene.render.engine,
        "unit_system": scene.unit_settings.system,
        "camera": scene.camera.name if scene.camera else None,
        "world": scene.world.name if scene.world else None,
        "collections": [c.name for c in bpy.data.collections][:40],
        "materials": [m.name for m in bpy.data.materials][:40],
    }
    if len(objects) > MAX_SCENE_OBJECTS:
        data["objects_note"] = f"only the first {MAX_SCENE_OBJECTS} of {len(objects)} objects are listed"
    return data


def history_contents(scene):
    """Earlier successful turns as Gemini conversation contents."""
    turns = [e for e in scene.gemini_history if e.status == "OK" and e.ai_response]
    contents = []
    for entry in turns[-MAX_HISTORY_TURNS:]:
        contents.append({"role": "user", "parts": [{"text": entry.user_input}]})
        contents.append({"role": "model", "parts": [{"text": entry.ai_response}]})
    return contents


def system_prompt():
    return SYSTEM_PROMPT.replace("{version}", "%d.%d" % tuple(bpy.app.version[:2]))


def build_request(context, prompt, failed_code=None, error=None):
    scene_json = json.dumps(get_scene_context(context), ensure_ascii=False, separators=(",", ":"))
    task = f"Scene context (JSON):\n{scene_json}\n\nTask: {prompt}"
    if failed_code is not None:
        task += ("\n\nYour previous code for this task raised an error. Data-blocks it created were "
                 "removed, but other changes it made may still be present (see the scene context).\n\n"
                 f"Previous code:\n{failed_code}\n\nError:\n{error}\n\n"
                 "Reply with the complete, corrected code.")
    contents = history_contents(context.scene)
    contents.append({"role": "user", "parts": [{"text": task}]})
    return {"systemInstruction": {"parts": [{"text": system_prompt()}]}, "contents": contents}


# =============================================================================
# Running generated code (main thread)
# =============================================================================

# Data-block collections whose new members are removed when generated code fails,
# so a retry does not pile up a second set of half-built objects.
_ROLLBACK_COLLECTIONS = (
    "objects", "meshes", "curves", "metaballs", "fonts", "lattices", "armatures", "cameras",
    "lights", "lightprobes", "speakers", "volumes", "pointclouds", "hair_curves", "grease_pencils",
    "grease_pencils_v3", "materials", "textures", "images", "node_groups", "worlds", "collections",
    "actions", "particles", "palettes", "linestyles", "masks", "movieclips", "sounds", "cache_files",
    "texts",
)

_MODE_SET = {
    "OBJECT": "OBJECT", "POSE": "POSE", "SCULPT": "SCULPT", "PARTICLE": "PARTICLE_EDIT",
    "PAINT_WEIGHT": "WEIGHT_PAINT", "PAINT_VERTEX": "VERTEX_PAINT", "PAINT_TEXTURE": "TEXTURE_PAINT",
    "EDIT_MESH": "EDIT", "EDIT_CURVE": "EDIT", "EDIT_CURVES": "EDIT", "EDIT_SURFACE": "EDIT",
    "EDIT_TEXT": "EDIT", "EDIT_ARMATURE": "EDIT", "EDIT_METABALL": "EDIT", "EDIT_LATTICE": "EDIT",
    "EDIT_GREASE_PENCIL": "EDIT", "EDIT_POINTCLOUD": "EDIT",
}


def _view3d_override():
    """Context override pointing at a 3D Viewport so view-dependent bpy.ops work."""
    try:
        windows = list(bpy.context.window_manager.windows)
    except Exception:
        return {}
    current = bpy.context.window
    windows.sort(key=lambda w: w != current)   # prefer the window already in the context
    for window in windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region is not None:
                    return {"window": window, "area": area, "region": region}
    return {}


def _id_key(idblock):
    uid = getattr(idblock, "session_uid", None)
    return uid if uid is not None else idblock.as_pointer()


def _snapshot_ids():
    snapshot = {}
    for attr in _ROLLBACK_COLLECTIONS:
        collection = getattr(bpy.data, attr, None)
        if collection is not None:
            snapshot[attr] = {_id_key(i) for i in collection}
    return snapshot


def _new_ids(snapshot):
    created = []
    for attr, before in snapshot.items():
        collection = getattr(bpy.data, attr, None)
        if collection is not None:
            created.extend(i for i in collection if _id_key(i) not in before)
    return created


def _capture_selection(view_layer):
    active = view_layer.objects.active
    selected = set()
    for obj in view_layer.objects:
        try:
            if obj.select_get(view_layer=view_layer):
                selected.add(obj.name)
        except RuntimeError:
            pass
    return {"active": active.name if active is not None else None, "selected": selected}


def _restore_selection(state):
    view_layer = bpy.context.view_layer
    for obj in view_layer.objects:
        want = obj.name in state["selected"]
        try:
            if obj.select_get(view_layer=view_layer) != want:
                obj.select_set(want, view_layer=view_layer)
        except RuntimeError:
            pass
    if state["active"] is not None:
        obj = view_layer.objects.get(state["active"])
        if obj is not None:
            view_layer.objects.active = obj


def _set_mode(mode):
    target = _MODE_SET.get(mode)
    if target is None or bpy.context.mode == mode:
        return
    with bpy.context.temp_override(**_view3d_override()):
        if bpy.ops.object.mode_set.poll():
            bpy.ops.object.mode_set(mode=target)


def _try(func, *args):
    try:
        return func(*args)
    except Exception:
        traceback.print_exc()
        return None


def _rollback(state):
    """Undo what failed code left behind: new data-blocks, mode and selection changes."""
    created = _try(_new_ids, state["ids"]) or []
    removed = 0
    if created:
        _try(_set_mode, "OBJECT")   # never delete data that is still in edit mode
        if bpy.context.mode == "OBJECT":
            try:
                bpy.data.batch_remove(created)
                removed = len(created)
            except Exception:
                traceback.print_exc()
    _try(_restore_selection, state["selection"])
    _try(_set_mode, state["mode"])
    return removed


def _format_exception(exc):
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename == _THIS_FILE:
        tb = tb.tb_next   # hide the add-on's own frames, the model only needs its code
    text = "".join(traceback.format_exception(type(exc), exc, tb)).rstrip()
    if len(text) > MAX_ERROR_CHARS:
        text = "...\n" + text[-MAX_ERROR_CHARS:]
    return text


def _forget_old_sources(keep=20):
    names = [n for n in linecache.cache if isinstance(n, str) and n.startswith("<gemini:")]
    for name in names[:-keep]:
        linecache.cache.pop(name, None)


def execute_ai_code(code, tag="0"):
    """Run generated code. Returns (ok, error_text, retry_allowed)."""
    import bmesh
    import mathutils

    filename = f"<gemini:{tag}>"
    # Register the source so tracebacks show the failing line (helps the auto-fix a lot).
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    _forget_old_sources()
    namespace = {"__name__": "__gemini__", "__file__": filename, "bpy": bpy, "bmesh": bmesh,
                 "math": math, "mathutils": mathutils, "context": bpy.context}
    # NOTE: this is not a sandbox - bpy alone gives full access to Blender and the machine.
    try:
        compiled = compile(code, filename, "exec")
        with bpy.context.temp_override(**_view3d_override()):
            exec(compiled, namespace)
    except BaseException as exc:   # SystemExit too: generated code must never close Blender
        return False, _format_exception(exc), not isinstance(exc, KeyboardInterrupt)
    return True, None, False


_THIS_FILE = execute_ai_code.__code__.co_filename


def run_generated_code(context, code, tag):
    """Execute code; on failure roll back what it created. Returns (ok, error, retry, removed)."""
    view_layer = context.view_layer
    state = {"ids": _snapshot_ids(), "mode": context.mode,
             "selection": _capture_selection(view_layer)}
    ok, error, retry = execute_ai_code(code, tag)
    if ok:
        return True, None, False, 0
    return False, error, retry, _rollback(state)


def save_code_for_review(prompt, code):
    text = bpy.data.texts.get(CODE_TEXT_NAME) or bpy.data.texts.new(CODE_TEXT_NAME)
    header = "# Generated by Gemini AI Assistant\n# Prompt: " + " ".join(prompt.split()) + "\n\n"
    text.from_string(header + code + "\n")
    print(f"\n--- Gemini code for: {prompt} ---\n{code}\n--- end ---")


def summarize_error(error):
    lines = [line for line in error.strip().splitlines() if line.strip()]
    last = lines[-1].strip() if lines else "Unknown error"
    line_no = None
    for line in lines:
        match = re.search(r'File "<gemini:[^"]*>", line (\d+)', line)
        if match:
            line_no = match.group(1)
    return f"Line {line_no}: {last}" if line_no else last


def push_undo(message):
    try:
        if bpy.ops.ed.undo_push.poll():
            bpy.ops.ed.undo_push(message=message)
    except Exception:
        traceback.print_exc()


def add_history(scene, prompt, code, status, info):
    history = scene.gemini_history
    item = history.add()
    item.user_input = prompt
    item.ai_response = code
    item.status = status
    item.info = info[:500]
    while len(history) > MAX_STORED_HISTORY:
        history.remove(0)


def tag_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'TEXT_EDITOR', 'PREFERENCES'}:
                    area.tag_redraw()
    except Exception:
        pass


# =============================================================================
# Background requests: one active job, results handed over through a queue
# =============================================================================

_messages = queue.Queue()
_job = None        # dict of the active request, or None
_job_counter = 0


def is_busy():
    return _job is not None


def _request_worker(job_id, attempt, api_key, model, payload, timeout, cancel):
    def post(kind, text):
        _messages.put({"job": job_id, "attempt": attempt, "kind": kind, "text": text})

    try:
        post("answer", generate_code(api_key, model, payload, timeout, cancel,
                                     on_status=lambda text: post("status", text)))
    except Exception as exc:
        post("error", str(exc) or type(exc).__name__)


def _start_request(context, failed_code=None, error=None):
    job = _job
    prefs = get_prefs(context)
    payload = build_request(context, job["prompt"], failed_code, error)
    args = (job["id"], job["attempt"], get_api_key(prefs), get_model(prefs), payload,
            float(prefs.timeout if prefs is not None else 180), job["cancel"])
    threading.Thread(target=_request_worker, args=args, daemon=True, name="GeminiRequest").start()


def start_job(context, prompt):
    global _job, _job_counter
    prefs = get_prefs(context)
    _job_counter += 1
    _job = {
        "id": _job_counter, "prompt": prompt, "attempt": 0,
        "max_fixes": prefs.max_fix_attempts if prefs is not None else 1,
        "status": f"Waiting for {get_model(prefs)}...", "cancel": threading.Event(),
        "answer": None, "held_since": None,
    }
    try:
        _start_request(context)
    except Exception:
        _job = None
        raise
    if not bpy.app.timers.is_registered(_poll_messages):
        bpy.app.timers.register(_poll_messages, first_interval=POLL_INTERVAL)


def cancel_job():
    global _job
    if _job is not None:
        _job["cancel"].set()
        _job = None
    while True:   # everything still queued belongs to the cancelled job
        try:
            _messages.get_nowait()
        except queue.Empty:
            break
    tag_redraw()


def _end_job():
    global _job
    _job = None
    tag_redraw()


def _hold_reason(job):
    """Why the answer cannot be applied right now (rendering, a modal tool...), or None."""
    for job_type in ("RENDER", "OBJECT_BAKE", "COMPOSITE"):
        try:
            if bpy.app.is_job_running(job_type):
                return "Answer received - waiting for rendering/baking to finish..."
        except Exception:
            pass
    try:
        modal = any(len(w.modal_operators) for w in bpy.context.window_manager.windows)
    except Exception:
        modal = False
    if not modal:
        return None
    if job["held_since"] is None:
        job["held_since"] = time.monotonic()
    if time.monotonic() - job["held_since"] > MODAL_WAIT_LIMIT:
        return None
    return "Answer received - waiting for the active tool to finish..."


def _handle_answer(job, msg):
    context = bpy.context
    scene = context.scene
    prompt = job["prompt"]

    if msg["kind"] == "error":
        add_history(scene, prompt, "", "FAIL", msg["text"])
        _end_job()
        return

    code = clean_code(msg["text"])
    save_code_for_review(prompt, code)
    ok, error, retry_allowed, removed = run_generated_code(context, code, f"{job['id']}.{job['attempt']}")

    if ok:
        info = "Done" if job["attempt"] == 0 else f"Done (fixed after {job['attempt']} failed attempt(s))"
        add_history(scene, prompt, code, "OK", info)
        if scene.gemini_user_prompt.strip() == prompt:   # keep a new prompt typed meanwhile
            scene.gemini_user_prompt = ""
        # History first, then the undo step: Ctrl+Z / Ctrl+Shift+Z keep both in sync.
        push_undo(f"Gemini: {prompt[:50]}")
        _end_job()
        return

    print(f"Gemini code failed (attempt {job['attempt'] + 1}):\n{error}")
    if retry_allowed and job["attempt"] < job["max_fixes"]:
        job["attempt"] += 1
        job["held_since"] = None
        job["status"] = f"The code failed - asking Gemini to fix it ({job['attempt']}/{job['max_fixes']})..."
        _start_request(context, failed_code=code, error=error)
        tag_redraw()
        return

    info = summarize_error(error)
    if removed:
        info += f" (removed {removed} data-block(s) it created)"
    add_history(scene, prompt, code, "FAIL", info)
    _end_job()


def _poll_messages():
    job = None
    try:
        while True:
            try:
                msg = _messages.get_nowait()
            except queue.Empty:
                break
            job = _job
            if job is None or msg["job"] != job["id"] or msg["attempt"] != job["attempt"]:
                continue   # stale: cancelled request or a file was loaded meanwhile
            if msg["kind"] == "status":
                job["status"] = msg["text"]
                tag_redraw()
            else:
                job["answer"] = msg

        job = _job
        if job is not None and job["answer"] is not None:
            reason = _hold_reason(job)
            if reason:
                if job["status"] != reason:
                    job["status"] = reason
                    tag_redraw()
            else:
                msg, job["answer"] = job["answer"], None
                _handle_answer(job, msg)
    except BaseException:
        traceback.print_exc()
        try:
            if _job is not None:
                add_history(bpy.context.scene, _job["prompt"], "", "FAIL",
                            "Internal error - see the system console")
        except Exception:
            pass
        _end_job()   # never leave the UI locked
    return POLL_INTERVAL if _job is not None else None


@persistent
def gemini_on_load_pre(*_args):
    # Timers are dropped when another file is loaded; an answer meant for the old
    # file must never run in the new one.
    cancel_job()


# =============================================================================
# Connection test (add-on preferences)
# =============================================================================

_conn_messages = queue.Queue()
_conn = {"busy": False, "ok": None, "text": "", "models": []}


def _conn_worker(api_key, model, timeout):
    try:
        models = list_models(api_key, timeout)
        try:
            _request_json(f"{API_ROOT}/models/{_model_path(model)}", api_key, None, timeout)
            ok, note = True, f"Model '{model}' is available."
        except GeminiError as exc:
            ok, note = False, str(exc)
        result = {"ok": ok, "text": f"Connected - {len(models)} text models available. {note}",
                  "models": models}
    except Exception as exc:
        result = {"ok": False, "text": str(exc) or type(exc).__name__, "models": []}
    _conn_messages.put(result)


def _poll_conn():
    try:
        result = _conn_messages.get_nowait()
    except queue.Empty:
        return POLL_INTERVAL
    _conn.update(result, busy=False)
    tag_redraw()
    return None


# =============================================================================
# UI helpers
# =============================================================================

def _chars_per_line(context, fallback=40):
    try:
        scale = context.preferences.system.ui_scale
        return max(16, int((context.region.width - 40 * scale) / (6.5 * scale)))
    except Exception:
        return fallback


def wrap_label(layout, text, width, icon='NONE'):
    col = layout.column(align=True)
    for i, line in enumerate(textwrap.wrap(text, width) or [""]):
        col.label(text=line, icon=icon if i == 0 else ('BLANK1' if icon != 'NONE' else 'NONE'))


# =============================================================================
# Blender classes
# =============================================================================

class GeminiPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    api_key: StringProperty(
        name="API Key", subtype='PASSWORD',
        description="Gemini API key from Google AI Studio. Leave empty to use the "
                    "GEMINI_API_KEY environment variable",
    )
    model: StringProperty(
        name="Model", default=DEFAULT_MODEL,
        description="Gemini model id (see ai.google.dev/gemini-api/docs/models)",
    )
    max_fix_attempts: IntProperty(
        name="Auto-fix Attempts", default=1, min=0, max=3,
        description="How many times Gemini may try to fix its own code after an error",
    )
    timeout: IntProperty(
        name="Timeout (s)", default=180, min=30, max=900,
        description="How long to wait for one answer from Gemini",
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "api_key")
        if not self.api_key.strip() and env_api_key():
            col.label(text="Using the API key from the environment variable", icon='INFO')
        row = col.row(align=True)
        row.prop(self, "model")
        row.menu("GEMINI_MT_model_menu", text="", icon='DOWNARROW_HLT')
        col.prop(self, "max_fix_attempts")
        col.prop(self, "timeout")

        box = layout.box()
        row = box.row()
        row.enabled = not _conn["busy"]
        row.operator("object.gemini_test_connection", icon='URL')
        if _conn["text"]:
            icon = 'SORTTIME' if _conn["busy"] else ('CHECKMARK' if _conn["ok"] else 'ERROR')
            wrap_label(box, _conn["text"], 90, icon)
        problem = online_problem()
        if problem:
            sub = box.column()
            sub.alert = True
            wrap_label(sub, problem, 90, 'ERROR')

        wrap_label(layout, "Generated code runs with full Blender permissions (no sandbox). "
                           "Review it in the Text Editor when in doubt.", 90, 'INFO')


class GEMINI_HistoryItem(bpy.types.PropertyGroup):
    user_input: StringProperty()
    ai_response: StringProperty()
    status: StringProperty(default="OK")
    info: StringProperty(default="")


class GEMINI_MT_model_menu(bpy.types.Menu):
    bl_idname = "GEMINI_MT_model_menu"
    bl_label = "Available Gemini Models"

    def draw(self, context):
        layout = self.layout
        if not _conn["models"]:
            layout.label(text="Press 'Test Connection' to load the list", icon='INFO')
            return
        for name in _conn["models"]:
            layout.operator("object.gemini_set_model", text=name).model = name


class OBJECT_OT_GeminiExecute(bpy.types.Operator):
    """Send the request to Gemini and run the Python code it writes"""
    bl_idname = "object.gemini_execute"
    bl_label = "Send to Gemini"

    def execute(self, context):
        if is_busy():
            if bpy.app.timers.is_registered(_poll_messages):
                self.report({'WARNING'}, "Gemini is still working - wait or press Cancel")
                return {'CANCELLED'}
            cancel_job()   # orphaned request whose timer is gone - start over
        problem = setup_problem(context)
        if problem:
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}
        prompt = context.scene.gemini_user_prompt.strip()
        if not prompt:
            self.report({'WARNING'}, "Type a request first")
            return {'CANCELLED'}
        try:
            start_job(context, prompt)
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, f"Could not start the request: {exc}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Request sent - Blender stays usable while Gemini works")
        return {'FINISHED'}


class OBJECT_OT_GeminiCancel(bpy.types.Operator):
    """Stop waiting for the current request (its answer will be ignored)"""
    bl_idname = "object.gemini_cancel"
    bl_label = "Cancel Gemini Request"

    @classmethod
    def poll(cls, context):
        return is_busy()

    def execute(self, context):
        prompt = _job["prompt"] if _job is not None else ""
        cancel_job()
        add_history(context.scene, prompt, "", "FAIL", "Cancelled")
        self.report({'INFO'}, "Request cancelled")
        return {'FINISHED'}


class OBJECT_OT_GeminiClearHistory(bpy.types.Operator):
    """Delete this scene's chat history (Gemini forgets the earlier requests)"""
    bl_idname = "object.gemini_clear_history"
    bl_label = "Clear Chat History"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.gemini_history) > 0

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        context.scene.gemini_history.clear()
        return {'FINISHED'}


class OBJECT_OT_GeminiTestConnection(bpy.types.Operator):
    """Check the API key and model, and load the list of available Gemini models"""
    bl_idname = "object.gemini_test_connection"
    bl_label = "Test Connection"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if _conn["busy"]:
            return {'CANCELLED'}
        problem = setup_problem(context)
        if problem:
            _conn.update(ok=False, text=problem)
            self.report({'ERROR'}, problem)
            return {'CANCELLED'}
        prefs = get_prefs(context)
        _conn.update(busy=True, ok=None, text="Connecting to Gemini...")
        threading.Thread(target=_conn_worker, args=(get_api_key(prefs), get_model(prefs), 20.0),
                         daemon=True, name="GeminiConnectionTest").start()
        if not bpy.app.timers.is_registered(_poll_conn):
            # persistent: not tied to the open file, must survive File > Open
            bpy.app.timers.register(_poll_conn, first_interval=POLL_INTERVAL, persistent=True)
        return {'FINISHED'}


class OBJECT_OT_GeminiSetModel(bpy.types.Operator):
    """Use this Gemini model"""
    bl_idname = "object.gemini_set_model"
    bl_label = "Set Gemini Model"
    bl_options = {'INTERNAL'}

    model: StringProperty()

    def execute(self, context):
        prefs = get_prefs(context)
        if prefs is None or not self.model:
            return {'CANCELLED'}
        prefs.model = self.model
        try:
            context.preferences.is_dirty = True   # so auto-save keeps the choice
        except Exception:
            pass
        _conn.update(ok=True, text=f"Model set to '{self.model}'.")
        return {'FINISHED'}


class VIEW3D_PT_GeminiPanel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Gemini AI'
    bl_label = "Gemini AI Agent"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        width = _chars_per_line(context)
        job = _job

        col = layout.column(align=True)
        col.label(text="Ask Gemini:")
        col.prop(scene, "gemini_user_prompt", text="")
        row = col.row(align=True)
        row.scale_y = 1.3
        if job is not None:
            row.operator("object.gemini_cancel", text="Cancel", icon='CANCEL')
            box = layout.box()
            wrap_label(box, job["status"], width, 'SORTTIME')
        else:
            row.operator("object.gemini_execute", text="Run", icon='PLAY')
            problem = setup_problem(context)
            if problem:
                box = layout.box()
                box.alert = True
                wrap_label(box, problem, width, 'ERROR')

        history = scene.gemini_history
        if len(history):
            layout.separator()
            layout.label(text="Chat History:")
            box = layout.box()
            shown = list(history)[-MAX_SHOWN_HISTORY:]
            if len(history) > len(shown):
                box.label(text=f"{len(history) - len(shown)} earlier request(s) hidden", icon='THREE_DOTS')
            for entry in shown:
                item = box.column(align=True)
                wrap_label(item, entry.user_input, width, 'USER')
                result = item.column(align=True)
                result.alert = entry.status != "OK"
                wrap_label(result, entry.info, width, 'CHECKMARK' if entry.status == "OK" else 'ERROR')
            layout.label(text=f"Last code: Text Editor > {CODE_TEXT_NAME}", icon='TEXT')
            layout.operator("object.gemini_clear_history", icon='TRASH', text="Clear History")


classes = (
    GeminiPreferences,
    GEMINI_HistoryItem,
    GEMINI_MT_model_menu,
    OBJECT_OT_GeminiExecute,
    OBJECT_OT_GeminiCancel,
    OBJECT_OT_GeminiClearHistory,
    OBJECT_OT_GeminiTestConnection,
    OBJECT_OT_GeminiSetModel,
    VIEW3D_PT_GeminiPanel,
)


def _remove_load_handler():
    for handler in list(bpy.app.handlers.load_pre):
        if getattr(handler, "__name__", "") == "gemini_on_load_pre":
            bpy.app.handlers.load_pre.remove(handler)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gemini_user_prompt = StringProperty(
        name="Prompt", description="Describe what Gemini should do in the scene")
    bpy.types.Scene.gemini_history = CollectionProperty(type=GEMINI_HistoryItem)
    _remove_load_handler()
    bpy.app.handlers.load_pre.append(gemini_on_load_pre)


def unregister():
    cancel_job()
    for func in (_poll_messages, _poll_conn):
        if bpy.app.timers.is_registered(func):
            bpy.app.timers.unregister(func)
    _remove_load_handler()
    del bpy.types.Scene.gemini_history
    del bpy.types.Scene.gemini_user_prompt
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
