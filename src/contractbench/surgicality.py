"""Surgicality and edit volume: how a redline is shaped, independent of what the judges thought of it.

surgicality(actor) = mean over the Turn 1 contracts of the actor's pooled inline share on that contract
(sum of inline events / sum of events over the actor's redlines of the contract). Models are profiled from their
counted, successful rollouts of the `-t1-a` input; attorneys from the scenario's five reference redlines.

The edit-volume table compares attorneys and models per Turn 1 contract (n = contracts) on redlined passages
(clusters), edits (events), average edit length, total edited characters and inline share, each with a 95% t
confidence interval over contracts; a Turn 2 row compares edits per Turn 2 task.
"""

from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from pathlib import Path
from statistics import mean

from .dataset import Dataset
from .docx_events import DocProfile, profile, reference_author
from .ledger import Run

MODEL_AUTHOR = "Reviewing Counsel"
METRICS = ("clusters", "events", "avg_edit_len", "total_chars", "inline_share")


def t975(df: int) -> float:
    """Two-sided 95% Student t quantile (series expansion, within 0.001 of the exact value for df >= 5)."""
    z = 1.959963985
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
    )


def mean_ci(xs) -> tuple:
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return (xs[0] if xs else None, None, None)
    m = sum(xs) / len(xs)
    h = t975(len(xs) - 1) * st.stdev(xs) / len(xs) ** 0.5
    return (m, m - h, m + h)


def agg(profiles: list[DocProfile], key: str) -> float | None:
    """A set of redlines' value: edit length and inline share weighted by events, the others plain means."""
    if key in ("avg_edit_len", "inline_share"):
        ps = [p for p in profiles if p.events]
        ev = sum(p.events for p in ps)
        if not ev:
            return None
        return (sum(p.avg_edit_len * p.events for p in ps) if key == "avg_edit_len" else sum(p.inline for p in ps)) / ev
    vals = [getattr(p, key) for p in profiles]
    return mean(vals) if vals else None


def model_profiles(run: Run, ds: Dataset) -> dict[str, dict[str, list[DocProfile]]]:
    """slug -> input task -> one profile per counted successful attempt."""
    out: dict[str, dict[str, list[DocProfile]]] = defaultdict(lambda: defaultdict(list))
    rollouts = run.dir / "rollouts"
    if not rollouts.exists():
        return out
    for slug_dir in sorted(p for p in rollouts.iterdir() if p.is_dir()):
        for input_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            if input_dir.name not in ds.tasks:
                continue
            for att in sorted(p for p in input_dir.glob("attempt*") if p.is_dir()):
                tj = att / "trial.json"
                if not tj.exists():
                    continue
                t = json.loads(tj.read_text())
                if not (t.get("counted") and t.get("status") == "success" and (att / "contract.docx").exists()):
                    continue
                pr = profile(att / "contract.docx", MODEL_AUTHOR)
                if pr is not None:
                    out[slug_dir.name][input_dir.name].append(pr)
    return out


def reference_profiles(ds: Dataset) -> dict[str, DocProfile]:
    """task -> profile of the attorney's reference redline (author detected against the task's input)."""
    out = {}
    for t in ds.tasks.values():
        ref = ds.reference(t.name)
        if ref is None:
            continue
        author = reference_author(ref, t.input_docx)
        pr = profile(ref, author if author else None)
        if pr is not None:
            out[t.name] = pr
    return out


def compute(run: Run, ds: Dataset) -> dict:
    models = model_profiles(run, ds)
    refs = reference_profiles(ds)
    scen = ds.scenarios()
    turn1_inputs = {s: f"{s}-t1-a" for s, ts in scen.items() if any(t.turn == 1 for t in ts)}
    slugs = sorted(models)
    per_contract_inline: dict[str, list[float]] = defaultdict(list)
    att_s: dict[str, list[float]] = defaultdict(list)
    mod_s: dict[str, list[float]] = defaultdict(list)
    for s, inp in sorted(turn1_inputs.items()):
        ref_ps = [refs[t.name] for t in scen[s] if t.turn == 1 and t.name in refs]
        per_model: dict[str, list[float]] = defaultdict(list)
        for slug in slugs:
            ps = models[slug].get(inp, [])
            for k in METRICS:
                v = agg(ps, k) if ps else None
                if v is not None:
                    per_model[k].append(v)
                    if k == "inline_share":
                        per_contract_inline[slug].append(v)
        for k in METRICS:
            a = agg(ref_ps, k) if ref_ps else None
            if a is not None and per_model[k]:
                att_s[k].append(a)
                mod_s[k].append(mean(per_model[k]))
                if k == "inline_share":
                    per_contract_inline["attorneys"].append(a)
    vs = {
        k: {
            "attorneys": mean_ci(att_s[k]),
            "models": mean_ci(mod_s[k]),
            "diff": mean_ci([m - a for a, m in zip(att_s[k], mod_s[k], strict=True)]),
            "n": len(att_s[k]),
        }
        for k in METRICS
    }
    t2_att, t2_mod = [], []
    for t in sorted((x for x in ds.tasks.values() if x.turn == 2), key=lambda x: x.name):
        r = refs.get(t.name)
        per_model = [mean([p.events for p in models[slug][t.name]]) for slug in slugs if models[slug].get(t.name)]
        if r is not None and per_model:
            t2_att.append(r.events)
            t2_mod.append(mean(per_model))
    vs["events_t2"] = {
        "attorneys": mean_ci(t2_att),
        "models": mean_ci(t2_mod),
        "diff": mean_ci([m - a for a, m in zip(t2_att, t2_mod, strict=True)]),
        "n": len(t2_att),
    }
    surg = {a: {"score": mean(v), "n": len(v)} for a, v in per_contract_inline.items() if v}
    return {"threshold": 0.30, "model_author": MODEL_AUTHOR, "n_references": len(refs), "surgicality": surg, "edit_volume": vs}


def write(out_dir: Path, result: dict, display: dict[str, str]) -> None:
    import csv

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "surgicality.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["actor", "surgicality", "n_contracts"])
        rows = sorted(result["surgicality"].items(), key=lambda kv: (kv[0] != "attorneys", -kv[1]["score"]))
        for actor, v in rows:
            w.writerow([display.get(actor, actor), f"{v['score']:.4f}", v["n"]])
    labels = {
        "clusters": "Redlined passages",
        "events": "Edits",
        "avg_edit_len": "Average edit length (chars)",
        "total_chars": "Total edited characters",
        "inline_share": "Inline share",
        "events_t2": "Turn 2 edits",
    }
    with open(out_dir / "edit_volume.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "metric",
                "attorneys",
                "attorneys_ci_low",
                "attorneys_ci_high",
                "models",
                "models_ci_low",
                "models_ci_high",
                "difference",
                "n",
            ]
        )
        for k, v in result["edit_volume"].items():
            a, m, d = v["attorneys"], v["models"], v["diff"]
            w.writerow([labels[k]] + [_fmt(x) for x in (a[0], a[1], a[2], m[0], m[1], m[2], d[0])] + [v["n"]])


def _fmt(x) -> str:
    return "" if x is None else f"{x:.4f}"
