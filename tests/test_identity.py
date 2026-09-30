"""The PC assistant's own identity, and how it stays distinct from Jarvis.

Two things have to hold. Out of the box, nothing about the PC should be
mistakable for the Pi: different name, voice, colours and MQTT topics. And
flipping `mirror_jarvis` has to bring every one of those back, because that is
the escape hatch for anyone who liked it the old way.
"""

import types

import pytest

from arnold.config import Config
from arnold.face.holo import PALETTES, HoloRenderer, HoloTheme
from arnold.face.state import FaceAnimator, FaceMode
from arnold.voice import session_config
from arnold.voice.session_config import (
    DEFAULT_DELIVERY,
    accent_reminder_for,
    SessionConfig,
    delivery_for,
    load_session_config,
    persona_for,
)
from arnold.voice.tools import ToolDispatcher, build_tools

PI_EXPORT = {
    "model": "gpt-realtime-9",
    "voice": "cedar",
    "instructions": "You are Jarvis, the butler.",
    "tts_voice": "fable",
    "turn_detection": {"type": "server_vad", "silence_duration_ms": 700},
}


@pytest.fixture
def config():
    cfg = Config()
    cfg.device.friendly_name = "the desktop"
    cfg.jarvis.home_assistant.token = "tok"
    cfg.voice.turn_detection = "pi"
    return cfg


@pytest.fixture
def pi_answers(monkeypatch):
    monkeypatch.setattr(session_config, "_fetch_from_pi", lambda ssh: dict(PI_EXPORT))
    monkeypatch.setattr(session_config, "_write_cache", lambda data: None)


class TestNameAndTopics:
    def test_default_is_not_jarvis(self, config):
        assert config.assistant_name() == "Arnold"
        assert config.assistant_topic_prefix() == "arnold/hud"
        assert config.face_palette() == "steel"

    def test_own_topics_never_collide_with_the_pi_bridge(self, config):
        assert config.assistant_topic_prefix() != config.face.jarvis_topic_prefix

    def test_mirroring_brings_everything_back(self, config):
        config.assistant.mirror_jarvis = True
        assert config.assistant_name() == "Jarvis"
        assert config.assistant_topic_prefix() == "jarvis/hud"
        assert config.face_palette() == "gold"

    def test_explicit_settings_win(self, config):
        config.assistant.name = "Athena"
        config.face.topic_prefix = "desk/hud/"
        config.face.palette = "gold"
        assert config.assistant_name() == "Athena"
        assert config.assistant_topic_prefix() == "desk/hud"
        assert config.face_palette() == "gold"

    def test_the_slug_is_topic_safe(self, config):
        config.assistant.name = "Dr. Watson 2"
        assert config.assistant_topic_prefix() == "dr_watson_2/hud"


class TestValidation:
    def test_claiming_the_wake_word_with_a_different_word_is_flagged(self, config):
        config.voice.claim_wake_word = True
        config.voice.wake_word = "hey_mycroft"
        assert any("claim_wake_word" in p for p in config.validate())

    def test_claiming_is_fine_when_both_answer_hey_jarvis(self, config):
        config.voice.claim_wake_word = True
        config.voice.wake_word = "hey_jarvis"
        assert not any("claim_wake_word" in p for p in config.validate())

    def test_claiming_is_fine_when_mirroring(self, config):
        config.voice.claim_wake_word = True
        config.voice.wake_word = "hey_mycroft"
        config.assistant.mirror_jarvis = True
        assert not any("claim_wake_word" in p for p in config.validate())

    def test_an_empty_name_is_flagged(self, config):
        config.assistant.name = "  "
        assert any("assistant.name" in p for p in config.validate())


class TestSession:
    def test_own_identity_over_the_pi_model(self, config, pi_answers):
        session = load_session_config(config)
        assert session.name == "Arnold"
        assert session.voice == "marin"
        assert session.tts_voice == "marin"
        # The model generation still follows the Pi.
        assert session.model == "gpt-realtime-9"
        assert "Jarvis, the butler" not in session.instructions
        assert "Arnold" in session.instructions
        assert "ask_jarvis" in session.instructions
        assert session.source == "pi+local"

    def test_mirroring_is_the_pi_plus_where_it_is_running(self, config, pi_answers):
        """The Pi's character verbatim, with one addendum.

        The Pi's prompt describes timer tools that live in that process and do
        not exist in this session, so mirroring it word for word left the model
        offering timers it could not set. The addendum names the tools that are
        actually here; everything about the character still comes from the Pi.
        """
        config.assistant.mirror_jarvis = True
        session = load_session_config(config)
        assert session.name == "Jarvis"
        assert session.voice == "cedar"
        assert session.instructions.startswith("You are Jarvis, the butler.")
        assert "timer.set" in session.instructions
        assert session.source == "pi"

    def test_a_custom_persona_replaces_the_built_in(self, config):
        config.assistant.persona = "You are Athena. Be brief."
        assert persona_for(config) == "You are Athena. Be brief."

    def test_the_built_in_persona_names_both_machines(self, config):
        config.assistant.name = "Athena"
        text = persona_for(config)
        assert "Athena" in text
        assert "Jarvis" in text
        assert "the desktop" in text

    def test_identity_survives_the_pi_being_down(self, config, monkeypatch):
        monkeypatch.setattr(session_config, "_fetch_from_pi", lambda ssh: None)
        monkeypatch.setattr(session_config, "_read_cache", lambda: None)
        session = load_session_config(config)
        assert session.name == "Arnold"
        assert session.voice == "marin"
        assert session.model == SessionConfig().model

    def test_blank_voice_keeps_the_pi_voice(self, config, pi_answers):
        config.assistant.voice = ""
        assert load_session_config(config).voice == "cedar"


class TestDelivery:
    def test_own_character_by_default(self, config):
        assert delivery_for(config) == DEFAULT_DELIVERY

    def test_the_butler_when_mirroring(self, config):
        config.assistant.mirror_jarvis = True
        assert "butler" in delivery_for(config).lower()

    def test_local_override_wins(self, config):
        config.voice.tts_instructions = "Shout."
        config.assistant.mirror_jarvis = True
        assert delivery_for(config) == "Shout."

    def test_assistant_delivery_beats_the_built_in(self, config):
        config.assistant.delivery = "Whisper."
        assert delivery_for(config) == "Whisper."


class FakeJarvis:
    def __init__(self, fail: bool = False) -> None:
        self.said: list[str] = []
        self.fail = fail
        self.enabled = True

    def say(self, text):
        from arnold.jarvis import JarvisError

        if self.fail:
            raise JarvisError("the Pi is off")
        self.said.append(text)
        return {"ok": True}


class FakeContext:
    def __init__(self, jarvis) -> None:
        self.jarvis = jarvis


class TestTalkingToJarvis:
    @staticmethod
    def names(config):
        return {t["name"] for t in build_tools(config)}

    def test_both_tools_are_offered(self, config):
        assert {"ask_jarvis", "tell_jarvis"} <= self.names(config)

    def test_not_when_the_pc_is_jarvis(self, config):
        config.assistant.mirror_jarvis = True
        assert not {"ask_jarvis", "tell_jarvis"} & self.names(config)

    def test_asking_needs_the_relay(self, config):
        config.jarvis.home_assistant.token = ""
        names = self.names(config)
        assert "ask_jarvis" not in names
        assert "tell_jarvis" in names

    def test_telling_needs_a_speech_route(self, config):
        config.jarvis.speech_route = "none"
        assert "tell_jarvis" not in self.names(config)

    def test_tell_reaches_the_pi(self, config):
        jarvis = FakeJarvis()
        dispatch = ToolDispatcher(config, FakeContext(jarvis))
        result = dispatch("tell_jarvis", {"message": "Dinner is ready."})
        assert result["ok"]
        assert jarvis.said == ["Dinner is ready."]

    def test_tell_reports_a_dead_pi(self, config):
        dispatch = ToolDispatcher(config, FakeContext(FakeJarvis(fail=True)))
        assert "error" in dispatch("tell_jarvis", {"message": "hello"})

    def test_tell_needs_words(self, config):
        dispatch = ToolDispatcher(config, FakeContext(FakeJarvis()))
        assert "error" in dispatch("tell_jarvis", {"message": "  "})

    def test_ask_relays_the_reply_attributed(self, config, monkeypatch):
        from arnold.voice import brain

        asked = []

        def fake_ask(self, text):
            asked.append(text)
            return "The oven timer has four minutes left."

        monkeypatch.setattr(brain.JarvisBrain, "ask", fake_ask)
        dispatch = ToolDispatcher(config, FakeContext(FakeJarvis()))
        result = dispatch("ask_jarvis", {"text": "how long on the oven?"})
        assert asked == ["how long on the oven?"]
        assert result["from"] == "Jarvis"
        assert "four minutes" in result["reply"]

    def test_ask_reports_an_unreachable_pi(self, config, monkeypatch):
        from arnold.voice import brain

        def fake_ask(self, text):
            raise brain.BrainError("I couldn't reach Jarvis on the Pi.")

        monkeypatch.setattr(brain.JarvisBrain, "ask", fake_ask)
        dispatch = ToolDispatcher(config, FakeContext(FakeJarvis()))
        assert "error" in dispatch("ask_jarvis", {"text": "anything"})


class TestPalettes:
    @pytest.mark.parametrize("palette", sorted(PALETTES))
    def test_every_palette_covers_every_mode(self, palette):
        for mode in FaceMode:
            assert mode in PALETTES[palette]

    def test_steel_idle_is_cool_where_gold_is_warm(self):
        gold = HoloTheme.for_mode(FaceMode.IDLE, "gold").wire
        steel = HoloTheme.for_mode(FaceMode.IDLE, "steel").wire
        assert gold[0] > gold[2]
        assert steel[2] > steel[0]

    def test_steel_listening_still_differs_from_its_idle(self):
        idle = HoloTheme.for_mode(FaceMode.IDLE, "steel")
        listening = HoloTheme.for_mode(FaceMode.LISTENING, "steel")
        assert idle.wire != listening.wire
        assert sum(listening.hot) > sum(idle.hot)

    def test_an_unknown_palette_falls_back_to_gold(self):
        assert HoloTheme.for_mode(FaceMode.IDLE, "plaid") == HoloTheme.for_mode(FaceMode.IDLE)
        assert HoloRenderer(80, supersample=1, palette="plaid").palette == "gold"

    @pytest.mark.parametrize("palette", sorted(PALETTES))
    def test_every_palette_renders_every_mode(self, palette):
        renderer = HoloRenderer(100, supersample=1, palette=palette)
        for mode in FaceMode:
            a = FaceAnimator()
            a.set_mode(mode)
            for _ in range(30):
                state = a.tick(1 / 60)
            assert renderer.render(state).size == (100, 100)


class TestDesigns:
    def test_default_design_follows_the_identity(self, config):
        assert config.face_design() == "lattice"
        config.assistant.mirror_jarvis = True
        assert config.face_design() == "core"
        config.face.design = "lattice"
        assert config.face_design() == "lattice"

    def test_an_unknown_design_falls_back_to_core(self):
        from arnold.face.holo import DESIGNS

        assert HoloRenderer(80, supersample=1, design="blob").design == "core"
        assert set(DESIGNS) >= {"core", "lattice"}

    def test_the_two_designs_are_different_objects(self):
        """Same palette, same state: the lattice must not be the core repainted."""
        a = FaceAnimator()
        a.set_mode(FaceMode.IDLE)
        for _ in range(60):
            state = a.tick(1 / 60)
        core = HoloRenderer(120, supersample=1, design="core").render(state)
        lattice = HoloRenderer(120, supersample=1, design="lattice").render(state)
        differing = sum(1 for p, q in zip(core.getdata(), lattice.getdata()) if p != q)
        assert differing > 120 * 120 * 0.15

    def test_the_lattice_turns_the_other_way(self):
        assert HoloRenderer(80, supersample=1, design="lattice").SPIN < 0
        assert HoloRenderer(80, supersample=1, design="core").SPIN > 0

    @pytest.mark.parametrize("design", ["core", "lattice"])
    def test_every_design_renders_every_mode_inside_the_window(self, design):
        renderer = HoloRenderer(100, supersample=1, design=design)
        for mode in FaceMode:
            a = FaceAnimator()
            a.set_mode(mode)
            for _ in range(30):
                state = a.tick(1 / 60)
            img = renderer.render(state)
            px = img.load()
            for i in range(100):
                assert px[i, 0] == px[0, i] == px[i, 99] == px[99, i]


class TestProfiles:
    """One name for the whole identity, with the flat fields as the fallback."""

    def test_no_profile_keeps_the_flat_fields(self, config):
        before = config.identity_key()
        assert config.apply_profile() == ""
        assert config.identity_key() == before
        assert config.active_profile == ""

    def test_builtin_mycroft_matches_the_defaults(self, config):
        config.apply_profile("mycroft")
        assert config.assistant_name() == "Mycroft"
        assert config.assistant.voice == "marin"
        assert config.voice.wake_word == "hey_mycroft"
        assert config.face_palette() == "steel"
        assert config.face_design() == "lattice"
        assert config.assistant.mirror_jarvis is False
        assert config.active_profile == "mycroft"

    def test_builtin_arnold_is_written_as_an_acronym(self, config):
        config.apply_profile("arnold")
        assert config.assistant_name() == "Arnold"
        assert config.assistant.slug == "arnold"
        assert config.assistant_title() == "A.R.N.O.L.D."
        assert config.assistant_motto() == "A Rather Nice, Ordinary, Loyal Daemon"

    def test_builtin_arnold_has_his_own_character(self, config):
        config.apply_profile("arnold")
        assert config.assistant.voice == "ash"
        assert "cheeky" in persona_for(config)
        assert "pc_agent" in persona_for(config)  # still knows his job
        assert "cheeky" in delivery_for(config)
        assert "Scottish" in persona_for(config) and "Scottish" in delivery_for(config)
        assert persona_for(config).startswith("ACCENT")  # leads, so it is not buried
        assert "Scottish" in accent_reminder_for(config)
        config.assistant.persona = "Someone else entirely."
        assert accent_reminder_for(config) == ""
        config.assistant.persona = ""
        config.apply_profile("mycroft")
        assert delivery_for(config) == DEFAULT_DELIVERY

    def test_title_falls_back_to_the_name(self, config):
        config.apply_profile("mycroft")
        assert config.assistant_title() == "Mycroft"
        assert config.assistant_motto() == ""

    def test_builtin_jarvis_mirrors(self, config):
        config.apply_profile("jarvis")
        assert config.assistant.mirror_jarvis is True
        assert config.assistant_name() == "Jarvis"
        assert config.face_palette() == "gold"
        assert config.face_design() == "core"
        assert config.voice.wake_word == "hey_jarvis"
        assert config.assistant_topic_prefix() == "jarvis/hud"

    def test_user_profile_overrides_one_field_of_a_builtin(self, config):
        config.assistant.profiles = {"jarvis": {"voice": "ash"}}
        config.apply_profile("jarvis")
        assert config.assistant.mirror_jarvis is True
        assert config.assistant.voice == "ash"

    def test_only_set_fields_override(self, config):
        config.face.palette = "gold"
        config.assistant.profiles = {"athena": {"name": "Athena"}}
        config.apply_profile("athena")
        assert config.assistant_name() == "Athena"
        assert config.assistant.voice == "marin"
        assert config.face_palette() == "gold"

    def test_empty_persona_in_a_profile_means_built_in(self, config):
        config.assistant.persona = "You are somebody else."
        config.assistant.profiles = {"plain": {"persona": ""}}
        config.apply_profile("plain")
        assert config.assistant.persona == ""
        assert "Arnold" in persona_for(config)

    def test_names_are_normalised(self, config):
        config.assistant.profiles = {"Dr Watson": {"name": "Watson"}}
        assert "dr_watson" in config.profiles()
        assert config.apply_profile("Dr Watson") == "dr_watson"

    def test_unknown_profile_is_a_config_error(self, config):
        from arnold.config import ConfigError

        with pytest.raises(ConfigError, match="jarvis"):
            config.apply_profile("nobody")

    def test_unknown_profile_key_is_a_config_error(self, config):
        from arnold.config import ConfigError

        config.assistant.profiles = {"x": {"colour": "red"}}
        with pytest.raises(ConfigError, match="assistant.profiles.x"):
            config.profiles()

    def test_validate_flags_an_unknown_profile(self, config):
        config.assistant.profile = "nobody"
        assert any("assistant.profile" in p for p in config.validate())
        config.assistant.profile = "jarvis"
        assert not any("assistant.profile" in p for p in config.validate())

    def test_load_config_applies_the_profile(self, tmp_path):
        from arnold.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text("assistant:\n  profile: jarvis\n", encoding="utf-8")
        loaded = load_config(path)
        assert loaded.assistant_name() == "Jarvis"
        assert loaded.voice.wake_word == "hey_jarvis"
        assert loaded.active_profile == "jarvis"

    def test_wake_word_dir_is_config_relative(self, tmp_path):
        from arnold.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text("assistant: {}\n", encoding="utf-8")
        loaded = load_config(path)
        assert loaded.voice.wake_word_dir == str(tmp_path / "models" / "wake")

    def test_adopt_identity_changes_in_place_and_reports(self, config):
        other = Config()
        other.apply_profile("jarvis")
        same = config
        assert config.adopt_identity(other) is True
        assert same is config
        assert config.assistant_name() == "Jarvis"
        assert config.active_profile == "jarvis"
        assert config.adopt_identity(other) is False

    def test_the_session_follows_the_profile(self, config, pi_answers):
        config.apply_profile("jarvis")
        session = load_session_config(config)
        assert session.voice == "cedar"
        assert session.source == "pi"
        config.apply_profile("mycroft")
        session = load_session_config(config)
        assert session.name == "Mycroft"
        assert session.voice == "marin"


class ProfileContext:
    def __init__(self, config) -> None:
        self.config = config
        self.jarvis = FakeJarvis()


class TestSwitchProfileTool:
    def test_the_tool_is_offered_and_lists_profiles(self, config):
        tool = next(t for t in build_tools(config) if t["name"] == "switch_profile")
        assert "mycroft" in tool["description"]
        assert "jarvis" in tool["description"]
        assert "end_conversation" in tool["description"]

    def test_it_rewrites_the_file_and_the_config(self, config, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("assistant:\n  profile: mycroft\n  name: Mycroft\n", encoding="utf-8")
        config.source_path = path
        result = ToolDispatcher(config, ProfileContext(config))("switch_profile", {"name": "jarvis"})
        assert result["ok"]
        assert result["name"] == "Jarvis"
        assert "end_conversation" in result["note"]
        assert "profile: jarvis" in path.read_text(encoding="utf-8")
        assert config.assistant_name() == "Jarvis"

    def test_the_display_name_works_too(self, config, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("assistant:\n  profile: mycroft\n", encoding="utf-8")
        config.source_path = path
        result = ToolDispatcher(config, ProfileContext(config))("switch_profile", {"name": "Jarvis"})
        assert result["ok"]

    def test_an_unknown_name_is_an_error_and_leaves_the_file(self, config, tmp_path):
        path = tmp_path / "config.yaml"
        original = "assistant:\n  profile: mycroft\n"
        path.write_text(original, encoding="utf-8")
        config.source_path = path
        result = ToolDispatcher(config, ProfileContext(config))("switch_profile", {"name": "hal"})
        assert "error" in result
        assert path.read_text(encoding="utf-8") == original
        assert config.assistant_name() == "Arnold"

    def test_without_a_file_it_says_so(self, config):
        config.source_path = None
        result = ToolDispatcher(config, ProfileContext(config))("switch_profile", {"name": "jarvis"})
        assert "error" in result


class TestRunnerNoticesTheSwitch:
    """The tool mutates the runner's own Config in place, so by the time the
    wake loop looks, the file and the config already agree and the watcher
    alone would see nothing. The runner has to compare against what it last
    built itself from."""

    class Stub:
        def __init__(self, config, watcher_result=None):
            self.config = config
            self._identity = config.identity_key()
            self._watcher = types.SimpleNamespace(changed=lambda: watcher_result)

    def test_a_file_change_is_reported_as_the_fresh_config(self, config):
        from arnold.voice.realtime_runner import RealtimeVoiceAssistant

        fresh = Config()
        fresh.apply_profile("jarvis")
        stub = self.Stub(config, watcher_result=fresh)
        assert RealtimeVoiceAssistant._identity_moved(stub) is fresh

    def test_an_in_place_switch_is_reported_as_our_own_config(self, config):
        from arnold.voice.realtime_runner import RealtimeVoiceAssistant

        stub = self.Stub(config)
        assert RealtimeVoiceAssistant._identity_moved(stub) is None
        config.apply_profile("jarvis")  # what switch_profile does
        assert RealtimeVoiceAssistant._identity_moved(stub) is config
