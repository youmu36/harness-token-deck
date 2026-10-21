"""Suspend result races and failures are observable without a WebView runtime."""

# pyright: reportPrivateUsage=false

from enum import Enum
from typing import Any

import pytest
from htd.adapters.desktop.webview2 import WebViewPower


class Task:
    IsCompleted = False
    Result = True


class Control:
    def __init__(self) -> None:
        self.CoreWebView2: Any = self
        self.task = Task()
        self.resumes = 0
        self.requests = 0
        self.visible = True

    def Hide(self) -> None:
        self.visible = False

    def Show(self) -> None:
        self.visible = True

    def TrySuspendAsync(self) -> Task:
        self.requests += 1
        return self.task

    def Resume(self) -> None:
        self.resumes += 1


def test_startup_reclaims_only_owned_processes_once_without_hiding_or_changing_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    now = [0.0]
    reclaimed: list[int] = []
    monkeypatch.setattr("htd.adapters.desktop.webview2.time.monotonic", lambda: now[0])
    monkeypatch.setattr("htd.adapters.desktop.webview2.reclaim_working_set", reclaimed.append)
    control: Any = Control()
    control.Environment = SimpleNamespace(GetProcessInfos=lambda: [SimpleNamespace(ProcessId=123)])
    power = WebViewPower(control)
    power.startup_ready()
    power.startup_ready()
    power.poll()
    assert reclaimed == []
    now[0] = 2
    power.poll()
    power.startup_ready()
    power.poll()
    assert reclaimed == [123] and control.visible
    assert control.requests == control.resumes == 0 and not power._background


def test_hiding_before_startup_reclamation_cancels_the_visible_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    reclaimed: list[int] = []
    monkeypatch.setattr("htd.adapters.desktop.webview2.reclaim_working_set", reclaimed.append)
    power = WebViewPower(Control())
    power.startup_ready()
    power.hide()
    power.restore()
    power.startup_ready()
    assert power._startup_at is None and reclaimed == []


def test_suspend_and_resume_own_the_memory_target_transition() -> None:
    class Target(Enum):
        Normal = 0
        Low = 1

    class ResettingControl(Control):
        def TrySuspendAsync(self) -> Task:
            self.MemoryUsageTargetLevel = Target.Low
            return super().TrySuspendAsync()

        def Resume(self) -> None:
            super().Resume()
            self.MemoryUsageTargetLevel = Target.Normal

    control: Any = ResettingControl()
    control.MemoryUsageTargetLevel = Target.Normal
    power = WebViewPower(control)
    power.configure()
    assert control.MemoryUsageTargetLevel is Target.Normal
    assert power._task is None and control.visible
    power.hide()
    power._retry_at = 0
    power.poll()
    assert control.MemoryUsageTargetLevel is Target.Low
    control.task.IsCompleted = True
    power.poll()
    power.configure()
    assert control.MemoryUsageTargetLevel is Target.Low
    power.restore()
    assert control.MemoryUsageTargetLevel is Target.Normal


def test_restore_during_suspend_resumes_again_on_task_completion() -> None:
    control = Control()
    power = WebViewPower(control)
    power.hide()
    power._retry_at = 0
    power.poll()
    assert not control.visible
    power.restore()
    assert control.visible and control.resumes == 1
    control.task.IsCompleted = True
    power.poll()
    assert control.resumes == 2
    power.poll()
    assert control.resumes == 2


def test_background_process_priority_is_restored_before_the_visible_webview_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    events: list[str] = []

    class Process:
        def __init__(self, pid: int) -> None:
            events.append(f"background:{pid}")

        def restore(self) -> None:
            events.append("foreground")

    class Ordered(Control):
        def Resume(self) -> None:
            assert events[-1] == "foreground"
            super().Resume()

    control = Ordered()
    control.Environment = SimpleNamespace(GetProcessInfos=lambda: [SimpleNamespace(ProcessId=123)])  # type: ignore[attr-defined]
    monkeypatch.setattr("htd.adapters.desktop.webview2.BackgroundProcess", Process)
    power = WebViewPower(control)
    power.hide()
    assert events == ["background:123"]
    power.restore()
    assert events == ["background:123", "foreground"]


def test_suspend_refusal_and_exception_only_log(caplog: pytest.LogCaptureFixture) -> None:
    control = Control()
    power = WebViewPower(control)
    power.hide()
    control.task.IsCompleted = True
    control.task.Result = False
    power._retry_at = 0
    power.poll()
    assert "拒绝挂起" in caplog.text

    class Broken(Control):
        def TrySuspendAsync(self) -> Task:
            raise RuntimeError("suspend unavailable")

    control = Broken()
    power = WebViewPower(control)
    power.hide()
    power._retry_at = 0
    power.poll()
    assert not control.visible and "挂起请求失败" in caplog.text


def test_hide_defers_suspend_until_native_visibility_has_settled_and_restore_cancels_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("htd.adapters.desktop.webview2.time.monotonic", lambda: now[0])
    control = Control()
    power = WebViewPower(control)
    power.hide()
    power.poll()
    assert not control.visible and control.requests == 0
    now[0] = 0.5
    power.poll()
    assert control.requests == 1
    power.restore()
    control.task.IsCompleted = True
    power.poll()
    power.hide()
    power.restore()
    now[0] += 1
    power.poll()
    assert control.requests == 1 and control.visible


def test_late_gpu_and_browser_processes_use_background_settings_and_unsubscribe_on_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    class Event:
        handler: Any = None

        def __iadd__(self, handler: Any) -> "Event":
            self.handler = handler
            return self

        def __isub__(self, handler: Any) -> "Event":
            assert self.handler == handler
            self.handler = None
            return self

    changed = Event()
    pids = [123]
    lowered: list[int] = []
    restored: list[int] = []

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            lowered.append(pid)

        def restore(self) -> None:
            restored.append(self.pid)

    control: Any = Control()
    control.Environment = SimpleNamespace(
        ProcessInfosChanged=changed,
        GetProcessInfos=lambda: [SimpleNamespace(ProcessId=pid) for pid in pids],
    )
    monkeypatch.setattr("htd.adapters.desktop.webview2.BackgroundProcess", Process)
    power = WebViewPower(control)
    power.hide()
    assert lowered == [123]
    pids[:] = [123, 456]
    changed.handler(None, None)
    changed.handler(None, None)
    assert lowered == [123, 456]
    pids[:] = [456]
    changed.handler(None, None)
    assert restored == [123] and list(power._background) == [456]
    power.restore()
    assert restored == [123, 456] and changed.handler is None
    assert not power._background and control.visible


def test_transient_suspend_refusal_retries_with_a_bound_and_restore_cancels_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("htd.adapters.desktop.webview2.time.monotonic", lambda: now[0])
    control = Control()
    control.task.IsCompleted = True
    control.task.Result = False
    power = WebViewPower(control)
    power.hide()
    for _ in range(10):
        power.poll()
        now[0] += 0.5
    assert power._suspend_attempts == 3 and power._retry_at is None
    power.hide()
    power.poll()
    assert power._retry_at is not None
    power.restore()
    attempts = power._suspend_attempts
    now[0] += 1
    power.poll()
    assert power._suspend_attempts == attempts and power._retry_at is None


@pytest.mark.parametrize("unavailable", [False, True])
def test_reclaims_only_suspended_environment_processes_once_and_covers_late_processes(
    monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    from types import SimpleNamespace

    pids = [123]
    reclaimed: list[int] = []
    restored: list[int] = []

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def reclaim(self) -> None:
            reclaimed.append(self.pid)
            if unavailable:
                raise OSError("optional quota access unavailable")

        def restore(self) -> None:
            restored.append(self.pid)

    control: Any = Control()
    control.Environment = SimpleNamespace(
        GetProcessInfos=lambda: [SimpleNamespace(ProcessId=pid) for pid in pids]
    )
    monkeypatch.setattr("htd.adapters.desktop.webview2.BackgroundProcess", Process)
    power = WebViewPower(control)
    power.hide()
    power._retry_at = 0
    power.poll()
    assert reclaimed == []
    control.task.IsCompleted = True
    power.poll()
    power._background_processes(control)
    assert reclaimed == [123]
    pids.append(456)
    power._background_processes(control)
    power._background_processes(control)
    assert reclaimed == [123, 456]
    pids.remove(123)
    power._background_processes(control)
    pids.append(123)
    power._background_processes(control)
    assert reclaimed == [123, 456, 123]
    power.restore()
    power.poll()
    assert restored == [123, 456, 123] and not power._reclaimed
    assert reclaimed == [123, 456, 123] and control.visible
