"""Skip macOS-only modules on other platforms before they import pyobjc."""
import sys

# These import pyobjc (or a module that does) at collection time, or they drive
# the native Mac session. Linux CI runs the rest of the suite plus the web smoke.
_MACOS_ONLY = {
    "test_agent.py",
    "test_ax_press.py",
    "test_capability_contracts.py",
    "test_editor_terminal.py",
    "test_recovery_contracts.py",
    "test_release_gaps.py",
    "test_safari_capture.py",
    "test_safari_input.py",
    "test_safari_window_lookup.py",
    "test_sdk_smoke.py",
    "test_smoke.py",
    "test_walk.py",
    "test_window_binding.py",
}

collect_ignore = sorted(_MACOS_ONLY) if sys.platform != "darwin" else []


def pytest_configure(config):
    config.addinivalue_line("markers", "macos: needs macOS and pyobjc")
