"""Lossless Scaling: its hotkey read from Settings.xml, AutoScale profiles
respected, and the hotkey pressed only once the game is settled in front."""

from arnold import lossless
from arnold.config import GameConfig

XML = r"""<?xml version="1.0" encoding="utf-8"?>
<Settings>
  <Hotkey>S</Hotkey>
  <HotkeyModifierKeys>Alt Control</HotkeyModifierKeys>
  <GameProfiles>
    <Profile><Title>Default</Title><AutoScale>false</AutoScale></Profile>
    <Profile>
      <Title>D2</Title>
      <Path>C:\Games\Destiny 2\destiny2.exe</Path>
      <AutoScale>true</AutoScale>
    </Profile>
  </GameProfiles>
</Settings>
"""


def test_key_names():
    assert lossless.key_code("S") == ord("S")
    assert lossless.key_code("D5") == 0x35
    assert lossless.key_code("F12") == 0x7B
    assert lossless.key_code("NumPad3") == 0x63
    assert lossless.key_code("Home") == 0x24
    assert lossless.key_code("Mystery") is None


def test_settings_hotkey_and_auto_profiles(tmp_path):
    path = tmp_path / "Settings.xml"
    path.write_text(XML, encoding="utf-8")
    s = lossless.read_settings(path)
    assert s.key == ord("S")
    assert s.modifiers == [0x12, 0x11]
    assert s.hotkey_text == "Alt+Control+S"
    assert s.auto_paths == {__import__("os").path.normcase(r"C:\Games\Destiny 2\destiny2.exe")}


def test_unreadable_settings(tmp_path):
    assert lossless.read_settings(tmp_path / "missing.xml") is None


def _clock():
    t = [0.0]
    return t, (lambda: t[0]), (lambda dt: t.__setitem__(0, t[0] + dt))


def test_waits_for_the_window_to_settle():
    t, clock, sleep = _clock()
    # Splash in front for 3s, then the game's window.
    fg = lambda: 99 if t[0] < 3 else 42
    assert lossless.wait_in_front(42, 5, 60, fg=fg, alive=lambda p: True, clock=clock, sleep=sleep)
    assert 8 <= t[0] <= 9


def test_alt_tab_restarts_the_settle():
    t, clock, sleep = _clock()
    fg = lambda: 99 if 4 <= t[0] < 6 else 42
    assert lossless.wait_in_front(42, 5, 60, fg=fg, alive=lambda p: True, clock=clock, sleep=sleep)
    assert t[0] >= 11


def test_gives_up_when_the_game_ends_or_never_shows():
    t, clock, sleep = _clock()
    assert not lossless.wait_in_front(42, 5, 30, fg=lambda: 99, alive=lambda p: True, clock=clock, sleep=sleep)
    t, clock, sleep = _clock()
    assert not lossless.wait_in_front(42, 5, 30, fg=lambda: 42, alive=lambda p: t[0] < 2, clock=clock, sleep=sleep)


_real = lossless.read_settings


def _scaler(monkeypatch, tmp_path, active=None, **game):
    """A Scaler with the desktop faked. `active` is a list of answers to
    scaling_active(), in order; by default a press always takes."""
    path = tmp_path / "Settings.xml"
    path.write_text(XML, encoding="utf-8")
    pressed = []
    answers = list(active) if active is not None else None
    state = {"on": False}

    def is_active():
        if answers is not None:
            return answers.pop(0) if answers else False
        return state["on"]

    def press(key, mods):
        pressed.append((key, mods))
        state["on"] = True

    monkeypatch.setattr(lossless, "read_settings", lambda p=path: _real(path))
    monkeypatch.setattr(lossless, "find_exe", lambda configured="": tmp_path / "LosslessScaling.exe")
    monkeypatch.setattr(lossless, "running", lambda: object())
    monkeypatch.setattr(lossless, "age", lambda proc: 60.0)
    monkeypatch.setattr(lossless, "wait_in_front", lambda *a, **k: True)
    monkeypatch.setattr(lossless, "foreground_pid", lambda: 1)
    monkeypatch.setattr(lossless, "scaling_active", is_active)
    monkeypatch.setattr(lossless, "press", press)
    scaler = lossless.Scaler(GameConfig(**game))
    return scaler, pressed, state


HADES = r"C:\Games\Hades\Hades.exe"
NOSLEEP = {"sleep": lambda s: None}


def test_hotkey_pressed_once_per_session(monkeypatch, tmp_path):
    scaler, pressed, state = _scaler(monkeypatch, tmp_path)
    assert "scaling" in scaler.activate(1, "Hades.exe", HADES, **NOSLEEP)
    state["on"] = False  # e.g. the player turned it off
    assert "already" in scaler.activate(2, "Hades.exe", HADES, **NOSLEEP)
    assert len(pressed) == 1
    scaler.reset()
    scaler.activate(3, "Hades.exe", HADES, **NOSLEEP)
    assert len(pressed) == 2


def test_never_pressed_while_already_scaling(monkeypatch, tmp_path):
    scaler, pressed, state = _scaler(monkeypatch, tmp_path)
    state["on"] = True
    assert "is scaling" in scaler.activate(1, "Hades.exe", HADES, **NOSLEEP)
    assert pressed == []


def test_a_press_that_did_not_take_is_retried(monkeypatch, tmp_path):
    # before press 1: off; after: off (missed). before press 2: off; after: on; 10s later: on.
    scaler, pressed, _ = _scaler(monkeypatch, tmp_path, active=[False, False, False, True, True])
    assert "scaling" in scaler.activate(1, "Hades.exe", HADES, **NOSLEEP)
    assert len(pressed) == 2


def test_scaling_dropped_by_a_window_reset_is_pressed_again(monkeypatch, tmp_path):
    # press 1 takes, then is gone 10s later; press 2 sticks.
    scaler, pressed, _ = _scaler(monkeypatch, tmp_path, active=[False, True, False, False, True, True])
    monkeypatch.setattr(lossless, "_belongs_to", lambda fg, pid: True)
    assert "scaling" in scaler.activate(1, "Hades.exe", HADES, **NOSLEEP)
    assert len(pressed) == 2


def test_gives_up_after_three_presses(monkeypatch, tmp_path):
    scaler, pressed, _ = _scaler(monkeypatch, tmp_path, active=[])
    assert "never scaled" in scaler.activate(1, "Hades.exe", HADES, **NOSLEEP)
    assert len(pressed) == 3


def test_a_fresh_start_waits_for_it_to_load(monkeypatch, tmp_path):
    scaler, pressed, _ = _scaler(monkeypatch, tmp_path)
    monkeypatch.setattr(lossless, "age", lambda proc: 2.0)
    slept = []
    scaler.activate(1, "Hades.exe", HADES, sleep=slept.append)
    assert 8.0 in slept  # warm-up 10s, 2s already gone


def test_auto_scale_profile_is_left_alone(monkeypatch, tmp_path):
    scaler, pressed, state = _scaler(monkeypatch, tmp_path)
    state["on"] = True  # its profile kicked in
    message = scaler.activate(1, "destiny2.exe", r"c:\games\destiny 2\DESTINY2.EXE", **NOSLEEP)
    assert "is scaling" in message and pressed == []


def test_scaling_games_limits_which_games(monkeypatch, tmp_path):
    scaler, _, _ = _scaler(monkeypatch, tmp_path, scaling_games=["cyberpunk*.exe"])
    assert scaler.wanted("Cyberpunk2077.exe")
    assert not scaler.wanted("Hades.exe")


def test_lossless_scaling_itself_is_not_a_game():
    from arnold import booster

    exe = r"C:\Program Files (x86)\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe"
    assert not booster.is_game("LosslessScaling.exe", exe, GameConfig())
