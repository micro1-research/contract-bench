"""Grade collected rollouts with the dataset's own grader, against every rubric set that applies.

A Turn 1 rollout is graded against the five rubric sets of its scenario (one per attorney); a Turn 2 rollout
against its task's rubric set. Each grade is one run of the target task's `tests/judge.py` from a temporary copy
(so the dataset directory is never written to), with the judge panel from `configs/protocol.toml`.

    runs/<run-id>/grades/<model>/<target task>/attempt<N>/{grade.json, reward.json, change_list.md, judges/,
                                                          judge.log, grade_meta.json}

A grade is skipped when `grade.json` exists for the same deliverable (by hash) with a full panel. Grades that come
back without `grade.json` or short of a full panel are retried, then kept and flagged as degraded.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .dataset import Dataset, TaskInfo, sha256
from .keys import grading_env
from .ledger import Run


@dataclass
class GradeJob:
    model_slug: str
    input_task: str
    attempt: int
    target: TaskInfo
    deliverable: Path
    out_dir: Path

    @property
    def key(self) -> str:
        return f"{self.model_slug}/{self.target.name}/attempt{self.attempt}"


def _file_sha(p: Path) -> str:
    return sha256(p)


def _meta(job: GradeJob, panel: str, started: str, rc: int | None, grade: dict | None, degraded: bool) -> dict:
    return {
        "model_slug": job.model_slug,
        "input_task": job.input_task,
        "attempt": job.attempt,
        "target_task": job.target.name,
        "deliverable_sha256": _file_sha(job.deliverable),
        "panel": panel,
        "judge_py_sha256": _file_sha(job.target.tests_dir / "judge.py"),
        "rubrics_sha256": _file_sha(job.target.rubrics_path),
        "started_at": started,
        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "exit_code": rc,
        "survivors": (grade or {}).get("survivors"),
        "judge_mode": (grade or {}).get("judge_mode"),
        "gate_passed": ((grade or {}).get("gate") or {}).get("passed"),
        "degraded": degraded,
    }


def plan(run: Run, ds: Dataset, slugs: list[str] | None = None, task_globs: list[str] | None = None) -> list[GradeJob]:
    """Every (counted, successful attempt) x (rubric set) pair."""
    import fnmatch

    jobs = []
    rollouts = run.dir / "rollouts"
    for slug_dir in sorted(p for p in rollouts.iterdir() if p.is_dir()) if rollouts.exists() else []:
        if slugs and slug_dir.name not in slugs:
            continue
        for input_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            if input_dir.name not in ds.tasks:
                continue
            if task_globs and not any(fnmatch.fnmatch(input_dir.name, g) for g in task_globs):
                continue
            for att in sorted(p for p in input_dir.glob("attempt*") if p.is_dir()):
                trial = att / "trial.json"
                if not trial.exists():
                    continue
                t = json.loads(trial.read_text())
                if not (t.get("counted") and t.get("status") == "success" and (att / "contract.docx").exists()):
                    continue
                n = int(att.name.replace("attempt", ""))
                for target in ds.siblings(input_dir.name):
                    out = run.dir / "grades" / slug_dir.name / target.name / att.name
                    jobs.append(GradeJob(slug_dir.name, input_dir.name, n, target, att / "contract.docx", out))
    return jobs


def is_done(job: GradeJob, panel_size: int, require_full_panel: bool) -> bool:
    g, m = job.out_dir / "grade.json", job.out_dir / "grade_meta.json"
    if not (g.exists() and m.exists()):
        return False
    try:
        meta = json.loads(m.read_text())
        grade = json.loads(g.read_text())
    except ValueError:
        return False
    if meta.get("deliverable_sha256") != _file_sha(job.deliverable):
        return False
    if meta.get("rubrics_sha256") != _file_sha(job.target.rubrics_path):
        return False
    if (grade.get("gate") or {}).get("passed") is False:
        return True  # a gate failure is a final verdict, no judges involved
    if require_full_panel and not meta.get("degraded", False):
        return len(grade.get("survivors") or []) == panel_size and grade.get("judge_mode") == "panel"
    return True


def run_one(job: GradeJob, env: dict[str, str], panel: str, python: str, timeout: int = 3600) -> dict:
    """Run judge.py for one job; returns grade.json content (or {} when the grader produced none)."""
    if job.out_dir.exists():
        shutil.rmtree(job.out_dir)
    job.out_dir.mkdir(parents=True)
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    work = Path(tempfile.mkdtemp(prefix="cb-grade-"))
    try:
        tests = work / "tests"
        shutil.copytree(job.target.tests_dir, tests, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        cmd = [
            python,
            "-B",
            str(tests / "judge.py"),
            "--contract",
            str(job.deliverable),
            "--baseline",
            str(tests / "baseline" / "contract.docx"),
            "--out-dir",
            str(job.out_dir),
        ]
        with open(job.out_dir / "judge.log", "w") as lf:
            r = subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    grade = {}
    if (job.out_dir / "grade.json").exists():
        try:
            grade = json.loads((job.out_dir / "grade.json").read_text())
        except ValueError:
            grade = {}
    (job.out_dir / "grade_meta.json").write_text(json.dumps(_meta(job, panel, started, rc, grade, False), indent=1) + "\n")
    return grade


def _short(grade: dict, panel_size: int) -> bool:
    if not grade:
        return True
    if (grade.get("gate") or {}).get("passed") is False:
        return False
    return len(grade.get("survivors") or []) < panel_size or grade.get("judge_mode") != "panel"


def grade_all(
    run: Run,
    ds: Dataset,
    env: dict[str, str],
    jobs: list[GradeJob],
    *,
    panel: str,
    parallel: int,
    retries: int,
    rubrics_per_call: int = 0,
    panel_parallel: bool = True,
    require_full_panel: bool = True,
    python: str | None = None,
    log=print,
) -> dict:
    """Grade every planned job that is not already done. Returns counts."""
    panel_size = len([j for j in panel.split(",") if j.strip()])
    genv = grading_env(env, panel, rubrics_per_call, panel_parallel)
    py = python or sys.executable
    todo = [j for j in jobs if not is_done(j, panel_size, require_full_panel)]
    log(
        f"grading: {len(jobs)} grades planned, {len(jobs) - len(todo)} already done, {len(todo)} to run, "
        f"{parallel} in parallel ({parallel * panel_size} judge calls in flight)"
    )
    counts = {"planned": len(jobs), "skipped": len(jobs) - len(todo), "run": 0, "degraded": 0, "failed": 0}
    pending = todo
    for pass_no in range(retries + 1):
        if not pending:
            break
        if pass_no:
            log(f"retry pass {pass_no}: {len(pending)} grades short of a full panel")
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
            for job, grade in zip(pending, ex.map(lambda j: run_one(j, genv, panel, py), pending), strict=True):
                results[job.key] = grade
                counts["run"] += 1
        pending = [j for j in pending if _short(results[j.key], panel_size)]
    for j in pending:  # still short after the retry passes: keep what we have, flagged
        m = j.out_dir / "grade_meta.json"
        if m.exists():
            meta = json.loads(m.read_text())
            meta["degraded"] = True
            m.write_text(json.dumps(meta, indent=1) + "\n")
        if (j.out_dir / "grade.json").exists():
            counts["degraded"] += 1
        else:
            counts["failed"] += 1
    log(f"grading done: {counts}")
    return counts


def estimate(jobs: list[GradeJob], ds: Dataset) -> dict:
    """Rough judge input volume from change lists captured at collection time and rubric sizes."""
    chars = 0
    missing = 0
    for j in jobs:
        cl = j.deliverable.parent / "gate" / "change_list.md"
        if cl.exists():
            chars += cl.stat().st_size
        else:
            missing += 1
        chars += j.target.rubrics_path.stat().st_size
    return {
        "grades": len(jobs),
        "judge_calls": len(jobs) * 3,
        "approx_input_tokens_per_judge": chars // 4,
        "grades_without_change_list": missing,
    }


def panel_of(run: Run) -> str:
    return run.data["protocol"]["grading"]["panel"]


def environ_with(env: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **env}


def grade_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()
