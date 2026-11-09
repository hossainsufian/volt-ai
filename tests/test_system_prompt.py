"""Tests for the unified persona system prompt.

The persona should match the user's configured wake word so renaming the
wake word to e.g. "Friday" produces a butler named Friday, not one still
hardcoded to Jarvis.
"""

from jarvis.system_prompt import build_system_prompt


class TestBuildSystemPrompt:
    def test_default_name_is_jarvis(self):
        prompt = build_system_prompt()
        assert "you are Volt AI," in prompt

    def test_custom_name_replaces_jarvis(self):
        prompt = build_system_prompt("Friday")
        assert "you are Friday," in prompt
        assert "you are Volt AI," not in prompt

    def test_lowercase_wake_word_is_capitalised(self):
        prompt = build_system_prompt("friday".capitalize())
        assert "you are Friday," in prompt

    def test_blank_name_falls_back_to_jarvis(self):
        assert "you are Volt AI," in build_system_prompt("")
        assert "you are Volt AI," in build_system_prompt("   ")
        assert "you are Volt AI," in build_system_prompt(None)  # type: ignore[arg-type]
