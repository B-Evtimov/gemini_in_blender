# Gemini AI Assistant for Blender

An open-source Blender add-on that brings Google's Gemini into the 3D Viewport. Describe what you want in plain language — the add-on asks Gemini for Blender Python, runs it, and if the code fails, rolls back what it created and lets Gemini fix its own code.

## Features

- **Natural language to code** — objects, materials, modifiers, animation, scene setup from a text prompt.
- **Non-blocking** — requests run on a background thread; Blender stays fully usable while Gemini thinks. A **Cancel** button stops waiting at any time.
- **Auto-correction with rollback** — when generated code raises an error, everything that attempt created is removed and the full traceback (including the failing line) goes back to Gemini for a fix. Retries never pile up duplicate objects.
- **Chat history** — follow-ups work ("Create a cube" → "Now move it up"). The history is saved in the `.blend` file.
- **One undo step per AI action** — `Ctrl+Z` reverts an AI change together with its history entry; `Ctrl+Shift+Z` brings both back.
- **Scene-aware** — every request carries a compact description of the current scene: active/selected objects, transforms, sizes, materials, modifiers, collections, frame range, render engine.
- **Safe hand-off** — answers wait while you render, bake or use a modal tool, and loading another file cancels a running request so it can never run in the wrong file.
- **Resilient networking** — automatic retries when the API is overloaded (HTTP 503) or rate-limited, clear error messages for bad keys or unknown models, and an SSL fallback for systems with a broken certificate store.
- **Model picker** — *Test Connection* checks your key and model and lists every Gemini text model your key can use.
- **Code review** — every generated script is saved to the Text Editor (`gemini_last_code.py`) and printed to the system console.
- Respects Blender's **Allow Online Access** setting.


## Demo video


https://github.com/user-attachments/assets/d088070a-e2d3-4d24-a7f2-2e73e9d7ce77



## Requirements

- Blender 3.2 or newer (tested on 4.2 LTS, 4.5 LTS, 5.0 and 5.3)
- A Gemini API key (the free tier works)

## Installation

1. Download `gemini_in_blender.py` from this repository.
2. In Blender open `Edit > Preferences > Add-ons`.
   - Blender 4.2 and newer: open the **⌄** menu in the top-right corner and choose **Install from Disk…**
   - Blender 3.2 – 4.1: click **Install…**
3. Select the file and enable **Gemini AI Assistant Pro**.
4. Expand the add-on, paste your **API Key** and press **Test Connection**.
5. Blender 4.2+: make sure `Edit > Preferences > System > Network > Allow Online Access` is enabled.

> **Upgrading from version 1.x – 2.1?** Those were installed from a file called `Gemini_for_blender*.py`, so Blender keeps them as a separate add-on next to this one. Remove the old *Gemini AI Assistant (Pro)* entry with version 1.x – 2.1 in `Edit > Preferences > Add-ons` first — otherwise the two copies fight over the same panel and you may keep seeing the old one.

## Getting an API key

1. Open [Google AI Studio](https://aistudio.google.com/).
2. Click **Get API key** and create a key.
3. Paste it into the add-on preferences — or leave the field empty and set the `GEMINI_API_KEY` environment variable before starting Blender (keeps the key out of `userpref.blend`).

## Usage

1. In the 3D Viewport press `N` and open the **Gemini AI** tab.
2. Type a request, e.g. *"Create a procedural landscape with 50 random rocks"*.
3. Click **Run**. Keep working — the result is applied when Gemini answers.
4. Follow up with things like *"make them darker"* or *"move the camera so all rocks are visible"*.
5. **Clear History** starts a fresh conversation.

## Settings

| Setting | Default | Description |
|---|---|---|
| API Key | — | Gemini API key. Empty = use the `GEMINI_API_KEY` environment variable. |
| Model | `gemini-3.8-flash` | Any Gemini model id. After *Test Connection*, the ⌄ button lists the available ones. `gemini-flash-latest` always follows the newest Flash model. |
| Auto-fix Attempts | 1 | How many times Gemini may fix its own code after an error (0–3). |
| Timeout | 180 s | How long to wait for one answer. Raise it for very large requests on slower models. |

## How it works

1. The prompt, a JSON snapshot of the scene and up to 6 earlier successful turns are sent to the Gemini `generateContent` REST endpoint (Python standard library only — nothing to install).
2. The request runs on a worker thread; a Blender timer picks up the answer on the main thread.
3. The code is executed with a 3D Viewport context. If it fails, new data-blocks are removed, mode and selection are restored, and the traceback is sent back for a fix.
4. On success the history entry is stored and one undo step is pushed.

## ⚠️ Security note

This add-on executes AI-generated Python with full Blender permissions — there is no sandbox. Don't use it on files or machines where you can't afford surprises, and check the generated code in the Text Editor when in doubt. Keys entered in the preferences are stored in plain text in `userpref.blend`; use the `GEMINI_API_KEY` environment variable if that matters to you.

## Changelog

### 2.3.0
- **Fixed:** `HTTP Error 404: Not Found` on every request ([#1](https://github.com/B-Evtimov/GeminiInBlender/issues/1)) — versions up to 2.1 had the retired `gemini-1.5-flash` model hard-coded. The model has been a setting since 2.2, and *Test Connection* now lists the models your key can use.
- **Fixed:** an auto-fix retry ran on top of the failed attempt, so everything it had created was duplicated (e.g. 10 rocks instead of 5). Failed attempts are now rolled back first.
- **Fixed:** opening or creating a file while a request was running left the add-on stuck on "AI is working" until Blender was restarted.
- **Fixed:** undo followed by redo of an AI step lost its chat-history entry.
- **Fixed:** `sys.exit()` / `exit()` in generated code escaped the add-on and could close Blender without saving.
- **Fixed:** the scene context described the first 30 objects of the whole file (all scenes, alphabetically) instead of the current scene.
- **Fixed:** a new prompt typed while Gemini was working was erased when the previous answer arrived.
- **New:** Cancel button, Test Connection with model list, retries for overloaded/rate-limited API, render/modal-tool hold, Allow Online Access check, `GEMINI_API_KEY` support, Auto-fix Attempts and Timeout settings, wrapped messages in the panel.
- **Improved:** richer scene context, tracebacks that quote the failing line, clearer system prompt (fresh namespace per reply, never delete the user's data unless asked), default model `gemini-3.8-flash`.

## License

MIT — see [LICENSE](LICENSE).

---
*Created by **Boris Evtimov***
