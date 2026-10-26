"""The local 2D UI uses software rendering; explicit runtime choices take precedence."""

from htd.adapters.desktop.host import configure_rendering


def test_default_rendering_avoids_gpu_caches_and_is_idempotent() -> None:
    environment: dict[str, str] = {}
    configure_rendering(environment)
    configure_rendering(environment)
    assert environment == {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--disable-gpu"}


def test_explicit_webview_arguments_are_preserved() -> None:
    environment = {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--use-angle=d3d11"}
    configure_rendering(environment)
    assert environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == "--use-angle=d3d11"


def test_hardware_override_leaves_the_runtime_default_intact() -> None:
    environment = {"HTD_WEBVIEW_RENDERING": "hardware"}
    configure_rendering(environment)
    assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" not in environment
