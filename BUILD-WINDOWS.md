# Building the packaged Windows app (FAAM.exe)

`FAAM.exe` is a self-contained Windows app with its **own window** (powered by
Microsoft Edge **WebView2**, via `pywebview`). It bundles the Python server, the
dashboard, and a tiny launcher into a single file — **end users install nothing**,
they just double-click `FAAM.exe`.

> The `.exe` must be built **on Windows** (PyInstaller can't cross-compile, and
> WebView2 is Windows-only). Pick one of the two paths below.

## Files involved
- `winshell.py` — the WebView2 window that starts the server and shows FAAM.
- `app.py`, `static/`, `advisers/` — the FAAM server + UI (bundled in).
- `FAAM.ico` — the app icon.
- `build_windows.bat` — one-click local build.
- `.github/workflows/build-windows.yml` — automated cloud build.

## Option A — Build on a Windows PC (one click)
1. Copy this whole folder to a Windows PC with **Python 3.9+** installed
   (tick *Add Python to PATH* during install).
2. Double-click **`build_windows.bat`**.
3. When it finishes, your app is at **`dist\FAAM.exe`**. Double-click to run.

That's it. (Under the hood it runs `pip install pywebview pyinstaller` then
`pyinstaller … winshell.py`.)

## Option B — Build in the cloud (no Windows machine)
1. Push this project to a GitHub repo (the workflow file must end up at
   `.github/workflows/build-windows.yml` in the repo root).
2. On GitHub → **Actions** → **Build FAAM Windows EXE** → **Run workflow**.
3. When it's green, download the **`FAAM-windows`** artifact — that's `FAAM.exe`.
   (Pushing a tag like `v1.0.0` also attaches the exe to a GitHub Release.)

## Requirements on the user's PC
- **WebView2 Runtime** — already present on Windows 11 and updated Windows 10.
  If it's ever missing, the free Microsoft "Evergreen" installer adds it in one click.
- An **OpenAI API key** — FAAM prompts for it on first launch and stores it at
  `%USERPROFILE%\.faam\key`.

## What the app does
Launches its own FAAM window, runs the local server, and prompts for the key on
first run — the same experience as the macOS app. FAAM still **never places
trades**; it prepares orders for you to review at your broker.

---
Prefer no build at all? The plain **`/download/windows`** package (run with
`python app.py`, opens in your browser) still works with zero setup — the `.exe`
is the polished, windowed upgrade.
