"""Observe suspend tasks on the GUI timer; an outstanding suspend cannot undo a restore."""

import logging
import time
from typing import Any, cast

from htu.adapters.desktop.process_power import BackgroundProcess, reclaim_working_set

_LOG = logging.getLogger(__name__)


class WebViewPower:
    def __init__(self, control: Any) -> None:
        self.control = control
        self.hidden = False
        self._task: Any = None
        self._background: dict[int, BackgroundProcess] = {}
        self._reclaimed: set[int] = set()
        self._suspended = False
        self._environment: Any = None
        self._suspend_attempts = 0
        self._retry_at: float | None = None
        self._startup_at: float | None = None
        self._startup_reclaimed = False

    def startup_ready(self) -> None:
        if not self._startup_reclaimed and self._startup_at is None and not self.hidden:
            # Wait for first paint and local queries; reuse the existing GUI timer.
            self._startup_at = time.monotonic() + 2

    def _reclaim_startup(self) -> None:
        self._startup_at = None
        self._startup_reclaimed = True
        try:
            core = self.control.CoreWebView2
            if core is not None:
                for process in core.Environment.GetProcessInfos():
                    try:
                        reclaim_working_set(int(process.ProcessId))
                    except OSError:
                        _LOG.debug("WebView2 启动驻留页回收不可用", exc_info=True)
        except Exception:
            _LOG.debug("WebView2 启动进程列表暂不可用", exc_info=True)

    def configure(self) -> None:
        # TrySuspend/Resume own the Low/Normal transition; the SDK advises against
        # mixing suspension with a separately managed Low target.
        core = self.control.CoreWebView2
        if core is not None and not self.hidden:
            self._memory_target(core, "Normal")

    def hide(self) -> None:
        self._startup_at = None
        self._startup_reclaimed = True
        self.hidden = True
        self._suspended = False
        self._reclaimed.clear()
        self._suspend_attempts = 0
        # Let Chromium process native occlusion/blur and release rendering resources
        # before freezing its page tasks. The GUI timer issues the suspend request.
        self._retry_at = time.monotonic() + 0.5
        self.control.Hide()
        try:
            core = self.control.CoreWebView2
            self._watch_processes(core)
            self._background_processes(core)
        except Exception:
            _LOG.exception("WebView2 挂起请求失败，继续隐藏")

    def poll(self) -> None:
        if not self.hidden and self._startup_at is not None and time.monotonic() >= self._startup_at:
            self._reclaim_startup()
        if (
            self.hidden
            and self._task is None
            and self._retry_at is not None
            and time.monotonic() >= self._retry_at
        ):
            self._retry_at = None
            try:
                self._suspend_attempts += 1
                self._task = self.control.CoreWebView2.TrySuspendAsync()
            except Exception:
                _LOG.warning("WebView2 挂起请求失败，继续隐藏", exc_info=True)
                self._retry_suspend()
        task = self._task
        if task is None or not task.IsCompleted:
            return
        self._task = None
        try:
            if not task.Result:
                _LOG.warning("WebView2 拒绝挂起")
                self._retry_suspend()
            elif not self.hidden:
                self.control.CoreWebView2.Resume()
                self.configure()
            else:
                self._retry_at = None
                self._suspended = True
                self._background_processes(self.control.CoreWebView2)
        except Exception:
            _LOG.exception("WebView2 挂起/恢复失败")
            self._retry_suspend()

    def _retry_suspend(self) -> None:
        if self.hidden and self._suspend_attempts < 3:
            self._retry_at = time.monotonic() + 0.5

    def restore(self) -> None:
        self.hidden = False
        self._retry_at = None
        self.close()
        try:
            core = self.control.CoreWebView2
            if core is not None:
                core.Resume()
                self._memory_target(core, "Normal")
        except Exception:
            _LOG.exception("WebView2 恢复失败，继续显示")
        self.control.Show()

    def _watch_processes(self, core: Any) -> None:
        if core is None or self._environment is not None:
            return
        try:
            environment = core.Environment
            environment.ProcessInfosChanged += self._processes_changed
            self._environment = environment
        except (AttributeError, TypeError):
            pass
        except Exception:
            _LOG.debug("WebView2 进程变化通知不可用", exc_info=True)

    def _processes_changed(self, sender: Any, args: Any) -> None:
        if self.hidden and self._environment is not None:
            self._background_processes(self.control.CoreWebView2)

    def _background_processes(self, core: Any) -> None:
        if core is None:
            return
        try:
            processes = core.Environment.GetProcessInfos()
            pids = {int(process.ProcessId) for process in processes}
            for pid in self._background.keys() - pids:
                background = self._background.pop(pid)
                self._reclaimed.discard(pid)
                try:
                    background.restore()
                except OSError:
                    _LOG.debug("WebView2 后台进程已退出", exc_info=True)
            for process in processes:
                pid = int(process.ProcessId)
                if pid in self._background:
                    continue
                try:
                    self._background[pid] = BackgroundProcess(pid)
                except OSError:
                    _LOG.debug("WebView2 进程效率模式不可用", exc_info=True)
            # Windows can reclaim resident pages after minimization, but Hide
            # does not guarantee it. Once suspension succeeds, reclaim this
            # environment's working sets once, including late-starting processes.
            if self._suspended:
                for pid, background in self._background.items():
                    if pid in self._reclaimed:
                        continue
                    self._reclaimed.add(pid)
                    try:
                        background.reclaim()
                    except OSError:
                        _LOG.debug("WebView2 后台驻留页回收不可用", exc_info=True)
        except (AttributeError, TypeError):
            pass
        except Exception:
            _LOG.debug("WebView2 进程列表暂不可用", exc_info=True)

    def close(self) -> None:
        self._startup_at = None
        self._retry_at = None
        self._suspended = False
        self._reclaimed.clear()
        if self._environment is not None:
            try:
                self._environment.ProcessInfosChanged -= self._processes_changed
            except Exception:
                _LOG.debug("WebView2 进程通知已关闭", exc_info=True)
            self._environment = None
        for process in self._background.values():
            try:
                process.restore()
            except OSError:
                _LOG.debug("WebView2 进程已退出", exc_info=True)
        self._background.clear()

    @staticmethod
    def _memory_target(core: Any, name: str) -> None:
        # Older Evergreen runtimes may not expose this optional SDK property.
        try:
            target = core.MemoryUsageTargetLevel
            core.MemoryUsageTargetLevel = getattr(cast(Any, type(target)), name)
        except (AttributeError, TypeError):
            pass
        except Exception:
            _LOG.debug("WebView2 内存目标暂不可用", exc_info=True)
