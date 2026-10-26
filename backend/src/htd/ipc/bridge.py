"""pywebview js_api surface: call is its only public method."""

from collections.abc import Callable

from pydantic import JsonValue

from htd.ipc.app import SystemActions
from htd.ipc.events import EventForwarder
from htd.ipc.router import Router


class Bridge:
    def __init__(self, router: Router, events: EventForwarder, actions: SystemActions | None = None) -> None:
        self._router = router
        self._events = events
        self._actions = actions

    def _set_system_actions(
        self,
        pick: Callable[[str, str | None], str | None],
        open_path: Callable[[str], None],
        restart: Callable[[], None] | None = None,
    ) -> None:
        if self._actions is not None:
            self._actions.pick, self._actions.open = pick, open_path
            self._actions.restart = restart

    def call(self, command: object, payload: object) -> dict[str, JsonValue]:
        return self._router.call(command, payload)

    def _ready(
        self,
        evaluate_js: Callable[[str], object],
        *,
        dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._events.attach(evaluate_js, dispatch=dispatch)

    def _unready(self) -> None:
        self._events.detach()

    def _set_visible(self, visible: bool) -> None:
        self._events.set_visible(visible)

    def _close(self) -> None:
        self._events.close()
