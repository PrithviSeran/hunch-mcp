"""Importing the SDK must not load pyobjc. Native calls fail clearly off macOS."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), os.pardir, "src")


def _python(code):
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)


def test_importing_hunch_does_not_load_pyobjc():
    code = (
        "import sys\n"
        "from hunch import Hunch\n"
        "from hunch.errors import UnsupportedPlatform\n"
        "heavy = [m for m in ('AppKit', 'objc', 'ApplicationServices', 'Quartz') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
        "assert UnsupportedPlatform\n"
    )
    result = _python(code)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform == "darwin", reason="native session is constructed on macOS")
def test_linux_hunch_constructs_and_native_calls_fail():
    code = (
        "from hunch import Hunch\n"
        "from hunch.errors import UnsupportedPlatform\n"
        "mac = Hunch(confirm='off', check_permissions=False)\n"
        "try:\n"
        "    mac.snapshot()\n"
        "except UnsupportedPlatform as exc:\n"
        "    assert 'snapshot needs macOS' in str(exc)\n"
        "else:\n"
        "    raise SystemExit('native call did not fail')\n"
        "try:\n"
        "    mac.files.trash(['/tmp/x'])\n"
        "except UnsupportedPlatform as exc:\n"
        "    assert 'files.trash needs macOS' in str(exc)\n"
        "else:\n"
        "    raise SystemExit('files did not fail')\n"
    )
    result = _python(code)
    assert result.returncode == 0, result.stderr


def test_dialog_confirm_fails_closed_off_macos(monkeypatch):
    from hunch import gate
    from hunch.errors import UnsupportedPlatform
    monkeypatch.setattr(gate.sys, "platform", "linux")
    with pytest.raises(UnsupportedPlatform, match="confirm='dialog' needs macOS"):
        gate.Gate(confirm="dialog").confirm_dialog("allow this?")
