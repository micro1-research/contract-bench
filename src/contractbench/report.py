"""Writers for the metrics outputs and the comparison against a published leaderboard."""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from .metrics import EXTRA_COLUMNS, LEADERBOARD_COLUMNS


def _f(x, nd: int = 6) -> str:
    return "" if x is None else f"{x:.{nd}f}"


def _pct(x) -> str:
    return "" if x is None else f"{100 * x:.1f}%"


def write_all(out_dir: Path, result: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = result["leaderboard"]
    with open(out_dir / "leaderboard.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(LEADERBOARD_COLUMNS + EXTRA_COLUMNS)
        for r in rows:
            w.writerow(
                [r["model"]]
                + [_f(r[c]) for c in LEADERBOARD_COLUMNS[1:]]
                + [
                    _f(r["turn1_score"]),
                    _f(r["turn2_score"]),
                    _f(r["spread_points"], 2),
                    r["n_grades"],
                    r["n_rollouts"],
                    r["n_counted"],
                    _f(r["completion_rate_public"]),
                    _f(r["completion_rate_private"]),
                    int(r["best_effort"]),
                ]
            )
    md = [
        "# ContractBench leaderboard",
        "",
        f"Run `{result['run_id']}`; judge panel {result['panel']}. Scores are weighted rubric pass rates on completed "
        "rollouts; blank cells mean the run does not cover that task set.",
        "",
    ]
    if not result.get("dataset_complete", True):
        md += [
            "Subset run: the dataset directory holds part of the task set, so the split columns stay blank; the "
            "completion rate and the per-turn scores cover the inputs that were run.",
            "",
        ]
    md += [
        "| Model | Private score | Public score | Full benchmark | Completion rate | Turn 1 | Turn 2 | Spread (pts) | Inputs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        name = r["model"] + (" *" if r["best_effort"] else "")
        comp = r["completion_rate"] if r["completion_rate"] is not None else r["completion_rate_run"]
        md.append(
            f"| {name} | {_pct(r['private_score'])} | {_pct(r['public_score'])} | {_pct(r['full_score'])} | "
            f"{_pct(comp)} | {_pct(r['turn1_score'])} | {_pct(r['turn2_score'])} | {_f(r['spread_points'], 1)} | "
            f"{r['inputs_run']}/{r['inputs_in_set']} |"
        )
    if any(r["best_effort"] for r in rows):
        md += ["", "\\* harness settings reproduced on a best-effort basis (see configs/models.toml)."]
    (out_dir / "leaderboard.md").write_text("\n".join(md) + "\n")
    with open(out_dir / "slices.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "slice", "value", "weighted_pass_rate"])
        for slug, groups in result["slices"].items():
            name = next((r["model"] for r in rows if r["slug"] == slug), slug)
            for key, vals in groups.items():
                for v, rate in sorted(vals.items()):
                    w.writerow([name, key, v, _f(rate)])
    with open(out_dir / "failures.csv", "w", newline="") as f:
        keys = sorted({k for d in result["failures"].values() for k in d})
        w = csv.writer(f)
        w.writerow(["model"] + keys)
        for slug, d in result["failures"].items():
            name = next((r["model"] for r in rows if r["slug"] == slug), slug)
            w.writerow([name] + [d.get(k, 0) for k in keys])
    with open(out_dir / "cost.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "mean_cost_usd", "mean_agent_minutes", "mean_output_tokens"])
        for slug, d in result["cost"].items():
            name = next((r["model"] for r in rows if r["slug"] == slug), slug)
            w.writerow([name, _f(d["mean_cost_usd"], 4), _f(d["mean_agent_minutes"], 2), _f(d["mean_output_tokens"], 0)])
    (out_dir / "summary.json").write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")


def compare(result: dict, published_csv: Path, tolerance: float = 5e-7) -> tuple[bool, list[str]]:
    """Per-model deltas against a published leaderboard.csv (model, private_score, public_score, full_score,
    completion_rate). Only the columns the run fills are compared."""
    pub = {r["model"]: r for r in csv.DictReader(open(published_csv, newline=""))}
    lines, ok = [], True
    for r in result["leaderboard"]:
        p = pub.get(r["model"])
        if p is None:
            lines.append(f"{r['model']}: not in the published leaderboard")
            continue
        for col in ("private_score", "public_score", "full_score", "completion_rate"):
            if r.get(col) is None or not p.get(col):
                continue
            d = r[col] - float(p[col])
            flag = "" if abs(d) <= tolerance else "  <-- differs"
            ok &= abs(d) <= tolerance
            lines.append(f"{r['model']:<18} {col:<16} run {r[col]:.6f}  published {float(p[col]):.6f}  delta {d:+.6f}{flag}")
    return ok, lines


def eval_results_yaml(result: dict, dataset_repo: str, out_dir: Path, date: str | None = None) -> list[Path]:
    """One Hub `.eval_results` file per model, in the dataset's benchmark format (values on a 0 to 100 scale)."""
    date = date or dt.date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    tasks = (
        ("contractbench_private", "private_score", "Private set (140 held-out tasks)"),
        ("contractbench_public", "public_score", "Public set (210 tasks)"),
        ("contractbench_full", "full_score", "Full benchmark (350 tasks)"),
    )
    for r in result["leaderboard"]:
        entries = []
        for task_id, col, label in tasks:
            if r.get(col) is None:
                continue
            entries.append(f"""- dataset:
    id: {dataset_repo}
    task_id: {task_id}
  value: {100 * r[col]:.1f}
  date: "{date}"
  notes: "{label}; weighted rubric pass rate (0 to 100) on completed rollouts, three rollouts per task, three-judge panel"
""")
        if entries:
            p = out_dir / f"{r['slug']}.yaml"
            p.write_text("".join(entries))
            written.append(p)
    return written
