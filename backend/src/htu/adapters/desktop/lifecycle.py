"""GUI-independent transitions; adapters report hide success before cancelling close."""

import logging
from typing import Literal
from uuid import uuid4

type State = Literal["starting", "visible", "hidden", "minimized", "closing", "stopped", "failed"]
type Event = Literal[
    "started",
    "activate",
    "close_requested",
    "hidden",
    "hide_failed",
    "minimize",
    "restored",
    "focused",
    "deactivated",
    "quit",
    "stopped",
    "webview_failed",
    "retry",
]
type Action = Literal["restore", "focus", "hide", "visibility", "shutdown", "error", "retry"]
_LOG = logging.getLogger(__name__)


class DesktopLifecycle:
    def __init__(self) -> None:
        self.state: State = "starting"
        self.focused = False
        self._activate_pending = False
        self._hiding = False

    def handle(self, event: Event) -> list[Action]:
        before = self.state
        actions = self._transition(event)
        if before != self.state:
            _LOG.info("lifecycle %s -> %s event=%s id=%s", before, self.state, event, uuid4().hex)
        return actions

    def _transition(self, event: Event) -> list[Action]:
        if self.state == "stopped":
            return []
        if event == "stopped":
            self.state = "stopped"
            return []
        if self.state == "closing":
            return []
        if event in {"quit", "hide_failed"}:
            self.state = "closing"
            return ["shutdown"]
        if event == "webview_failed":
            if self.state == "failed":
                return []
            self.state = "failed"
            return ["error"]
        if self.state == "failed":
            if event == "retry":
                self.state = "starting"
                self._activate_pending = True
                self._hiding = False
                return ["retry"]
            return []
        if event == "started" and self.state == "starting":
            self.state = "visible"
            pending, self._activate_pending = self._activate_pending, False
            return ["restore"] if pending else ["visibility"]
        if event == "activate":
            if self.state == "starting":
                self._activate_pending = True
                return []
            if self.state in {"hidden", "minimized"}:
                self.state, self.focused = "visible", True
                return ["restore"]
            if not self.focused:
                self.focused = True
                return ["focus"]
        if event == "close_requested" and self.state != "hidden" and not self._hiding:
            self._hiding = True
            return ["hide"]
        if event == "hidden" and self._hiding:
            self._hiding = False
            self.state, self.focused = "hidden", False
        elif event == "minimize" and self.state == "visible":
            self.state, self.focused = "minimized", False
        elif event == "restored" and self.state == "minimized":
            self.state = "visible"
        elif event in {"focused", "deactivated"}:
            self.focused = event == "focused"
        return []
