"""Collect Harbor job trees into the run's canonical layout.

    runs/<run-id>/rollouts/<model>/<input task>/attempt<N>/{contract.docx, result.json, trial.json, gate/}
    runs/<run-id>/rollouts/<model>/<input task>/rerun/<trial name>/trial.json      (re-run trials, archived)
    runs/<run-id>/manifest.csv, ledger.json

`jobs/` is ephemeral: after collection, a job directory can be deleted. Collection is idempotent and renumbers
attempts on every pass; grades are keyed by the deliverable's hash as well, so renumbering never orphans one.
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from .classify import classify
from .dataset import Dataset
from .harbor_io import iter_trials, read_trial
from .ledger import Run, TrialEntry, number_attempts

MANIFEST_COLUMNS = [
    "model_slug",
    "model_id",
    "input_task",
    "scenario",
    "turn",
    "split",
    "attempt",
    "counted",
    "status",
    "reason",
    "started_at",
    "finished_at",
    "agent_exec_sec",
    "n_input_tokens",
    "n_cache_tokens",
    "n_output_tokens",
    "cost_usd",
    "deliverable_sha256",
    "gate_passed",
    "gate_reason",
    "trial_name",
    "job_name",
]


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    tmp.replace(p)


def collect(
    run: Run, ds: Dataset, slugs: list[str] | None = None, keep_logs: bool = False, run_gate: bool = True, log=print
) -> list[TrialEntry]:
    """Scan every wave's job directory of the chosen models, classify the trials, write the run layout."""
    protocol = run.data["protocol"]
    infra_types = set(protocol["classification"]["harbor_infra_error_types"])
    timeout_policy = protocol["rollouts"]["timeout_policy"]
    per_input = int(protocol["rollouts"]["attempts_per_input"])
    rollouts = run.dir / "rollouts"
    entries: list[TrialEntry] = []
    records: dict[tuple[str, str, str], dict] = {}  # (slug, job_name, trial_name) -> trial.json content
    for wave in run.waves():
        if slugs and wave.model_slug not in slugs:
            continue
        settings = run.model_settings(wave.model_slug) or {}
        job_dir = Path(wave.job_dir)
        for trial_dir in iter_trials(job_dir):
            rec = read_trial(trial_dir)
            task = rec.task_name
            if task is None or task not in ds.tasks:
                log(f"  skipping {trial_dir.name}: task unknown ({task})")
                continue
            input_task = ds.tasks[task].input_task
            # a classification is cached next to the trial while the trial directory still exists
            cache = trial_dir / "contractbench-classification.json"
            cached = None
            if cache.exists() and rec.finished:  # a finished trial never changes, so its classification is final
                try:
                    cached = json.loads(cache.read_text())
                except ValueError:
                    cached = None
            if cached is None:
                staging = run.dir / "_gate" / wave.model_slug / input_task / trial_dir.name
                c = classify(
                    rec, ds.tasks[input_task], staging, infra_types=infra_types, timeout_policy=timeout_policy, run_gate=run_gate
                )
                cls = c.to_dict()
                if rec.finished:
                    try:
                        cache.write_text(json.dumps(cls, indent=1, sort_keys=True))
                    except OSError:
                        pass
            else:
                cls = cached
            entry = TrialEntry(
                model_slug=wave.model_slug,
                input_task=input_task,
                trial_name=trial_dir.name,
                job_name=wave.job_name,
                status=cls["status"],
                started_at=rec.started_at,
                finished_at=rec.finished_at,
                reason=cls.get("reason", ""),
            )
            entries.append(entry)
            records[(wave.model_slug, wave.job_name, trial_dir.name)] = {
                "run_id": run.run_id,
                "model_slug": wave.model_slug,
                "model_id": settings.get("id"),
                "input_task": input_task,
                "scenario": ds.tasks[input_task].scenario,
                "turn": ds.tasks[input_task].turn,
                "split": ds.tasks[input_task].split,
                "trial_name": trial_dir.name,
                "job_name": wave.job_name,
                "job_dir": str(job_dir),
                "started_at": rec.started_at,
                "finished_at": rec.finished_at,
                "phases": rec.phases,
                "wall_sec": rec.wall_sec,
                "agent_exec_sec": rec.agent_exec_sec,
                "tokens": {"input": rec.n_input_tokens, "cache": rec.n_cache_tokens, "output": rec.n_output_tokens},
                "cost_usd": rec.cost_usd,
                "agent_version": rec.agent_version,
                "exception": {"type": rec.exception_type, "message_head": rec.exception_message},
                "classification": cls,
                "_trial_dir": str(trial_dir),
            }
    number_attempts(entries, per_input)
    # materialize
    manifest_rows = []
    for e in entries:
        rec = records[(e.model_slug, e.job_name, e.trial_name)]
        trial_dir = Path(rec.pop("_trial_dir"))
        rec["attempt"], rec["counted"] = e.attempt, e.counted
        rec["status"], rec["counted_failure"] = (
            e.status,
            e.status in ("length_stop", "ended_in_thinking", "no_edit_finished", "gate_rejected"),
        )
        rec["rerun"] = e.attempt is None
        base = rollouts / e.model_slug / e.input_task
        if e.attempt is None:
            dest = base / "rerun" / f"{e.job_name}__{e.trial_name}"
            dest.mkdir(parents=True, exist_ok=True)
            _write_json(dest / "trial.json", rec)
            if (trial_dir / "result.json").exists():
                shutil.copyfile(trial_dir / "result.json", dest / "result.json")
            continue
        dest = base / f"attempt{e.attempt}"
        dest.mkdir(parents=True, exist_ok=True)
        if (trial_dir / "result.json").exists():
            shutil.copyfile(trial_dir / "result.json", dest / "result.json")
        art = trial_dir / "artifacts" / "app" / "contract.docx"
        if art.exists():
            if not (dest / "contract.docx").exists() or (dest / "contract.docx").stat().st_size != art.stat().st_size:
                shutil.copyfile(art, dest / "contract.docx")
        staging = run.dir / "_gate" / e.model_slug / e.input_task / e.trial_name
        if staging.exists():
            if (dest / "gate").exists():
                shutil.rmtree(dest / "gate")
            shutil.move(str(staging), str(dest / "gate"))
        if keep_logs and (trial_dir / "agent" / "opencode.txt").exists():
            shutil.copyfile(trial_dir / "agent" / "opencode.txt", dest / "opencode.txt")
        _write_json(dest / "trial.json", rec)
        cls = rec["classification"]
        manifest_rows.append(
            {
                "model_slug": e.model_slug,
                "model_id": rec["model_id"],
                "input_task": e.input_task,
                "scenario": rec["scenario"],
                "turn": rec["turn"],
                "split": rec["split"],
                "attempt": e.attempt,
                "counted": int(e.counted),
                "status": e.status,
                "reason": e.reason,
                "started_at": e.started_at,
                "finished_at": e.finished_at,
                "agent_exec_sec": rec["agent_exec_sec"],
                "n_input_tokens": rec["tokens"]["input"],
                "n_cache_tokens": rec["tokens"]["cache"],
                "n_output_tokens": rec["tokens"]["output"],
                "cost_usd": rec["cost_usd"],
                "deliverable_sha256": (cls.get("deliverable") or {}).get("sha256"),
                "gate_passed": (cls.get("gate") or {}).get("passed"),
                "gate_reason": (cls.get("gate") or {}).get("reason"),
                "trial_name": e.trial_name,
                "job_name": e.job_name,
            }
        )
    shutil.rmtree(run.dir / "_gate", ignore_errors=True)
    manifest_rows.sort(key=lambda r: (r["model_slug"], r["input_task"], r["attempt"]))
    run.dir.mkdir(parents=True, exist_ok=True)
    with open(run.dir / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        w.writeheader()
        w.writerows(manifest_rows)
    _write_json(run.dir / "ledger.json", {"entries": [asdict(e) for e in entries]})
    return entries


def load_ledger(run: Run) -> list[TrialEntry]:
    p = run.dir / "ledger.json"
    if not p.exists():
        return []
    return [TrialEntry(**e) for e in json.loads(p.read_text())["entries"]]
