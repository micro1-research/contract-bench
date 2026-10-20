"""Launch and resume Harbor jobs under the protocol.

One Harbor job ("wave") per model and launch. `run` starts wave 0 for a model, or resumes it when the job already
exists. A tick does three things in order: collect what is on disk, resume waves that still have unfinished or
infrastructure-failed trials (`harbor job resume`, which re-runs exactly those trial slots), then open a top-up
wave for inputs that still lack finished attempts. Counted failures are never re-run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, harbor_cmd, keys
from .collect import collect, load_ledger
from .dataset import Dataset
from .ledger import Run, Wave, need_by_input, now
from .models import ResolvedModel, settings_diff


class RunError(RuntimeError):
    pass


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_run(
    run_id: str,
    ds: Dataset,
    protocol: dict,
    set_name: str,
    task_globs: list[str],
    harbor_version: str,
    attempts_per_input: int | None = None,
) -> Run:
    """Create the run (recording the protocol, with -k as its attempts per input) or load and check an existing one."""
    run = Run(run_id)
    if run.exists():
        run.load()
        if run.data["dataset"].get("task_digest") != ds.digest():
            raise RunError(f"run {run_id} was created on a different task set (digest changed); start a new run id")
        recorded = int(run.data["protocol"]["rollouts"]["attempts_per_input"])
        if attempts_per_input and attempts_per_input != recorded:
            raise RunError(f"run {run_id} uses {recorded} attempts per input; pass the same -k or start a new run id")
        return run
    proto = json.loads(json.dumps(protocol))
    if attempts_per_input:
        proto["rollouts"]["attempts_per_input"] = int(attempts_per_input)
    return run.create(ds.describe(), proto, set_name, task_globs, harbor_version, __version__)


def register_model(run: Run, model: ResolvedModel, n_concurrent: int) -> None:
    settings = model.settings()
    settings["n_concurrent"] = n_concurrent
    recorded = run.model_settings(model.slug)
    if recorded is None:
        run.record_model(model.slug, settings)
        return
    diff = settings_diff(recorded, settings)
    if diff:
        raise RunError(
            f"model {model.slug} in run {run.run_id} was recorded with different settings:\n  "
            + "\n  ".join(diff)
            + "\n  use a new --run-id, or pass the same settings"
        )


def start_wave(
    run: Run,
    ds: Dataset,
    model: ResolvedModel,
    inputs: list[str],
    k: int,
    kind: str,
    env: dict[str, str],
    dry_run: bool = False,
    log=print,
) -> Wave | None:
    binary = harbor_cmd.harbor_bin()
    idx = run.next_wave_index(model.slug)
    job_name = run.job_name(model.slug, idx)
    jobs_root = run.jobs_root()
    argv = harbor_cmd.run_argv(
        binary=binary,
        tasks_dir=ds.root / "tasks",
        inputs=inputs,
        model=model,
        k=k,
        n_concurrent=int(run.model_settings(model.slug)["n_concurrent"]),
        job_name=job_name,
        jobs_dir=jobs_root,
    )
    wave = Wave(
        model_slug=model.slug,
        wave=idx,
        job_name=job_name,
        job_dir=str(jobs_root / job_name),
        kind=kind,
        inputs=inputs,
        k=k,
        argv=[keys.mask(a, env) for a in argv],
    )
    log(f"[{model.slug}] wave {idx} ({kind}): {len(inputs)} inputs x {k} attempts -> {job_name}")
    if dry_run:
        log("  $ " + keys.mask(" ".join(argv), env))
        return None
    run.add_wave(wave)
    rc = _run_wave(run, wave, argv, keys.harbor_env(env, model.output_token_max), log)
    log(f"  harbor exited {rc}")
    return wave


def _run_wave(run: Run, wave: Wave, argv: list[str], env: dict[str, str], log) -> int:
    log_path = run.dir / "logs" / f"{wave.job_name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log("  $ " + keys.mask(" ".join(argv), env))
    with open(log_path, "ab") as lf:
        proc = subprocess.Popen(argv, env=env, stdout=lf, stderr=subprocess.STDOUT, cwd=os.getcwd())
        wave.pid, wave.launched_at, wave.status = proc.pid, now(), "running"
        run.update_wave(wave)
        rc = proc.wait()
    wave.exit_code, wave.finished_at, wave.pid = rc, now(), None
    wave.status = "finished" if rc == 0 else "crashed"
    run.update_wave(wave)
    return rc


def resume_wave(run: Run, wave: Wave, model: ResolvedModel, filter_types: list[str], env: dict[str, str], log=print) -> int:
    binary = harbor_cmd.harbor_bin()
    argv = harbor_cmd.resume_argv(binary=binary, job_dir=Path(wave.job_dir), filter_error_types=filter_types)
    log(f"[{model.slug}] resuming {wave.job_name} (filters: {', '.join(filter_types) or 'none'})")
    return _run_wave(run, wave, argv, keys.harbor_env(env, model.output_token_max), log)


def waves_needing_resume(run: Run, slug: str, ledger_entries, infra_types: set[str]) -> list[Wave]:
    """Waves with trials that are unfinished or classified as re-run with a Harbor exception type to filter."""
    out = []
    for w in run.waves(slug):
        if not Path(w.job_dir).exists():
            continue
        mine = [e for e in ledger_entries if e.job_name == w.job_name]
        if any(e.status in ("unfinished", "infra", "timeout") for e in mine):
            out.append(w)
    return out


def tick(
    run: Run,
    ds: Dataset,
    model: ResolvedModel,
    inputs: list[str],
    env: dict[str, str],
    *,
    until_complete: bool,
    max_iterations: int = 20,
    log=print,
) -> dict:
    """Collect, resume, top up; repeat while progress is possible. Returns the per-input need table."""
    protocol = run.data["protocol"]
    per_input = int(protocol["rollouts"]["attempts_per_input"])
    max_reruns = int(protocol["rollouts"]["max_reruns_per_input"])
    infra_types = list(protocol["classification"]["harbor_infra_error_types"])
    if protocol["rollouts"]["timeout_policy"] == "rerun":
        infra_types = infra_types + ["AgentTimeoutError"]
    names = [t.name for t in inputs]
    for it in range(max_iterations):
        for w in run.waves(model.slug):
            if _alive(w.pid):
                raise RunError(f"wave {w.job_name} still has a live Harbor process (pid {w.pid}); wait or stop it first")
        entries = collect(run, ds, [model.slug], log=log)
        need = need_by_input(entries, names, model.slug, per_input, max_reruns)
        pending = {k: v for k, v in need.items() if v["need"] > 0 and not v["exhausted"]}
        log(
            f"[{model.slug}] {sum(1 for v in need.values() if v['need'] == 0)}/{len(names)} inputs complete, "
            f"{len(pending)} pending, {sum(1 for v in need.values() if v['exhausted'])} exhausted"
        )
        if not pending:
            break
        progressed = False
        for w in waves_needing_resume(run, model.slug, entries, set(infra_types)):
            resume_wave(run, w, model, infra_types, env, log)
            progressed = True
        if progressed:
            entries = collect(run, ds, [model.slug], log=log)
            need = need_by_input(entries, names, model.slug, per_input, max_reruns)
            pending = {k: v for k, v in need.items() if v["need"] > 0 and not v["exhausted"] and v["unfinished"] == 0}
            if not pending:
                if not until_complete:
                    break
                continue
        if not until_complete and it > 0:
            break
        # top-up waves grouped by how many attempts are still missing
        by_need: dict[int, list[str]] = {}
        for inp, v in pending.items():
            if v["unfinished"] == 0:
                by_need.setdefault(v["need"], []).append(inp)
        if not by_need:
            break
        for k_needed, inps in sorted(by_need.items()):
            start_wave(run, ds, model, sorted(inps), k_needed, "topup", env, log=log)
        if not until_complete:
            break
    entries = collect(run, ds, [model.slug], log=log)
    return need_by_input(entries, names, model.slug, per_input, max_reruns)


def status(run: Run, ds: Dataset) -> dict:
    protocol = run.data["protocol"]
    per_input = int(protocol["rollouts"]["attempts_per_input"])
    max_reruns = int(protocol["rollouts"]["max_reruns_per_input"])
    entries = load_ledger(run)
    out = {}
    for slug in run.data["models"]:
        inputs = sorted({w_in for w in run.waves(slug) for w_in in w.inputs})
        need = need_by_input(entries, inputs, slug, per_input, max_reruns)
        out[slug] = {
            "inputs": len(inputs),
            "complete": sum(1 for v in need.values() if v["need"] == 0),
            "pending": sum(1 for v in need.values() if v["need"] > 0 and not v["exhausted"]),
            "exhausted": sum(1 for v in need.values() if v["exhausted"]),
            "waves": [(w.job_name, w.status) for w in run.waves(slug)],
            "live": [w.job_name for w in run.waves(slug) if _alive(w.pid)],
        }
    return out


def python_for_grading() -> str:
    """The interpreter that runs the dataset's grader: this process's, which has the `grade` extra installed."""
    return sys.executable


def harbor_available() -> bool:
    return bool(os.environ.get("HARBOR_BIN")) or shutil.which("harbor") is not None
