"""Scores and the leaderboard, computed from a run's collected rollouts and grades.

Formulas (docs/scoring.md states them in full):
- grade score = the grader's `score.weighted`: earned rubric weight / total rubric weight, with every rubric item
  decided by a strict majority of the judge panel;
- model score on a task set = mean of grade scores over all scored grades of tasks in the set, pooled across
  turns (a Turn 1 rollout contributes one grade per sibling rubric set, a Turn 2 rollout one grade);
- completion rate = counted attempts that produced a gradeable redline / counted attempts;
- slices = weighted pass rate, sum(weight x pass) / sum(weight), over positive-weight rubric items;
- rubric-set spread = mean over scenarios of the mean, over Turn 1 attempts graded on all five sibling sets, of
  the population standard deviation of the attempt's five grade scores.
"""

from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from .classify import COUNTED_FAILURES
from .dataset import Dataset
from .ledger import Run

LEADERBOARD_COLUMNS = ["model", "private_score", "public_score", "full_score", "completion_rate"]
EXTRA_COLUMNS = [
    "turn1_score",
    "turn2_score",
    "spread_points",
    "n_grades",
    "n_rollouts",
    "n_counted",
    "completion_rate_public",
    "completion_rate_private",
    "best_effort",
]


@dataclass
class Attempt:
    model_slug: str
    input_task: str
    scenario: str
    turn: int
    split: str
    attempt: int
    counted: bool
    status: str
    cost_usd: float | None
    tokens: dict
    agent_exec_sec: float | None


@dataclass
class Grade:
    model_slug: str
    input_task: str
    target_task: str
    attempt: int
    scenario: str
    turn: int
    split: str
    weighted: float
    gate_passed: bool
    survivors: list
    judge_mode: str
    degraded: bool
    items: list[dict] = field(default_factory=list)  # per_rubric joined with rubric attributes
    diagnostics: dict = field(default_factory=dict)


def _load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def load_attempts(run: Run, ds: Dataset) -> list[Attempt]:
    out = []
    rollouts = run.dir / "rollouts"
    if not rollouts.exists():
        return out
    for slug_dir in sorted(p for p in rollouts.iterdir() if p.is_dir()):
        for input_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            if input_dir.name not in ds.tasks:
                continue
            ti = ds.tasks[input_dir.name]
            for att in sorted(p for p in input_dir.glob("attempt*") if p.is_dir()):
                t = _load_json(att / "trial.json")
                if not t:
                    continue
                out.append(
                    Attempt(
                        slug_dir.name,
                        input_dir.name,
                        ti.scenario,
                        ti.turn,
                        ti.split,
                        int(t["attempt"]),
                        bool(t.get("counted")),
                        t["status"],
                        t.get("cost_usd"),
                        t.get("tokens") or {},
                        t.get("agent_exec_sec"),
                    )
                )
    return out


def load_grades(run: Run, ds: Dataset) -> list[Grade]:
    out = []
    grades = run.dir / "grades"
    if not grades.exists():
        return out
    rubric_cache: dict[str, dict[str, dict]] = {}
    for slug_dir in sorted(p for p in grades.iterdir() if p.is_dir()):
        for target_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            if target_dir.name not in ds.tasks:
                continue
            ti = ds.tasks[target_dir.name]
            if target_dir.name not in rubric_cache:
                rubric_cache[target_dir.name] = {r["id"]: r for r in ds.rubrics(target_dir.name)["rubrics"]}
            rub = rubric_cache[target_dir.name]
            for att in sorted(p for p in target_dir.glob("attempt*") if p.is_dir()):
                g = _load_json(att / "grade.json")
                m = _load_json(att / "grade_meta.json") or {}
                if not g:
                    continue
                score = g.get("score") or {}
                items = []
                for pr in score.get("per_rubric") or []:
                    r = rub.get(pr.get("rubric_id"), {})
                    items.append(
                        {
                            "rubric_id": pr.get("rubric_id"),
                            "verdict": pr.get("verdict"),
                            "weight": float(pr.get("weight") or 0),
                            "is_penalty": bool(pr.get("is_penalty")),
                            "required_action": r.get("required_action"),
                            "sub_action": r.get("sub_action"),
                            "priority": r.get("priority"),
                            "evaluation_dimension": r.get("evaluation_dimension"),
                        }
                    )
                out.append(
                    Grade(
                        slug_dir.name,
                        m.get("input_task") or ti.input_task,
                        target_dir.name,
                        int(att.name.replace("attempt", "")),
                        ti.scenario,
                        ti.turn,
                        ti.split,
                        float(score.get("weighted") or 0.0),
                        bool((g.get("gate") or {}).get("passed")),
                        list(g.get("survivors") or []),
                        str(g.get("judge_mode") or ""),
                        bool(m.get("degraded", False)),
                        items,
                        {
                            k: score.get(k)
                            for k in (
                                "redlines",
                                "median_insertion_chars",
                                "touched_paragraphs",
                                "comments_added",
                                "n_escalations",
                                "n_escalate_rubrics",
                            )
                        },
                    )
                )
    return out


def scored(grades: list[Grade], attempts: list[Attempt]) -> list[Grade]:
    """Grades of counted, successful attempts whose gate passed."""
    ok = {(a.model_slug, a.input_task, a.attempt) for a in attempts if a.counted and a.status == "success"}
    return [g for g in grades if g.gate_passed and (g.model_slug, g.input_task, g.attempt) in ok]


def _mean_or_none(xs):
    xs = list(xs)
    return mean(xs) if xs else None


def model_score(gs: list[Grade], splits: set[str] | None = None, turn: int | None = None) -> float | None:
    sel = [g for g in gs if (splits is None or g.split in splits) and (turn is None or g.turn == turn)]
    return _mean_or_none(g.weighted for g in sel)


def completion(attempts: list[Attempt], splits: set[str] | None = None) -> float | None:
    sel = [a for a in attempts if a.counted and (splits is None or a.split in splits)]
    return (sum(1 for a in sel if a.status == "success") / len(sel)) if sel else None


def spread_points(gs: list[Grade], ds: Dataset) -> float | None:
    """Mean over scenarios of the mean pstdev of a Turn 1 attempt's five sibling grades, in points."""
    by_attempt: dict[tuple[str, int], list[float]] = defaultdict(list)
    for g in gs:
        if g.turn == 1:
            by_attempt[(g.input_task, g.attempt)].append(g.weighted)
    by_scenario: dict[str, list[float]] = defaultdict(list)
    for (inp, _), vals in by_attempt.items():
        if len(vals) == 5:
            by_scenario[ds.tasks[inp].scenario].append(st.pstdev(vals))
    per = [mean(v) for v in by_scenario.values() if v]
    return 100 * mean(per) if per else None


def slice_rates(gs: list[Grade], key: str) -> dict[str, float]:
    num: dict[str, float] = defaultdict(float)
    den: dict[str, float] = defaultdict(float)
    for g in gs:
        for it in g.items:
            if it["is_penalty"] or it["weight"] <= 0:
                continue
            v = it.get(key)
            if v is None:
                continue
            den[v] += it["weight"]
            if it["verdict"] == "PASS":
                num[v] += it["weight"]
    return {k: num[k] / den[k] for k in den if den[k]}


def task_slice_rates(gs: list[Grade], ds: Dataset, attr: str) -> dict[str, float]:
    num: dict[str, float] = defaultdict(float)
    den: dict[str, float] = defaultdict(float)
    for g in gs:
        v = getattr(ds.tasks[g.target_task], attr, None)
        if not v:
            continue
        for it in g.items:
            if it["is_penalty"] or it["weight"] <= 0:
                continue
            den[v] += it["weight"]
            if it["verdict"] == "PASS":
                num[v] += it["weight"]
    return {k: num[k] / den[k] for k in den if den[k]}


def covers(ds: Dataset, attempts: list[Attempt], splits: set[str]) -> bool:
    """True when every input of the dataset's tasks in `splits` has at least one counted attempt for the model."""
    wanted = {t.input_task for t in ds.tasks.values() if t.split in splits}
    have = {a.input_task for a in attempts if a.counted}
    return bool(wanted) and wanted <= have


def compute(run: Run, ds: Dataset) -> dict:
    attempts = load_attempts(run, ds)
    grades = load_grades(run, ds)
    sg = scored(grades, attempts)
    slugs = sorted({a.model_slug for a in attempts} | {g.model_slug for g in grades})
    settings = run.data.get("models", {})
    all_splits = {t.split for t in ds.tasks.values()}
    complete = ds.is_complete()  # a subset of the dataset never fills a split column
    rows = []
    slices: dict[str, dict] = {}
    failures: dict[str, dict] = {}
    cost: dict[str, dict] = {}
    for s in slugs:
        A = [a for a in attempts if a.model_slug == s]
        G = [g for g in sg if g.model_slug == s]
        row = {"model": settings.get(s, {}).get("display", s), "slug": s}
        for col, split in (("private_score", {"private"}), ("public_score", {"public"}), ("full_score", all_splits)):
            full_ok = complete and split <= all_splits and covers(ds, A, split)
            if col == "full_score":
                full_ok = full_ok and all_splits >= {"public", "private"}
            row[col] = model_score(G, split) if full_ok else None
        row["completion_rate"] = completion(A, all_splits) if complete and covers(ds, A, all_splits) else None
        row["completion_rate_public"] = completion(A, {"public"}) if complete and covers(ds, A, {"public"}) else None
        row["completion_rate_private"] = completion(A, {"private"}) if complete and covers(ds, A, {"private"}) else None
        row["completion_rate_run"] = completion(A)  # over the inputs this run actually covers
        row["inputs_run"] = len({a.input_task for a in A if a.counted})
        row["inputs_in_set"] = len(ds.inputs(run.data.get("set", "all")))
        row["turn1_score"], row["turn2_score"] = model_score(G, turn=1), model_score(G, turn=2)
        row["spread_points"] = spread_points(G, ds)
        row["n_grades"], row["n_rollouts"] = len(G), len({(a.input_task, a.attempt) for a in A if a.counted})
        row["n_counted"] = sum(1 for a in A if a.counted)
        row["best_effort"] = bool(settings.get(s, {}).get("best_effort", False))
        row["degraded_grades"] = sum(1 for g in G if g.degraded)
        rows.append(row)
        slices[s] = {
            "required_action": slice_rates(G, "required_action"),
            "sub_action": slice_rates(G, "sub_action"),
            "priority": slice_rates(G, "priority"),
            "evaluation_dimension": slice_rates(G, "evaluation_dimension"),
            "agreement_type": task_slice_rates(G, ds, "agreement_type"),
            "playbook": task_slice_rates(G, ds, "playbook"),
            "jurisdiction": task_slice_rates(G, ds, "jurisdiction"),
        }
        counted = [a for a in A if a.counted]
        failures[s] = {st_: sum(1 for a in counted if a.status == st_) for st_ in ("success",) + COUNTED_FAILURES}
        failures[s]["reruns_archived"] = _count_reruns(run, s)
        done = [a for a in counted if a.status == "success"]
        cost[s] = {
            "mean_cost_usd": _mean_or_none(a.cost_usd for a in done if a.cost_usd is not None),
            "mean_agent_minutes": _mean_or_none(a.agent_exec_sec / 60 for a in done if a.agent_exec_sec),
            "mean_output_tokens": _mean_or_none(a.tokens.get("output") for a in done if a.tokens.get("output")),
        }
    rows.sort(
        key=lambda r: (
            -(
                r["private_score"]
                if r["private_score"] is not None
                else r["public_score"]
                if r["public_score"] is not None
                else -1
            )
        )
    )
    return {
        "run_id": run.run_id,
        "dataset": run.data.get("dataset"),
        "panel": run.data["protocol"]["grading"]["panel"],
        "dataset_complete": complete,
        "leaderboard": rows,
        "slices": slices,
        "failures": failures,
        "cost": cost,
        "n_attempts": len(attempts),
        "n_grades_loaded": len(grades),
        "n_grades_scored": len(sg),
    }


def _count_reruns(run: Run, slug: str) -> int:
    base = run.dir / "rollouts" / slug
    return sum(1 for p in base.glob("*/rerun/*") if p.is_dir()) if base.exists() else 0
