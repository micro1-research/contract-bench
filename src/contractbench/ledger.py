"""The run ledger: `runs/<run-id>/run.json` (what was run, with which settings) and the attempt accounting that
turns Harbor trials into counted attempts.

Counting rule (the leaderboard's): per model and input, finished trials that are successes or counted failures
are ordered by start time and numbered 1, 2, 3, ...; the first `attempts_per_input` count. Re-run statuses
(infrastructure errors, timeouts under the re-run policy, missing deliverables, unfinished trials) are never
numbered and never counted.
"""

from __future__ import annotations

import datetime as dt
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .classify import COUNTED_FAILURES

RUN_JSON = "run.json"
LEDGER_JSON = "ledger.json"


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def load_protocol(path: Path | None = None) -> dict:
    with open(path or paths.configs_dir() / "protocol.toml", "rb") as f:
        return tomllib.load(f)


@dataclass
class Wave:
    model_slug: str
    wave: int
    job_name: str
    job_dir: str
    kind: str
    inputs: list[str]
    k: int
    argv: list[str]
    created_at: str = field(default_factory=now)
    launched_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    pid: int | None = None
    status: str = "planned"


class Run:
    """run.json wrapper. One run holds one dataset snapshot, one protocol, any number of models and their waves."""

    def __init__(self, run_id: str, run_dir: Path | None = None):
        self.run_id = run_id
        self.dir = run_dir or paths.run_dir(run_id)
        self.data: dict = {}

    @property
    def path(self) -> Path:
        return self.dir / RUN_JSON

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Run:
        self.data = json.loads(self.path.read_text())
        return self

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def create(
        self, dataset_desc: dict, protocol: dict, set_name: str, task_globs: list[str], harbor_version: str, package_version: str
    ) -> Run:
        self.data = {
            "run_id": self.run_id,
            "created_at": now(),
            "contractbench_version": package_version,
            "harbor_version": harbor_version,
            "dataset": dataset_desc,
            "protocol": protocol,
            "set": set_name,
            "task_globs": task_globs,
            "models": {},
            "waves": [],
        }
        self.save()
        return self

    # models
    def model_settings(self, slug: str) -> dict | None:
        return self.data["models"].get(slug)

    def record_model(self, slug: str, settings: dict) -> None:
        self.data["models"][slug] = settings
        self.save()

    # waves
    def waves(self, slug: str | None = None) -> list[Wave]:
        ws = [Wave(**w) for w in self.data.get("waves", [])]
        return [w for w in ws if slug is None or w.model_slug == slug]

    def add_wave(self, w: Wave) -> None:
        self.data.setdefault("waves", []).append(w.__dict__)
        self.save()

    def update_wave(self, w: Wave) -> None:
        for i, raw in enumerate(self.data.get("waves", [])):
            if raw["job_name"] == w.job_name:
                self.data["waves"][i] = w.__dict__
        self.save()

    def next_wave_index(self, slug: str) -> int:
        return max((w.wave for w in self.waves(slug)), default=-1) + 1

    def job_name(self, slug: str, wave: int) -> str:
        return f"{self.run_id}--{slug}--w{wave:02d}"

    def jobs_root(self) -> Path:
        return paths.jobs_dir() / self.run_id


# ---- attempt accounting ------------------------------------------------------------------------------------------ #
@dataclass
class TrialEntry:
    """One trial as the ledger sees it (built by collect from a TrialRecord plus its Classification)."""

    model_slug: str
    input_task: str
    trial_name: str
    job_name: str
    status: str
    started_at: str | None
    finished_at: str | None
    reason: str = ""
    attempt: int | None = None
    counted: bool = False


def number_attempts(entries: list[TrialEntry], attempts_per_input: int) -> list[TrialEntry]:
    """Assign attempt numbers per (model, input) to finished, non-rerun trials in start order; mark the counted ones."""
    groups: dict[tuple[str, str], list[TrialEntry]] = {}
    for e in entries:
        e.attempt, e.counted = None, False
        groups.setdefault((e.model_slug, e.input_task), []).append(e)
    for members in groups.values():
        finished = [e for e in members if e.status == "success" or e.status in COUNTED_FAILURES]
        finished.sort(key=lambda e: (e.started_at or "", e.trial_name))
        for i, e in enumerate(finished, 1):
            e.attempt = i
            e.counted = i <= attempts_per_input
    return entries


def need_by_input(
    entries: list[TrialEntry], inputs: list[str], model_slug: str, attempts_per_input: int, max_reruns: int
) -> dict[str, dict]:
    """Per input: how many finished attempts exist, how many are still needed, how many re-runs were spent."""
    out = {}
    for inp in inputs:
        mine = [e for e in entries if e.model_slug == model_slug and e.input_task == inp]
        finished = sum(1 for e in mine if e.attempt is not None)
        reruns = sum(1 for e in mine if e.status in ("infra", "timeout", "no_deliverable"))
        unfinished = sum(1 for e in mine if e.status == "unfinished")
        need = max(0, attempts_per_input - finished)
        out[inp] = {
            "finished": finished,
            "need": need,
            "reruns": reruns,
            "unfinished": unfinished,
            "exhausted": need > 0 and reruns >= max_reruns and unfinished == 0,
        }
    return out
