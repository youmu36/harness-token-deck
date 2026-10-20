"""Desktop startup arbitration plus smoke and persistent windowless contract modes."""

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from htu.adapters.desktop._win32 import show_error
from htu.adapters.desktop.host import run as run_host
from htu.adapters.desktop.installer import shortcut_app_id
from htu.adapters.desktop.logging_setup import logging_session
from htu.adapters.desktop.runtime import DOWNLOAD_URL, webview2_version
from htu.adapters.desktop.single_instance import SingleInstance
from htu.adapters.sqlite.installer_backup import backup_data
from htu.bootstrap import Application, default_data_dir
from htu.ipc.headless import serve

_LOG = logging.getLogger(__name__)
_UNRESPONSIVE = "Harness Token Usage 已在运行但没有响应"


def _instance_options(directory: Path, *, smoke: bool = False) -> dict[str, str]:
    instance_options: dict[str, str] = {}
    installer_root = os.environ.get("HTU_INSTALLER_TEST_ROOT")
    if installer_root:
        root = Path(installer_root).resolve()
        if (root / ".htu-installer-test").is_file() and directory == root / "data":
            suffix = hashlib.sha256(str(directory).casefold().encode()).hexdigest()[:24]
            name = "HarnessTokenUsage.Installer." + suffix
            return {"name": name, "pipe_name": name}
    isolated_desktop = (
        os.environ.get("HTU_TEST_HOOKS") == "1" and os.environ.get("HTU_DESKTOP_ISOLATED") == "1"
    )
    if (smoke and os.environ.get("HTU_SMOKE_ISOLATED") == "1") or isolated_desktop:
        if not os.environ.get("HTU_DATA_DIR"):
            raise ValueError("隔离冒烟必须指定临时数据目录")
        suffix = hashlib.sha256(str(directory).casefold().encode()).hexdigest()[:24]
        name = ("HarnessTokenUsage.Test." if isolated_desktop else "HarnessTokenUsage.Smoke.") + suffix
        instance_options = {"name": name, "pipe_name": name}
    return instance_options


def _desktop(
    directory: Path, *, smoke: bool, quit_requested: bool, debug: bool, wait_for_exit: bool = False
) -> int:
    instance_options = _instance_options(directory, smoke=smoke)
    with SingleInstance(directory, **instance_options) as instance:
        if not instance.primary:
            command = "quit" if quit_requested else "activate"
            forwarded = (
                (
                    instance.forward(command, wait_for_exit=True)
                    if wait_for_exit
                    else instance.forward(command)
                )
                if not smoke
                else False
            )
            if forwarded:
                _LOG.info("第二实例收到 ACK，退出")
                return 0
            _LOG.warning("第二实例没有获得 ACK，未打开数据库")
            message = _UNRESPONSIVE + f"\n日志: {directory / 'logs/htu.log'}"
            if smoke or quit_requested:
                if sys.stderr is not None:
                    sys.stderr.write(message + "\n")
            else:
                show_error(message)
            return 2
        if quit_requested:
            return 0
        if smoke:
            with Application(directory, start_scheduler=False) as application:
                hello = application.bridge.call("app.hello", {"clientProtocol": 1})
                if not hello["ok"]:
                    raise RuntimeError("冒烟握手失败")
                if sys.stdout is not None:
                    sys.stdout.write(json.dumps(hello, ensure_ascii=True) + "\n")
            _LOG.info("冒烟迁移、握手与资源关闭完成")
            return 0

        instance.listen()
        _LOG.info("第一实例开始监听，检查 WebView2")
        runtime = webview2_version()
        if runtime is None:
            show_error(
                f"缺少 Microsoft Edge WebView2 运行时，请下载并安装:\n{DOWNLOAD_URL}\n"
                f"日志: {directory / 'logs/htu.log'}"
            )
            return 1
        _LOG.info("WebView2 运行时 %s，初始化应用", runtime)
        if instance.quitting.is_set():
            return 0
        with Application(directory) as application:
            _LOG.info("应用初始化完成，启动桌面")
            run_host(
                application.bridge,
                instance,
                directory,
                debug=debug,
                shutdown=lambda geometry: application.close(window_geometry=geometry),
                window_store=application.window_state,
                release_memory=application.release_idle_memory,
            )
        _LOG.info("应用资源关闭完成")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless-bridge", action="store_true", help="stdin/stdout JSON-line IPC")
    mode.add_argument("--smoke", action="store_true", help="无窗口迁移、握手与退出")
    mode.add_argument("--quit", action="store_true", help="请求已运行的桌面实例退出")
    mode.add_argument("--backup-data", type=Path, help="安装器: 一致性备份后退出")
    mode.add_argument("--set-shortcut-app-id", type=Path, help="安装器: 设置快捷方式标识后退出")
    parser.add_argument("--wait-for-exit", action="store_true", help="与 --quit 一起等待单实例释放")
    args = parser.parse_args(argv)
    if args.wait_for_exit and not args.quit:
        parser.error("--wait-for-exit requires --quit")
    if args.set_shortcut_app_id:
        try:
            shortcut_app_id(args.set_shortcut_app_id, write=True)
        except OSError:
            return 1
        return 0
    if args.headless_bridge:
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        with Application() as application:
            serve(application.bridge, sys.stdin, sys.stdout)
        return 0
    directory, debug = default_data_dir().resolve(), os.environ.get("HTU_DEBUG") == "1"
    with logging_session(directory, debug=debug) as log_path:
        try:
            if args.backup_data:
                with SingleInstance(directory, **_instance_options(directory)) as instance:
                    if not instance.primary:
                        return 2
                    backup_data(directory, args.backup_data)
                return 0
            result = _desktop(
                directory,
                smoke=args.smoke,
                quit_requested=args.quit,
                debug=debug,
                wait_for_exit=args.wait_for_exit,
            )
            if result == 0 and args.wait_for_exit:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with SingleInstance(directory, **_instance_options(directory)) as instance:
                        if instance.primary:
                            return 0
                    time.sleep(0.1)
                return 2
            return result
        except Exception:
            _LOG.exception("应用启动失败")
            message = f"Harness Token Usage 启动失败，请查看日志:\n{log_path}"
            if args.smoke or args.quit or args.backup_data:
                if sys.stderr is not None:
                    sys.stderr.write(message + "\n")
            else:
                show_error(message)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
