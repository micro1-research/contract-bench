"""API keys and process environments.

`.env` in the working directory is loaded without overriding variables that are already set. Keys are aliased to the
names the tools read: Harbor's opencode agent forwards only a provider's own variables to the container, the judge
panel is called through litellm, and some users keep Google or xAI keys under other names.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ALIASES = {
    # source -> targets that are filled when absent
    "GROK_API_KEY": ["XAI_API_KEY"],
    "GOOGLE_API_KEY": ["GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"],
    "GEMINI_API_KEY": ["GOOGLE_GENERATIVE_AI_API_KEY"],
}
SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
OUTPUT_CAP_VAR = "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"
PANEL_KEY_OF = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}


def load_env(dotenv: Path | None = None) -> dict[str, str]:
    """os.environ plus the .env file (existing variables win), with aliases applied."""
    env = dict(os.environ)
    path = dotenv or Path(".env")
    if path.exists():
        from dotenv import dotenv_values

        for k, v in dotenv_values(path).items():
            if v is not None and not env.get(k):
                env[k] = v
    for src, targets in ALIASES.items():
        if env.get(src):
            for t in targets:
                env.setdefault(t, env[src])
    return env


def missing_keys(env: dict[str, str], names: list[str]) -> list[str]:
    return [n for n in names if not env.get(n)]


def panel_requirements(panel: str) -> list[str]:
    """Env var names the judge panel needs; the first provider (Anthropic) is also the fallback judge."""
    out = []
    for judge in [j.strip() for j in panel.split(",") if j.strip()]:
        provider = judge.split("/", 1)[0]
        var = PANEL_KEY_OF.get(provider, f"{provider.upper()}_API_KEY")
        if var not in out:
            out.append(var)
    return out


def harbor_env(env: dict[str, str], output_token_max: int) -> dict[str, str]:
    """The environment of the Harbor subprocess. The output cap is exported here as well as passed with `--ae`:
    Harbor treats the variable as a secret and only re-resolves it on resume when the process env carries the same
    value."""
    e = dict(env)
    e[OUTPUT_CAP_VAR] = str(output_token_max)
    return e


def grading_env(env: dict[str, str], panel: str, rubrics_per_call: int = 0, panel_parallel: bool = True) -> dict[str, str]:
    e = dict(env)
    e["JUDGE_PANEL"] = panel
    e["JUDGE_RUBRICS_PER_CALL"] = str(rubrics_per_call)
    e["JUDGE_PANEL_PARALLEL"] = "1" if panel_parallel else "0"
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    e.pop("JUDGE_MODEL", None)
    return e


def mask(text: str, env: dict[str, str]) -> str:
    """Replace secret values with asterisks when printing commands or environments."""
    for k, v in env.items():
        if v and len(v) >= 8 and SECRET_RE.search(k):
            text = text.replace(v, "****")
    return text
