"""`contractbench`: fetch, preflight, run, resume, status, collect, grade, metrics."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, harbor_cmd, keys, paths, report, runner, surgicality
from . import dataset as dsmod
from . import grade as grademod
from . import metrics as metricsmod
from . import models as modmod
from .collect import collect
from .ledger import Run, load_protocol


def _log(msg: str) -> None:
    print(msg, flush=True)


def _dataset(args) -> dsmod.Dataset:
    ds = dsmod.load(getattr(args, "dataset", None))
    problems = dsmod.validate(ds, deep=False)
    if problems:
        for p in problems[:10]:
            _log(f"dataset problem: {p}")
        raise SystemExit(f"{len(problems)} dataset problem(s); run `contractbench preflight` for the full list")
    return ds


# ---- commands ---------------------------------------------------------------------------------------------------- #
def cmd_fetch(args) -> int:
    dest = dsmod.fetch(Path(args.dest) if args.dest else None, revision=args.revision or None, patterns=args.pattern or None)
    ds = dsmod.index(dest)
    _log(f"fetched {len(ds.tasks)} tasks ({', '.join(ds.describe()['splits'])}) into {dest}")
    problems = dsmod.validate(ds, deep=True)
    for p in problems[:20]:
        _log(f"problem: {p}")
    return 1 if problems else 0


def cmd_preflight(args) -> int:
    ok = True
    env = keys.load_env()
    try:
        v = harbor_cmd.check_version()
        _log(f"harbor {'.'.join(map(str, v))}: ok")
    except (FileNotFoundError, RuntimeError) as e:
        _log(f"harbor: {e}")
        ok = False
    if not args.no_docker:
        docker = shutil.which("docker")
        if not docker:
            _log("docker: not on PATH")
            ok = False
        else:
            r = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
            if r.returncode == 0:
                _log(f"docker: server {r.stdout.strip()} reachable")
            else:
                _log(f"docker: not reachable ({(r.stderr or '').strip()[:120]}); set DOCKER_HOST if needed")
                ok = False
    try:
        ds = dsmod.load(args.dataset)
        problems = dsmod.validate(ds, deep=True)
        _log(f"dataset: {len(ds.tasks)} tasks at {ds.root}" + (" ok" if not problems else f", {len(problems)} problem(s)"))
        for p in problems[:20]:
            _log(f"  {p}")
        ok &= not problems
    except FileNotFoundError as e:
        _log(f"dataset: {e}")
        ds = None
        ok = False
    if not args.no_keys:
        protocol = load_protocol()
        missing = keys.missing_keys(env, keys.panel_requirements(protocol["grading"]["panel"]))
        _log("judge panel keys: " + ("ok" if not missing else f"missing {', '.join(missing)}"))
        ok &= not missing
        reg = modmod.load_registry()
        for m in args.model or []:
            rm = modmod.resolve(m, reg)
            miss = keys.missing_keys(env, modmod.required_env_keys(rm, reg))
            _log(
                f"model {rm.alias} ({rm.id}): "
                + ("keys ok" if not miss else f"missing {', '.join(miss)}")
                + (" [best effort settings]" if rm.best_effort else "")
            )
            ok &= not miss
    if args.build and ds is not None:
        inp = ds.inputs(args.set)[0].name
        jobs = paths.jobs_dir() / "_preflight"
        argv = harbor_cmd.nop_argv(
            binary=harbor_cmd.harbor_bin(), tasks_dir=ds.root / "tasks", input_task=inp, job_name="preflight-nop", jobs_dir=jobs
        )
        shutil.rmtree(jobs / "preflight-nop", ignore_errors=True)
        _log("building one task image and running the no-op agent: " + " ".join(argv))
        rc = subprocess.run(argv, env=env).returncode
        art = list((jobs / "preflight-nop").glob("*/artifacts/app/contract.docx"))
        _log(f"image build and no-op run: {'ok' if rc == 0 and art else 'FAILED'}")
        ok &= rc == 0 and bool(art)
    _log("preflight: " + ("all checks passed" if ok else "problems found"))
    return 0 if ok else 1


def _prepare(args, create: bool):
    env = keys.load_env()
    protocol = load_protocol()
    ds = _dataset(args)
    hv = ".".join(map(str, harbor_cmd.check_version()))
    if create:
        run = runner.ensure_run(args.run_id, ds, protocol, args.set, args.tasks or [], hv, getattr(args, "k", None))
    else:
        run = Run(args.run_id)
        if not run.exists():
            raise SystemExit(f"run {args.run_id} does not exist")
        run.load()
    return env, protocol, ds, run


def cmd_run(args) -> int:
    env, protocol, ds, run = _prepare(args, create=True)
    reg = modmod.load_registry()
    inputs = ds.inputs(run.data["set"], run.data["task_globs"])
    if not inputs:
        raise SystemExit("no inputs selected")
    k = int(run.data["protocol"]["rollouts"]["attempts_per_input"])
    for name in args.model:
        rm = modmod.resolve(name, reg, args.override, args.output_token_max, args.n)
        miss = keys.missing_keys(env, modmod.required_env_keys(rm, reg))
        if miss:
            raise SystemExit(f"{rm.alias}: missing {', '.join(miss)} (see .env.example)")
        runner.register_model(run, rm, rm.n_concurrent)
        _log(
            f"[{rm.alias}] {len(inputs)} inputs, {k} attempts each, set {run.data['set']}, model {rm.id}"
            + (" (best-effort harness settings)" if rm.best_effort else "")
        )
        if not run.waves(rm.slug):
            runner.start_wave(run, ds, rm, [t.name for t in inputs], k, "initial", env, dry_run=args.dry_run, log=_log)
            if args.dry_run:
                continue
        need = runner.tick(run, ds, rm, inputs, env, until_complete=args.until_complete, log=_log)
        _summarize(rm.alias, need)
    return 0


def cmd_resume(args) -> int:
    env, protocol, ds, run = _prepare(args, create=False)
    inputs = ds.inputs(run.data["set"], run.data["task_globs"])
    recorded = run.data["models"]
    wanted = []
    for name in args.model or list(recorded):
        slug = name if name in recorded else modmod.resolve(name, modmod.load_registry()).slug
        if slug not in recorded:
            raise SystemExit(f"{name} is not part of run {run.run_id} (models: {', '.join(recorded)})")
        wanted.append(slug)
    for slug in wanted:
        rm = modmod.from_settings(recorded[slug])
        if args.filter_error_type:
            for w in runner.waves_needing_resume(run, rm.slug, [], set()):
                runner.resume_wave(run, w, rm, args.filter_error_type, env, _log)
        need = runner.tick(run, ds, rm, inputs, env, until_complete=args.until_complete, log=_log)
        _summarize(rm.alias, need)
    return 0


def _summarize(alias: str, need: dict) -> None:
    done = sum(1 for v in need.values() if v["need"] == 0)
    exhausted = sum(1 for v in need.values() if v["exhausted"])
    _log(f"[{alias}] {done}/{len(need)} inputs complete, {exhausted} exhausted, {len(need) - done - exhausted} still pending")


def cmd_status(args) -> int:
    run = Run(args.run_id)
    if not run.exists():
        raise SystemExit(f"run {args.run_id} does not exist")
    run.load()
    ds = _dataset(args)
    for slug, s in runner.status(run, ds).items():
        _log(
            f"{slug}: {s['complete']}/{s['inputs']} complete, {s['pending']} pending, {s['exhausted']} exhausted; "
            f"waves {s['waves']}" + (f"; live {s['live']}" if s["live"] else "")
        )
    return 0


def cmd_collect(args) -> int:
    env, protocol, ds, run = _prepare(args, create=False)
    entries = collect(run, ds, args.model or None, keep_logs=args.keep_logs, log=_log)
    counted = sum(1 for e in entries if e.counted)
    _log(
        f"collected {len(entries)} trials: {counted} counted attempts, "
        f"{sum(1 for e in entries if e.attempt is None)} re-run trials archived -> {run.dir}"
    )
    return 0


def cmd_grade(args) -> int:
    env, protocol, ds, run = _prepare(args, create=False)
    g = protocol["grading"]
    panel = args.panel or g["panel"]
    jobs = grademod.plan(run, ds, args.model or None, args.tasks or None)
    if args.estimate:
        _log(json.dumps(grademod.estimate(jobs, ds), indent=1))
        return 0
    missing = keys.missing_keys(env, keys.panel_requirements(panel))
    if missing and not args.gate_only:
        raise SystemExit(f"judge panel keys missing: {', '.join(missing)}")
    if args.gate_only:
        n = len(collect(run, ds, args.model or None, run_gate=True, log=_log))
        _log(f"gate re-run on {n} trials")
        return 0
    if args.panel and args.panel != g["panel"]:
        _log(f"note: grading with a panel other than the benchmark's ({args.panel}); metrics will say so")
    counts = grademod.grade_all(
        run,
        ds,
        env,
        jobs,
        panel=panel,
        parallel=args.parallel or int(g["parallel_grades"]),
        retries=args.retries if args.retries is not None else int(g["retries"]),
        rubrics_per_call=int(g["rubrics_per_call"]),
        panel_parallel=bool(g["panel_parallel"]),
        log=_log,
    )
    return 0 if counts["failed"] == 0 else 1


def cmd_metrics(args) -> int:
    env, protocol, ds, run = _prepare(args, create=False)
    result = metricsmod.compute(run, ds)
    out = run.dir / "metrics"
    report.write_all(out, result)
    display = {r["slug"]: r["model"] for r in result["leaderboard"]}
    display["attorneys"] = "Attorneys"
    if not args.no_surgicality:
        surg = surgicality.compute(run, ds)
        surgicality.write(out, surg, display)
        result["surgicality"] = surg
        (out / "summary.json").write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")
    _log((out / "leaderboard.md").read_text())
    if args.eval_results:
        for p in report.eval_results_yaml(result, paths.HF_REPO_ID, out / "eval_results"):
            _log(f"wrote {p}")
    if args.compare:
        ok, lines = report.compare(result, Path(args.compare), args.tolerance)
        _log("\n".join(lines))
        _log("comparison: " + ("within tolerance" if ok else "DIFFERS"))
        return 0 if ok else 2
    return 0


# ---- parser ------------------------------------------------------------------------------------------------------ #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="contractbench", description=__doc__)
    ap.add_argument("--version", action="version", version=f"contractbench {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, run_id=True):
        if run_id:
            sp.add_argument("--run-id", required=True, help="name of the run (runs/<run-id>)")
        sp.add_argument("--dataset", help="dataset directory (default: $CONTRACTBENCH_DIR or ./benchmark)")

    sp = sub.add_parser("fetch", help="download the dataset from Hugging Face")
    sp.add_argument("--revision", default=paths.DEFAULT_REVISION, help="tag, branch or commit (default v1.0)")
    sp.add_argument("--dest", help="destination directory (default ./benchmark)")
    sp.add_argument("--pattern", action="append", help="glob of files to fetch (repeatable; default: everything)")
    sp.set_defaults(fn=cmd_fetch)

    sp = sub.add_parser("preflight", help="check Harbor, Docker, the dataset and the keys")
    common(sp, run_id=False)
    sp.add_argument("--model", action="append", help="model alias or provider/model id to check keys for")
    sp.add_argument("--set", default="public", choices=["public", "private", "full"])
    sp.add_argument("--build", action="store_true", help="build one task image and run the no-op agent")
    sp.add_argument("--no-docker", action="store_true")
    sp.add_argument("--no-keys", action="store_true")
    sp.set_defaults(fn=cmd_preflight)

    sp = sub.add_parser("run", help="run a model over the task inputs with Harbor")
    common(sp)
    sp.add_argument("--model", action="append", required=True, help="model alias or provider/model id (repeatable)")
    sp.add_argument("--set", default="public", choices=["public", "private", "full"])
    sp.add_argument("--tasks", action="append", help="glob on input task names (repeatable)")
    sp.add_argument("-k", type=int, help="attempts per input (default: protocol, 3)")
    sp.add_argument("-n", type=int, help="concurrent trials per model (default: registry, 4)")
    sp.add_argument("--override", action="append", help="opencode option override path.to.key=JSON (repeatable)")
    sp.add_argument("--output-token-max", type=int)
    sp.add_argument("--until-complete", action="store_true", help="resume and top up until every input has its attempts")
    sp.add_argument("--dry-run", action="store_true", help="print the Harbor command and exit")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("resume", help="resume an interrupted run (finished attempts are kept)")
    common(sp)
    sp.add_argument("--model", action="append", help="model alias (default: every model of the run)")
    sp.add_argument("--filter-error-type", action="append", help="extra Harbor error types to re-run first")
    sp.add_argument("--until-complete", action="store_true")
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("status", help="show per-model progress")
    common(sp)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("collect", help="collect Harbor job trees into runs/<run-id>")
    common(sp)
    sp.add_argument("--model", action="append")
    sp.add_argument("--keep-logs", action="store_true", help="keep the agent's event log with each attempt")
    sp.set_defaults(fn=cmd_collect)

    sp = sub.add_parser("grade", help="grade collected rollouts with the judge panel")
    common(sp)
    sp.add_argument("--model", action="append")
    sp.add_argument("--tasks", action="append", help="glob on input task names")
    sp.add_argument("--parallel", type=int)
    sp.add_argument("--retries", type=int)
    sp.add_argument("--panel", help="override the judge panel (comma-separated litellm ids)")
    sp.add_argument("--gate-only", action="store_true", help="re-run the validity gate only (no judge calls)")
    sp.add_argument("--estimate", action="store_true", help="count grades and judge input without calling judges")
    sp.set_defaults(fn=cmd_grade)

    sp = sub.add_parser("metrics", help="compute scores, the leaderboard and the surgicality tables")
    common(sp)
    sp.add_argument("--compare", help="published leaderboard.csv to compare against")
    sp.add_argument("--tolerance", type=float, default=5e-7)
    sp.add_argument("--eval-results", action="store_true", help="also write Hub .eval_results YAML per model")
    sp.add_argument("--no-surgicality", action="store_true")
    sp.set_defaults(fn=cmd_metrics)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (runner.RunError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
