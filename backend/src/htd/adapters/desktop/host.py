"""pywebview entry point; lifecycle operations are owned by the WinForms GUI thread."""

import hashlib
import importlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from htd.adapters.desktop._win32 import enable_per_monitor_dpi
from htd.adapters.desktop.controller import DesktopController, WindowStore
from htd.adapters.desktop.evaluation import evaluation_worker
from htd.adapters.desktop.single_instance import SingleInstance
from htd.adapters.desktop.startup import set_startup
from htd.adapters.desktop.static_server import StaticOnlyServer
from htd.adapters.desktop.tray import icon_path
from htd.adapters.sqlite.window_state import WindowGeometry
from htd.ipc.bridge import Bridge

_LOG = logging.getLogger(__name__)


def frontend_url(*, debug: bool) -> str:
    configured = os.environ.get("HTD_DEV_URL") if debug else None
    if configured:
        url = urlsplit(configured)
        if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTD_DEV_URL 必须是本机 HTTP 开发服务器")
        return configured
    index = Path(__file__).resolve().parents[2] / "web/index.html"
    if not index.is_file():
        raise FileNotFoundError("缺少前端构建，请先运行 npm run build")
    # Persistent WebView2 profiles can reuse an older index at the fixed loopback origin.
    # Include the content identity even when the application version is unchanged.
    build = hashlib.sha256(index.read_bytes()).hexdigest()[:16]
    return str(index) + "?build=" + build


def run(
    bridge: Bridge,
    instance: SingleInstance,
    directory: Path,
    *,
    debug: bool,
    shutdown: Callable[[WindowGeometry | None], None],
    window_store: WindowStore,
    release_memory: Callable[[], None] = lambda: None,
) -> None:
    url = frontend_url(debug=debug)
    snapshot = bridge.call("preferences.get", {})
    data = snapshot.get("data")
    preferences = data.get("preferences") if isinstance(data, dict) else None
    if isinstance(preferences, dict) and preferences.get("startAtLogin") is True:
        try:
            set_startup(True)
        except OSError:
            _LOG.warning("无法更新开机自启的程序路径")
    theme = preferences.get("theme", "system") if isinstance(preferences, dict) else "system"
    fragment = urlsplit(url).fragment if url.startswith("http") else url.partition("#")[2]
    route, _, query = fragment.partition("?")
    if not route and isinstance(preferences, dict):
        remembered = preferences.get("lastRoute")
        if isinstance(remembered, str):
            from htd.adapters.desktop.controller import page_from_url

            if page_from_url("http://localhost/#" + remembered):
                route = remembered
    boot = urlencode({"theme": str(theme), "logPath": str(directory / "logs/htd.log")})
    url = url.partition("#")[0] + "#" + route + "?" + (query + "&" if query else "") + boot
    enable_per_monitor_dpi()
    _LOG.info("加载桌面组件")
    webview: Any = importlib.import_module("webview")
    window: Any = webview.create_window(
        "Harness Token Deck", url, js_api=bridge, width=1280, height=820, min_size=(1024, 680)
    )
    controller = DesktopController(
        window,
        bridge,
        instance,
        directory,
        shutdown=shutdown,
        window_store=window_store,
        release_memory=release_memory,
    )
    try:
        _LOG.info("启动 WebView2 窗口")
        with evaluation_worker(window):
            webview.start(
                gui="edgechromium",
                debug=debug,
                server=StaticOnlyServer,
                private_mode=False,
                storage_path=str(directory / "webview"),
                icon=str(icon_path()),
            )
    finally:
        controller.close()
        server: Any = webview.http.global_server
        if isinstance(server, StaticOnlyServer):
            server.stop()
