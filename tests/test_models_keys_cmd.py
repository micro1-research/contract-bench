import json
from pathlib import Path

import pytest

from contractbench import harbor_cmd, keys, models
from tests.conftest import write_env


def test_registry_resolves_leaderboard_models():
    reg = models.load_registry()
    opus = models.resolve("opus-5-5", reg)
    assert opus.id == "anthropic/claude-opus-5-5" and opus.agent_kwargs == {"variant": "max"}
    assert opus.opencode_config()["provider"]["anthropic"]["models"]["claude-opus-5-5"]["options"] == {
        "thinking": {"type": "adaptive"}
    }
    gem = models.resolve("google/gemini-3.8-flash", reg)  # by id
    assert gem.alias == "gemini-3-8-flash" and gem.output_token_max == 65536 and gem.agent_kwargs == {"variant": "high"}
    kimi = models.resolve("kimi-k3", reg)
    assert kimi.provider == "openrouter" and kimi.best_effort
    raw = models.resolve("openai/some-new-model", reg)
    assert raw.alias == "some-new-model" and raw.output_token_max == 128000 and raw.opencode_options == {}


def test_overrides_and_settings_diff():
    reg = models.load_registry()
    rm = models.resolve("gpt-6-astra", reg, overrides=['reasoningEffort="high"', "extra.depth=3"], output_token_max=4096)
    assert rm.opencode_options == {"reasoningEffort": "high", "extra": {"depth": 3}} and rm.output_token_max == 4096
    a, b = models.resolve("gpt-6-astra", reg).settings(), rm.settings()
    diff = models.settings_diff(a, b)
    assert any(d.startswith("opencode_options") for d in diff) and any(d.startswith("output_token_max") for d in diff)
    with pytest.raises(ValueError):
        models.apply_override({}, "novalue")
    with pytest.raises(ValueError):
        models.split_id("nomodel")


def test_env_loading_and_aliases(workdir: Path):
    write_env(
        workdir, GROK_API_KEY="xai-secret-123456", GOOGLE_API_KEY="goog-secret-123456", ANTHROPIC_API_KEY="ant-secret-123456"
    )
    env = keys.load_env()
    assert env["XAI_API_KEY"] == "xai-secret-123456"
    assert env["GEMINI_API_KEY"] == env["GOOGLE_GENERATIVE_AI_API_KEY"] == "goog-secret-123456"
    assert keys.missing_keys(env, ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]) == ["OPENAI_API_KEY"]
    assert keys.panel_requirements("anthropic/claude-opus-4-8,openai/gpt-5.5,gemini/gemini-3.5-flash") == [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    ]
    assert keys.harbor_env(env, 65536)[keys.OUTPUT_CAP_VAR] == "65536"
    g = keys.grading_env({"JUDGE_MODEL": "x"}, "a/b,c/d")
    assert g["JUDGE_PANEL"] == "a/b,c/d" and "JUDGE_MODEL" not in g and g["JUDGE_PANEL_PARALLEL"] == "1"
    assert "xai-secret-123456" not in keys.mask("key xai-secret-123456 here", env)


def test_run_argv_shape(tmp_path: Path):
    rm = models.resolve("grok-4-7")
    argv = harbor_cmd.run_argv(
        binary="harbor",
        tasks_dir=tmp_path / "tasks",
        inputs=["a-t1-a", "a-t2-b"],
        model=rm,
        k=3,
        n_concurrent=4,
        job_name="r--grok-4-7--w00",
        jobs_dir=tmp_path / "jobs",
    )
    assert argv[:4] == ["harbor", "run", "-p", str(tmp_path / "tasks")]
    assert argv.count("-i") == 2 and "--disable-verification" in argv and "-y" in argv
    assert argv[argv.index("-r") + 1] == "0" and argv[argv.index("-k") + 1] == "3"
    ak = [argv[i + 1] for i, a in enumerate(argv) if a == "--ak"]
    cfg = json.loads(ak[0].split("=", 1)[1])
    assert cfg["provider"]["xai"]["models"]["grok-4.7"]["options"] == {"reasoningEffort": "xhigh"}
    assert cfg["permission"] == {"external_directory": "allow"}
    assert "--ae" in argv and f"{keys.OUTPUT_CAP_VAR}=128000" in argv
    res = harbor_cmd.resume_argv(
        binary="harbor", job_dir=tmp_path / "j", filter_error_types=["CancelledError", "ApiRateLimitError"]
    )
    assert res == ["harbor", "job", "resume", "-p", str(tmp_path / "j"), "-f", "CancelledError", "-f", "ApiRateLimitError"]


def test_fake_harbor_version(fake_harbor):
    assert harbor_cmd.check_version() == (0, 18, 0)
