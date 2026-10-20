"""Readers for what Harbor writes: a job directory with `config.json`, `result.json` and one directory per trial
holding `config.json`, `result.json`, `artifacts/app/contract.docx` and the agent's `opencode.txt` event stream.

Trial directory names are truncated by Harbor, so the task a trial belongs to is always read from its files.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

ARTIFACT_REL = Path("artifacts") / "app" / "contract.docx"
OPENCODE_LOG_REL = Path("agent") / "opencode.txt"


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _find_key(obj, key: str):
    """Depth-first search for a key in nested dicts/lists (Harbor's config layouts have moved between versions)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class TrialRecord:
    trial_dir: Path
    trial_name: str
    task_name: str | None
    finished: bool
    started_at: str | None = None
    finished_at: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    agent_version: str | None = None
    phases: dict = field(default_factory=dict)
    artifact: Path | None = None
    opencode_log: Path | None = None

    @property
    def agent_exec_sec(self) -> float | None:
        p = self.phases.get("agent_execution") or {}
        a, b = parse_time(p.get("started_at")), parse_time(p.get("finished_at"))
        return (b - a).total_seconds() if a and b else None

    @property
    def wall_sec(self) -> float | None:
        a, b = parse_time(self.started_at), parse_time(self.finished_at)
        return (b - a).total_seconds() if a and b else None


def trial_task_name(trial_dir: Path) -> str | None:
    """The task a trial ran, from result.json or, before it exists, from the trial's config.json."""
    res = _read_json(trial_dir / "result.json")
    if res and res.get("task_name"):
        return str(res["task_name"]).split("/")[-1]
    cfg = _read_json(trial_dir / "config.json")
    if cfg:
        task = cfg.get("task") if isinstance(cfg.get("task"), dict) else None
        path = (task or {}).get("path") or _find_key(cfg, "path")
        if path:
            return Path(str(path)).name
    return None


def read_trial(trial_dir: Path) -> TrialRecord:
    res = _read_json(trial_dir / "result.json")
    rec = TrialRecord(
        trial_dir=trial_dir, trial_name=trial_dir.name, task_name=trial_task_name(trial_dir), finished=res is not None
    )
    art = trial_dir / ARTIFACT_REL
    rec.artifact = art if art.exists() else None
    log = trial_dir / OPENCODE_LOG_REL
    rec.opencode_log = log if log.exists() else None
    if res is None:
        return rec
    rec.started_at, rec.finished_at = res.get("started_at"), res.get("finished_at")
    exc = res.get("exception_info") or {}
    rec.exception_type = exc.get("exception_type")
    rec.exception_message = (exc.get("exception_message") or "")[:500] or None
    ar = res.get("agent_result") or {}
    rec.n_input_tokens, rec.n_cache_tokens = ar.get("n_input_tokens"), ar.get("n_cache_tokens")
    rec.n_output_tokens, rec.cost_usd = ar.get("n_output_tokens"), ar.get("cost_usd")
    rec.agent_version = (res.get("agent_info") or {}).get("version")
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        if res.get(phase):
            rec.phases[phase] = res[phase]
    return rec


def iter_trials(job_dir: Path) -> list[Path]:
    """Trial directories of a job: every subdirectory with a config.json or a result.json."""
    if not job_dir.is_dir():
        return []
    return sorted(p for p in job_dir.iterdir() if p.is_dir() and ((p / "config.json").exists() or (p / "result.json").exists()))


def job_model(job_dir: Path) -> str | None:
    cfg = _read_json(job_dir / "config.json") or {}
    return _find_key(cfg, "model_name")


@dataclass
class OpencodeSummary:
    n_steps: int = 0
    last_finish_reason: str | None = None
    last_step_tool_calls: int = 0
    last_step_reasoning_parts: int = 0
    last_step_text_parts: int = 0
    error_messages: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cost: float = 0.0


def summarize_opencode_log(path: Path) -> OpencodeSummary:
    """One pass over `opencode run --format=json` events: steps, the last step's finish reason and parts, errors."""
    s = OpencodeSummary()
    cur_tools = cur_reason = cur_text = 0
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return s
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("type")
        part = e.get("part") if isinstance(e.get("part"), dict) else {}
        if t == "step_start":
            cur_tools = cur_reason = cur_text = 0
        elif t == "tool_use":
            cur_tools += 1
        elif t == "reasoning":
            cur_reason += 1
        elif t == "text":
            cur_text += 1
        elif t == "step_finish":
            s.n_steps += 1
            s.last_finish_reason = part.get("reason")
            s.last_step_tool_calls, s.last_step_reasoning_parts, s.last_step_text_parts = cur_tools, cur_reason, cur_text
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            s.total_input_tokens += int(tokens.get("input") or 0) + int(cache.get("read") or 0)
            s.total_output_tokens += int(tokens.get("output") or 0)
            s.total_reasoning_tokens += int(tokens.get("reasoning") or 0)
            s.total_cost += float(part.get("cost") or 0)
        elif t == "error":
            err = e.get("error") or {}
            msg = err.get("data", {}).get("message") if isinstance(err.get("data"), dict) else None
            s.error_messages.append(str(msg or err.get("name") or err)[:300])
    return s
