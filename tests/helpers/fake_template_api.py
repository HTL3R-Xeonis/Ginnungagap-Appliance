"""Template API double with optional real HTTP transport and fault injection."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from image.build.ports import TemplateEntry


class FakeTemplateApi:
    def __init__(self, entries=(), *, on_refresh=None):
        self.entries = list(entries)
        self.on_refresh = on_refresh
        self.calls = []
        self.failures = defaultdict(list)

    def inject(self, method, exc, *, times=1):
        self.failures[method].extend([exc] * times)

    def _call(self, name):
        self.calls.append(name)
        if self.failures[name]:
            raise self.failures[name].pop(0)

    def check(self):
        self._call("check")

    def list_templates(self):
        self._call("list_templates")
        return list(self.entries)

    def refresh(self):
        self._call("refresh")
        if self.on_refresh:
            result = self.on_refresh()
            if result is not None:
                self.entries = list(result)

    def add(self, name, id, file, source_dir="", **raw):
        entry = TemplateEntry(name, id, file, source_dir, raw)
        self.entries.append(entry)
        return entry

    @contextmanager
    def server(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/api/templates":
                    self.send_error(404)
                    return
                rows = []
                for entry in api.list_templates():
                    row = asdict(entry)
                    row.pop("raw")
                    row["_source_dir"] = row.pop("source_dir")
                    rows.append({**entry.raw, **row})
                body = json.dumps({"templates": rows, "count": len(rows)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/api/refresh":
                    self.send_error(404)
                    return
                api.refresh()
                self.send_response(202)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
