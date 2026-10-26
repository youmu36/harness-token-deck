"""The production pywebview server exposes only files under its loopback static root."""

from http.client import HTTPConnection
from pathlib import Path

import pytest
from htd.adapters.desktop.static_server import StaticOnlyServer


def test_asset_mime_types_do_not_read_the_machine_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimetypes

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Application assets must not initialize the system MIME registry")

    monkeypatch.setattr(mimetypes, "init", forbidden)
    for name in ("index.html", "chunk.js", "style.css", "icon.svg", "font.woff2"):
        (tmp_path / name).write_bytes(b"synthetic asset")
    server = StaticOnlyServer(tmp_path, None)
    try:
        for name, mime in (
            ("index.html", "text/html"),
            ("chunk.js", "text/javascript"),
            ("style.css", "text/css"),
            ("icon.svg", "image/svg+xml"),
            ("font.woff2", "font/woff2"),
        ):
            connection = HTTPConnection("127.0.0.1", server.port, timeout=1)
            try:
                connection.request("GET", "/" + name)
                response = connection.getresponse()
                assert response.status == 200 and response.getheader("Content-Type") == mime
                assert response.read() == b"synthetic asset"
            finally:
                connection.close()
    finally:
        server.stop()


def test_occupied_static_port_loads_own_assets_on_another_port(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "index.html").write_text("old frontend", encoding="utf-8")
    (new / "index.html").write_text("new frontend", encoding="utf-8")
    first = StaticOnlyServer(old, None)
    second = StaticOnlyServer(new, first.port)
    try:
        assert second.port != first.port
        for server, body in ((first, b"old frontend"), (second, b"new frontend")):
            connection = HTTPConnection("127.0.0.1", server.port, timeout=1)
            try:
                connection.request("GET", "/index.html?build=rebuilt")
                response = connection.getresponse()
                assert response.status == 200 and response.read() == body
            finally:
                connection.close()
    finally:
        second.stop()
        first.stop()


def test_static_server_denies_listing_traversal_and_http_bridge(tmp_path: Path) -> None:
    root = tmp_path / "frontend"
    root.mkdir()
    (root / "index.html").write_text("synthetic frontend", encoding="utf-8")
    (root / "folder").mkdir()
    (tmp_path / "outside.txt").write_text("synthetic outside file", encoding="utf-8")
    server = StaticOnlyServer(root, None)
    try:
        assert server.httpd.server_address[0] == "127.0.0.1"
        for method, path, expected in (
            ("GET", "/", 200),
            ("GET", "/folder/", 403),
            ("GET", "/../outside.txt", 404),
            ("POST", "/js_api/uid", 501),
        ):
            connection = HTTPConnection("127.0.0.1", server.port, timeout=1)
            try:
                connection.request(method, path)
                response = connection.getresponse()
                assert response.status == expected
                assert response.getheader("Cache-Control") == "no-store"
                body = response.read()
                assert b"synthetic outside file" not in body
                if path == "/":
                    assert body == b"synthetic frontend"
            finally:
                connection.close()
    finally:
        server.stop()
    assert not server.is_running and not server.thread.is_alive()


def test_cached_entry_is_refetched_with_query_and_old_conditional_headers(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text("updated frontend", encoding="utf-8")
    _, _, server = StaticOnlyServer.start_server([str(index) + "?build=current#?theme=dark"], None)
    try:
        connection = HTTPConnection("127.0.0.1", server.port, timeout=1)
        try:
            connection.request(
                "GET",
                "/index.html?build=current",
                headers={"If-Modified-Since": "Thu, 01 Jan 2099 00:00:00 GMT", "If-None-Match": "old"},
            )
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("Cache-Control") == "no-store"
            assert response.read() == b"updated frontend"
        finally:
            connection.close()
    finally:
        server.stop()
