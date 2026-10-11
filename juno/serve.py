"""Llama X JUNO's local server. FAAM starts it on demand; it only listens on this
Mac (127.0.0.1) and only answers requests addressed to localhost.

    GET  /health    -> {ok, name, base, tuned, bandMeanZ}
    POST /forecast  {prompt}            -> {probs: {A..E}}
    POST /chat      {messages, maxTokens} -> {text}
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime import BASE, Juno  # noqa: E402

PORT = int(os.environ.get("JUNO_PORT", "8899"))
MAX_BODY = 64 * 1024
LOCK = threading.Lock()              # one inference at a time on the GPU
JUNO: Juno | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):           # quiet
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and not self.headers.get("Origin")

    def do_GET(self):
        if not self._local():
            return self._send({"error": "forbidden"}, 403)
        if self.path == "/health":
            return self._send({"ok": True, "name": JUNO.meta["name"], "base": BASE, "tuned": JUNO.tuned,
                               "bandMeanZ": JUNO.meta["bandMeanZ"], "builtWith": "Built with Llama"})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._local():
            return self._send({"error": "forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, MAX_BODY)) or b"{}") if 0 < n <= MAX_BODY else {}
        except ValueError:
            body = {}
        try:
            if self.path == "/forecast":
                prompt = str(body.get("prompt") or "")[:4000]
                if not prompt:
                    return self._send({"error": "prompt required"}, 400)
                with LOCK:
                    return self._send({"probs": JUNO.band_probs(prompt)})
            if self.path == "/chat":
                msgs = [{"role": m["role"], "content": str(m["content"])[:4000]}
                        for m in (body.get("messages") or [])[-8:]
                        if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")]
                if not msgs:
                    return self._send({"error": "messages required"}, 400)
                with LOCK:
                    text = JUNO.chat(msgs, max_tokens=max(16, min(int(body.get("maxTokens") or 300), 800)))
                return self._send({"text": text})
        except Exception as e:  # noqa: BLE001
            return self._send({"error": str(e)[:200]}, 500)
        self._send({"error": "not found"}, 404)


def main():
    global JUNO
    JUNO = Juno(adapters=True)
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"{JUNO.meta['name']} ({'fine-tuned' if JUNO.tuned else 'base, untuned'}) on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
