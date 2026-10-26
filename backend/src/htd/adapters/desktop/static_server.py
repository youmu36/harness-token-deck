"""pywebview static-server extension: loopback GETs, no HTTP bridge routes."""

import errno
import functools
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class StaticHandler(SimpleHTTPRequestHandler):
    def guess_type(self, path: str | os.PathLike[str]) -> str:
        # These are our build assets. Avoid initializing the machine-wide Windows
        # MIME registry (and racing that initialization in parallel HTTP workers).
        return {
            ".html": "text/html",
            ".js": "text/javascript",
            ".mjs": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".map": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".ttf": "font/ttf",
        }.get(Path(path).suffix.lower(), "application/octet-stream")

    def do_GET(self) -> None:
        # Never answer 304 using a cached entry page from another installation.
        for header in ("If-Modified-Since", "If-None-Match"):
            if header in self.headers:
                del self.headers[header]
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def list_directory(self, path: str | os.PathLike[str]) -> None:
        self.send_error(403, "Directory listing disabled")

    def log_message(self, format: str, *args: object) -> None:
        pass

    def translate_path(self, path: str) -> str:
        translated = Path(super().translate_path(path)).resolve()
        root = Path(self.directory).resolve()
        return str(translated if translated.is_relative_to(root) else root / ".not-found")


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can bind two listeners and route a new window to an
    # older application's assets. Keep the listener exclusive on this platform.
    allow_reuse_address = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="htd-static-file")
        self._slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        self._slots.acquire()
        try:
            request.settimeout(5)
            self._workers.submit(self._serve_request, request, client_address)
        except BaseException:
            self._slots.release()
            self.shutdown_request(request)
            raise

    def _serve_request(self, request: socket.socket, client_address: Any) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_close(self) -> None:
        super().server_close()
        self._workers.shutdown(wait=True)

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class StaticOnlyServer:
    common_path: str = ""

    def __init__(self, root: Path, port: int | None) -> None:
        self.root_path = str(root)
        self.js_callback: dict[str, Any] = {}
        self.js_api_endpoint = None
        handler = functools.partial(StaticHandler, directory=str(root))
        try:
            self.httpd = ExclusiveHTTPServer(("127.0.0.1", port or 0), handler)
        except OSError as error:
            if not port or (error.errno != errno.EADDRINUSE and getattr(error, "winerror", None) != 10048):
                raise
            self.httpd = ExclusiveHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_port
        self.address = f"http://127.0.0.1:{self.port}/"
        self.running = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="htd-static-http", daemon=True)
        self.thread.start()

    @classmethod
    def start_server(
        cls, urls: list[str], http_port: int | None, **kwargs: object
    ) -> tuple[str, str, "StaticOnlyServer"]:
        paths = [Path(url.split("#")[0].split("?")[0]).resolve() for url in urls]
        roots = {path if path.is_dir() else path.parent for path in paths}
        if len(roots) != 1:
            raise ValueError("静态服务只接受一个前端目录")
        root = roots.pop()
        cls.common_path = str(root)
        server = cls(root, http_port)
        return server.address, str(root), server

    @property
    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        if self.running:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=1)
            self.running = False
