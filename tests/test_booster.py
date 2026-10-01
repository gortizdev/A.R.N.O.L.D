"""Game boost: power plan choice, game recognition, the launch watcher, and
that restore puts back exactly what apply changed."""

from types import SimpleNamespace

from arnold import booster
from arnold.config import GameConfig

PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
    "c79a2775-fb30-4cd8-b7f0-544b39fc1a2e": "Razer Cortex Power Plan",
}


def test_plan_aliases_names_and_guids():
    assert booster.resolve_plan("high", PLANS) == "SCHEME_MIN"
    assert booster.resolve_plan("Razer Cortex Power Plan", PLANS) == "c79a2775-fb30-4cd8-b7f0-544b39fc1a2e"
    assert booster.resolve_plan("8C5E7FDA-E8BF-4A96-9A85-A6E23A8C635C", PLANS) == "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
    assert booster.resolve_plan("nonsense", PLANS) is None
    assert booster.resolve_plan("", PLANS) is None


def test_ultimate_falls_back_when_not_installed():
    assert booster.resolve_plan("ultimate", PLANS) == "SCHEME_MIN"
    with_ultimate = dict(PLANS, **{booster._ULTIMATE: "Ultimate Performance"})
    assert booster.resolve_plan("ultimate", with_ultimate) == booster._ULTIMATE


def test_games_are_recognised_by_folder_and_name():
    game = GameConfig(games=["valorant*.exe"])
    steam = r"D:\SteamLibrary\steamapps\common\Hades\x64\Hades.exe"
    assert booster.is_game("Hades.exe", steam, game)
    assert booster.is_game("VALORANT-Win64-Shipping.exe", r"C:\x\y.exe", game)
    assert not booster.is_game("notepad.exe", r"C:\Windows\notepad.exe", game)


def test_wallpaper_engine_and_crash_handlers_are_not_games():
    game = GameConfig()
    root = r"C:\Program Files (x86)\Steam\steamapps\common"
    assert not booster.is_game("wallpaper64.exe", root + r"\wallpaper_engine\wallpaper64.exe", game)
    assert not booster.is_game("UnityCrashHandler64.exe", root + r"\Foo\UnityCrashHandler64.exe", game)


def test_store_clients_are_not_games_but_their_games_are():
    game = GameConfig()
    # Left running after Fortnite closed, these held the boost for hours.
    for name, exe in [
        ("EpicWebHelper.exe", r"C:\Program Files\Epic Games\Launcher\Engine\Binaries\Win64\EpicWebHelper.exe"),
        ("EpicOnlineServicesUserHelper.exe",
         r"C:\Program Files (x86)\Epic Games\Epic Online Services\EpicOnlineServicesUserHelper.exe"),
        ("EOSBootStrapper.exe", r"C:\Program Files (x86)\Epic Games\Epic Online Services\EOSBootStrapper.exe"),
        ("RiotClientServices.exe", r"C:\Riot Games\Riot Client\RiotClientServices.exe"),
    ]:
        assert not booster.is_game(name, exe, game), name
    fortnite = r"C:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64\FortniteClient-Win64-Shipping.exe"
    assert booster.is_game("FortniteClient-Win64-Shipping.exe", fortnite, game)
    assert booster.is_game("VALORANT.exe", r"C:\Riot Games\VALORANT\live\VALORANT.exe", game)
    # A game named outright is one wherever it lives.
    named = GameConfig(games=["EpicWebHelper.exe"])
    assert booster.is_game("EpicWebHelper.exe", r"C:\Program Files\Epic Games\Launcher\EpicWebHelper.exe", named)


def _watcher(procs, clock, grace=20.0):
    events = []
    game = GameConfig(games=["game.exe"], exit_grace_seconds=grace)
    w = booster.GameWatcher(
        game,
        started=lambda pid, name, exe: events.append(("start", pid)),
        ended=lambda: events.append(("end",)),
        pids=lambda: list(procs),
        describe=lambda pid: (procs[pid], ""),
        clock=lambda: clock[0],
    )
    return w, events


def test_watcher_boosts_on_launch_and_waits_out_the_grace():
    procs = {1: "explorer.exe"}
    clock = [0.0]
    w, events = _watcher(procs, clock)
    w.scan()
    assert events == []
    procs[2] = "game.exe"
    w.scan()
    assert events == [("start", 2)]
    del procs[2]
    w.scan()
    clock[0] = 10
    w.scan()
    assert events == [("start", 2)]  # still inside the grace
    procs[3] = "game.exe"  # the launcher restarted it
    w.scan()
    del procs[3]
    clock[0] = 15
    w.scan()
    clock[0] = 36
    w.scan()
    assert events == [("start", 2), ("start", 3), ("end",)]


def test_watcher_ends_a_leftover_boost_when_no_game_runs():
    procs = {1: "explorer.exe"}
    clock = [0.0]
    w, events = _watcher(procs, clock)
    w.active = True
    w.scan()
    clock[0] = 25
    w.scan()
    assert events == [("end",)]


def _config(tmp_path, **game):
    return SimpleNamespace(state_file=str(tmp_path / "state.json"), game=GameConfig(**game))


def test_apply_then_restore_round_trips(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(booster, "list_plans", lambda: PLANS)
    monkeypatch.setattr(booster, "active_plan", lambda: "381b4222-f694-41f0-9685-ff5bb260df2e")
    monkeypatch.setattr(booster, "set_plan", lambda plan: calls.append(("plan", plan)) or True)
    monkeypatch.setattr(
        booster, "close_apps",
        lambda patterns: [{"name": "OneDrive.exe", "argv": [r"C:\od\OneDrive.exe", "/background"]}],
    )
    monkeypatch.setattr(booster, "trim_memory", lambda skip=None: 512 * 1024 * 1024)
    monkeypatch.setattr(booster, "raise_priority", lambda pid, level: calls.append(("prio", pid)) or True)
    monkeypatch.setattr(booster, "reopen_apps", lambda entries: [e["name"] for e in entries])
    config = _config(tmp_path)

    done = booster.apply(config, game_pid=42)
    assert ("plan", "SCHEME_MIN") in calls and ("prio", 42) in calls
    assert any("OneDrive" in line for line in done)
    assert booster.read_boost(config)["power_plan"] == "381b4222-f694-41f0-9685-ff5bb260df2e"

    # A second boost must not overwrite what is to be restored.
    monkeypatch.setattr(booster, "active_plan", lambda: "SCHEME_MIN")
    booster.apply(config)
    assert booster.read_boost(config)["power_plan"] == "381b4222-f694-41f0-9685-ff5bb260df2e"

    done = booster.restore(config)
    assert calls[-1] == ("plan", "381b4222-f694-41f0-9685-ff5bb260df2e")
    assert "started OneDrive.exe" in done
    assert booster.read_boost(config) is None
    assert booster.restore(config) == []


def test_blank_settings_touch_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not be called")

    for name in ("list_plans", "set_plan", "close_apps", "trim_memory", "raise_priority"):
        monkeypatch.setattr(booster, name, boom)
    config = _config(tmp_path, power_plan="", close_apps=[], trim_memory=False, game_priority="")
    assert booster.apply(config, game_pid=7) == []
