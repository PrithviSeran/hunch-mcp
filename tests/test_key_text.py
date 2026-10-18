"""A pressed key carries the text it types, as a keyboard's does, unless it is a shortcut."""
import pytest

from hunch import cdp


def press(monkeypatch, key, modifiers=None):
    value = cdp.CDPSession.__new__(cdp.CDPSession)
    calls = []
    monkeypatch.setattr(value, "_cmd", lambda method, params=None: calls.append(params) or {})
    value.press_key(key, modifiers)
    return calls


@pytest.mark.parametrize("key, text", [("Enter", "\r"), ("return", "\r"), ("a", "a"), ("space", " ")])
def test_a_typing_key_carries_its_text(monkeypatch, key, text):
    down, up = press(monkeypatch, key)
    assert (down["type"], down["text"], down["unmodifiedText"]) == ("keyDown", text, text)
    assert up["type"] == "keyUp" and "text" not in up


@pytest.mark.parametrize("key, mods", [("a", ["cmd"]), ("Enter", ["ctrl"]), ("z", ["meta", "shift"])])
def test_a_shortcut_types_nothing(monkeypatch, key, mods):
    down, _ = press(monkeypatch, key, mods)
    assert down["type"] == "rawKeyDown" and "text" not in down


def test_shift_with_a_character_still_types_it(monkeypatch):
    down, _ = press(monkeypatch, "A", ["shift"])
    assert (down["type"], down["text"], down["modifiers"]) == ("keyDown", "A", 8)


@pytest.mark.parametrize("key", ["Tab", "Escape", "down", "backspace"])
def test_a_key_that_types_nothing_has_no_text(monkeypatch, key):
    down, _ = press(monkeypatch, key)
    assert down["type"] == "keyDown" and "text" not in down
