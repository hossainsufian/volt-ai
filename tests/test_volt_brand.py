"""Every Volt brain knows who owns and made it."""

import volt_brand
from desktop_app import volt_claude, volt_fast, volt_online
from jarvis.system_prompt import build_system_prompt


def test_every_brain_is_told_who_owns_and_made_volt():
    prompts = [volt_fast.SYSTEM_PROMPT.format(today="x"), volt_online.SYSTEM_PROMPT.format(today="x"),
               volt_claude.SYSTEM_PROMPT, build_system_prompt()]
    for prompt in prompts:
        assert volt_brand.OWNER in prompt and volt_brand.MAKER in prompt
