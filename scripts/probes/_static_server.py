"""pywebview server extension: frontend GETs only, no HTTP bridge endpoint."""

import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class StaticHandler(SimpleHTTPRequestHandler):
    def list_directory(self, path: str | os.PathLike[str]) -> None:
        self.send_error(403, "Directory listing disabled")

    def log_message(self, format: str, *args: object) -> None:
        pass

    def translate_path(self, path: str) -> str:
        translated = Path(super().translate_path(path)).resolve()
        root = Path(self.directory).resolve()
        return str(translated if translated.is_relative_to(root) else root / ".not-found")


class StaticOnlyServer:
    common_path: str = ""

    def __init__(self, root: Path, port: int | None) -> None:
        self.root_path = str(root)
        self.js_callback: dict[str, Any] = {}
        self.js_api_endpoint = None
        handler = functools.partial(StaticHandler, directory=str(root))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port or 0), handler)
        self.port = self.httpd.server_port
        self.address = f"http://127.0.0.1:{self.port}/"
        self.running = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @classmethod
    def start_server(
        cls, urls: list[str], http_port: int | None, **kwargs: object
    ) -> tuple[str, str, "StaticOnlyServer"]:
        paths = [Path(url.split("#")[0]).resolve() for url in urls]
        roots = {path if path.is_dir() else path.parent for path in paths}
        if len(roots) != 1:
            raise ValueError("Probe server requires one static frontend directory")
        root = roots.pop()
        cls.common_path = str(root)
        server = cls(root, http_port)
        return server.address, str(root), server

    @property
    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.running = False
