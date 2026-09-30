"""The console's settings form: drawn from Config's own types, and saved
into config.yaml without disturbing anything it did not change."""

import re

import pytest

from arnold import config_edit, config_schema
from arnold.config import Config

FILE = """\
# arnold configuration
device:
  id: desk   # the name HA knows it by

game:
  power_plan: high
  close_apps:
    - OneDrive.exe
    - Widgets.exe

log_level: INFO
"""


def _fields():
    return {f["path"]: f for s in config_schema.schema() for f in s["fields"]}


def test_every_setting_is_in_the_form():
    fields = _fields()
    for name in ("device.id", "game.power_plan", "jarvis.home_assistant.token", "log_level", "alerts"):
        assert name in fields
    assert "source_path" not in fields and "active_profile" not in fields


def test_controls_follow_the_types():
    f = _fields()
    assert f["game.boost"]["type"] == "bool"
    assert f["game.scan_seconds"]["type"] == "float"
    assert f["game.close_apps"]["type"] == "list"
    assert f["alerts"]["type"] == "yaml"
    assert f["game.game_priority"]["choices"] == ["", "high", "above_normal"]
    assert f["game.power_plan"]["open"] is True  # a plan's name is allowed too
    assert f["mqtt.password"].get("secret") and f["jarvis.home_assistant.token"].get("secret")


def test_help_comes_from_the_comments():
    assert "Razer" not in _fields()["game.power_plan"]["help"]
    assert "powercfg" in _fields()["game.power_plan"]["help"]


def test_choices_name_real_fields():
    fields = _fields()
    for path in config_schema.CHOICES:
        assert path in fields, path
        default = fields[path]["default"]
        assert default in config_schema.CHOICES[path] or path in config_schema.OPEN_CHOICES, path


def test_profiles_become_the_profile_picker():
    sections = config_schema.schema(["arnold", "jarvis"])
    field = next(f for s in sections for f in s["fields"] if f["path"] == "assistant.profile")
    assert field["choices"] == ["", "arnold", "jarvis"]


def test_values_are_what_the_file_says():
    sections = config_schema.schema()
    values = config_schema.values(config_edit.raw_values(FILE), sections)
    assert values["device.id"] == "desk"
    assert values["game.close_apps"] == ["OneDrive.exe", "Widgets.exe"]
    assert "game.boost" not in values  # not in the file: the default applies


def test_changes_keep_comments_and_everything_else():
    new = config_edit.apply_changes(FILE, {"device.id": "tower", "game.close_apps": ["Teams.exe"]})
    assert re.search(r"id: tower +# the name HA knows it by", new)
    assert "# arnold configuration" in new
    assert "    - Teams.exe" in new and "Widgets" not in new  # block style kept
    restored = re.sub(r"id: tower +#", "id: desk   #", new)
    assert restored.replace("    - Teams.exe\n", "    - OneDrive.exe\n    - Widgets.exe\n") == FILE


def test_none_removes_the_key_and_new_sections_are_added():
    new = config_edit.apply_changes(FILE, {"game.power_plan": None, "face.style": "orb"})
    assert "power_plan" not in new
    assert new.endswith("log_level: INFO\n\nface:\n  style: orb\n")


@pytest.mark.parametrize("path,value,expected", [
    ("game.boost", False, False),
    ("game.scan_seconds", "2.5", 2.5),
    ("game.scan_seconds", "", None),
    ("ui.port", 8771.0, 8771),
    ("game.close_apps", [" a.exe ", ""], ["a.exe"]),
    ("game.power_plan", "Razer Cortex Power Plan", "Razer Cortex Power Plan"),
])
def test_coercion(path, value, expected):
    assert config_edit.coerce(_fields()[path], value) == expected


@pytest.mark.parametrize("path,value", [
    ("game.boost", "yes"),
    ("ui.port", "abc"),
    ("ui.port", 1.5),
    ("game.game_priority", "realtime"),
    ("alerts", []),
])
def test_coercion_refuses(path, value):
    with pytest.raises(ValueError):
        config_edit.coerce(_fields()[path], value)
