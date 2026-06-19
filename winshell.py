"""FAAM for Windows — a native WebView2 window that hosts the local FAAM server.

This mirrors the macOS `faamview.swift` app: it starts the bundled Python server
(app.py) in-process and shows it in a real, titled desktop window powered by
Microsoft Edge WebView2 (via pywebview). Packaged into a single self-contained
FAAM.exe with PyInstaller — see build_windows.bat / BUILD-WINDOWS.md.

Build-time deps (NOT shipped to users): pywebview, pyinstaller.
The end user installs nothing: they just double-click FAAM.exe.
"""
import io
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

# When frozen as a windowed (no-console) exe, stdout/stderr are None — guard them
# so the server's startup prints don't crash the process.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# PyInstaller unpacks bundled data (static/, advisers/, app.py) to sys._MEIPASS.
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PORT = int(os.environ.get("FAAM_PORT") or "8765")
FAAM_DIR = Path.home() / ".faam"
KEY_FILE = FAAM_DIR / "key"


def disk_key():
    """The OpenAI key from the environment or a previous run (~/.faam/key)."""
    env = os.environ.get("OPENAI_API_KEY")
    if env and env.strip():
        return env.strip()
    try:
        k = KEY_FILE.read_text(encoding="utf-8").strip()
        if k:
            return k
    except Exception:
        pass
    return None


def prompt_key():
    """First-run prompt for the OpenAI key (tkinter ships with Python)."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        val = simpledialog.askstring(
            "Welcome to FAAM",
            "Paste your OpenAI API key (sk-...).\n"
            "Stored only on this PC at %USERPROFILE%\\.faam\\key.",
            show="*",
        )
        root.destroy()
        return (val or "").strip() or None
    except Exception:
        return None


class Api:
    """Exposed to the page as window.pywebview.api — opens broker / Stripe / news
    links in the user's default browser instead of the app window."""

    def open_external(self, url):
        import webbrowser
        try:
            webbrowser.open(str(url))
        except Exception:
            pass
        return True


def wait_for_server():
    url = f"http://127.0.0.1:{PORT}/api/health"
    for _ in range(80):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)


def main():
    key = disk_key() or prompt_key()
    if not key:
        return
    try:
        FAAM_DIR.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key, encoding="utf-8")
    except Exception:
        pass

    # Configure the server BEFORE importing app (it reads env at import time).
    os.environ["FAAM_PORT"] = str(PORT)
    os.environ["OPENAI_API_KEY"] = key
    os.environ.setdefault("FAAM_ROOT", str(BASE))
    sys.path.insert(0, str(BASE))

    import app  # the bundled FAAM server (app.py)
    threading.Thread(target=app.main, daemon=True).start()
    wait_for_server()

    import webview  # pywebview → WebView2 (Edge Chromium) on Windows
    webview.create_window(
        "FAAM",
        f"http://127.0.0.1:{PORT}/login",
        width=1320, height=880, min_size=(900, 600),
        js_api=Api(),
    )
    webview.start()  # blocks until the window is closed; the daemon server exits with us


if __name__ == "__main__":
    main()
