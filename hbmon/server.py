"""HTTP server: JSON API plus the static browser UI."""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import string
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .jobs import VIDEO_EXTS, QueueManager, is_hidden, natural_key

STATIC_DIR = Path(__file__).parent / "static"
TEXT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def list_roots() -> list[dict]:
    roots = [{"name": "~ " + str(Path.home()), "path": str(Path.home()), "dir": True}]
    if sys.platform == "win32":
        drives = getattr(os, "listdrives", None)
        letters = drives() if drives else [f"{c}:\\" for c in string.ascii_uppercase
                                           if os.path.exists(f"{c}:\\")]
        roots += [{"name": d, "path": d, "dir": True} for d in letters]
    else:
        roots.append({"name": "/", "path": "/", "dir": True})
    return roots


def list_dir(path: str, show_hidden: bool, all_files: bool) -> list[dict]:
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if not show_hidden and is_hidden(e):
                    continue
                is_dir = e.is_dir()
                if not is_dir:
                    if not e.is_file():
                        continue
                    if not all_files and os.path.splitext(e.name)[1].lower() not in VIDEO_EXTS:
                        continue
                st = e.stat()
                out.append({"name": e.name, "path": e.path, "dir": is_dir,
                            "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue
    out.sort(key=lambda x: (not x["dir"], natural_key(x["name"])))
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "hbmon"
    qm: QueueManager
    allowed_hosts: set[str]
    defaults: dict

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- helpers --------------------------------------------------------------

    def _host_ok(self) -> bool:
        """Reject DNS-rebinding: Host must be an IP literal, localhost or allow-listed."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:host.find("]")]
        elif host.count(":") == 1:
            host = host.split(":")[0]
        if host in self.allowed_hosts or host == "localhost":
            return True
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _error(self, code: int, msg: str):
        self._json({"error": msg}, code)

    # -- routing --------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "bad host")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/state":
                return self._json(self.qm.snapshot())
            if url.path == "/api/config":
                return self._json({"roots": list_roots(), "sep": os.sep,
                                   "hb_cmd": self.qm.hb_cmd, **self.defaults})
            if url.path == "/api/presets":
                return self._json(self.qm.presets(refresh=q.get("refresh") == "1"))
            if url.path == "/api/ls":
                path = q.get("path", "")
                return self._json(list_dir(path, q.get("hidden") == "1", q.get("all") == "1"))
            if url.path == "/api/log":
                return self._json(self.qm.log_tail(q.get("id", ""), int(q.get("lines", 80))))
        except OSError as e:
            return self._error(400, str(e))
        return self._static(url.path)

    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "bad host")
        # A custom header forces a CORS preflight, so other sites can't post here.
        if self.headers.get("X-HBMon") != "1":
            return self._error(403, "missing X-HBMon header")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error(400, "bad json")
        path = urlparse(self.path).path
        qm = self.qm
        try:
            if path == "/api/jobs":
                return self._json(qm.add(
                    paths=body.get("paths", []), dest=body["dest"], preset=body.get("preset", ""),
                    ext=body.get("ext", "mkv"), keep_structure=body.get("keep_structure", True),
                    existing=body.get("existing", "skip"), extra_args=body.get("extra_args", "")))
            if path == "/api/remove":
                qm.remove(body.get("ids", []))
            elif path == "/api/move":
                qm.move(body.get("ids", []), body.get("before"))
            elif path == "/api/retry":
                qm.retry(body.get("ids", []))
            elif path == "/api/clear_done":
                qm.clear_done()
            elif path == "/api/control":
                qm.control(body.get("action", ""))
            else:
                return self._error(404, "not found")
        except (KeyError, ValueError, OSError) as e:
            return self._error(400, str(e))
        return self._json({"ok": True})

    def _static(self, path: str):
        if path == "/":
            path = "/index.html"
        f = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in f.parents or not f.is_file():
            return self._error(404, "not found")
        # Don't trust mimetypes for these: Windows' registry can map .js to text/plain.
        ctype = TEXT_TYPES.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self._send(200, f.read_bytes(), ctype)


def make_server(host: str, port: int, qm: QueueManager, allowed_hosts: set[str],
                defaults: dict) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "qm": qm, "allowed_hosts": {h.lower() for h in allowed_hosts}, "defaults": defaults})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
