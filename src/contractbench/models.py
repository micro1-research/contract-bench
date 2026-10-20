"""The model registry (`configs/models.toml`) and how a model's settings become Harbor agent arguments.

A resolved model carries everything the runner passes to Harbor: the `provider/model` id, the opencode options
(rendered as an `opencode_config` JSON document), extra agent kwargs such as `variant`, and the output token cap.
The resolved settings are frozen into a run's `run.json` so later invocations can detect drift.
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths


@dataclass
class ResolvedModel:
    alias: str
    id: str
    provider: str
    model_id: str
    display: str
    opencode_options: dict = field(default_factory=dict)
    agent_kwargs: dict = field(default_factory=dict)
    output_token_max: int = 128000
    n_concurrent: int = 4
    agent_setup_timeout_multiplier: float = 3.0
    best_effort: bool = False
    notes: str = ""

    @property
    def slug(self) -> str:
        return slugify(self.alias)

    def opencode_config(self) -> dict:
        """The document merged into opencode.json by Harbor's opencode agent."""
        cfg: dict = {"permission": {"external_directory": "allow"}}
        if self.opencode_options:
            cfg["provider"] = {self.provider: {"models": {self.model_id: {"options": copy.deepcopy(self.opencode_options)}}}}
        return cfg

    def settings(self) -> dict:
        """What run.json records; compared on every later invocation."""
        d = asdict(self)
        d["opencode_config"] = self.opencode_config()
        return d


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-")


def load_registry(path: Path | None = None) -> dict:
    p = path or paths.configs_dir() / "models.toml"
    with open(p, "rb") as f:
        return tomllib.load(f)


def split_id(model_id: str) -> tuple[str, str]:
    if "/" not in model_id:
        raise ValueError(f"model id {model_id!r} must be provider/model (for example anthropic/claude-opus-5-5)")
    provider, rest = model_id.split("/", 1)
    return provider, rest


def resolve(
    name: str,
    registry: dict | None = None,
    overrides: list[str] | None = None,
    output_token_max: int | None = None,
    n_concurrent: int | None = None,
) -> ResolvedModel:
    """Resolve a registry alias or a raw provider/model id into the settings Harbor receives."""
    reg = registry or load_registry()
    defaults = reg.get("defaults", {})
    providers = reg.get("providers", {})
    models = reg.get("models", {})
    if name in models:
        entry = models[name]
        alias = name
        model_id_full = entry["id"]
    else:
        matches = [a for a, e in models.items() if e.get("id") == name]
        if matches:
            alias = matches[0]
            entry = models[alias]
            model_id_full = entry["id"]
        else:
            alias = slugify(name.split("/", 1)[-1])
            entry = {}
            model_id_full = name
    provider, model_id = split_id(model_id_full)
    pdefaults = providers.get(provider, {})
    rm = ResolvedModel(
        alias=alias,
        id=model_id_full,
        provider=provider,
        model_id=model_id,
        display=entry.get("display", model_id),
        opencode_options=copy.deepcopy(entry.get("opencode_options", {})),
        agent_kwargs=copy.deepcopy(entry.get("agent_kwargs", {})),
        output_token_max=int(
            output_token_max
            or entry.get("output_token_max")
            or pdefaults.get("output_token_max")
            or defaults.get("output_token_max", 128000)
        ),
        n_concurrent=int(n_concurrent or entry.get("n_concurrent") or defaults.get("n_concurrent", 4)),
        agent_setup_timeout_multiplier=float(
            entry.get("agent_setup_timeout_multiplier") or defaults.get("agent_setup_timeout_multiplier", 3.0)
        ),
        best_effort=bool(entry.get("best_effort", False)),
        notes=str(entry.get("notes", "")),
    )
    for ov in overrides or []:
        apply_override(rm.opencode_options, ov)
    return rm


def apply_override(options: dict, spec: str) -> None:
    """`a.b.c=<JSON>` sets a nested key of the opencode options (creating intermediate tables)."""
    if "=" not in spec:
        raise ValueError(f"override {spec!r} must look like path.to.key=JSON")
    path, raw = spec.split("=", 1)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw  # a bare string
    node = options
    keys = [k for k in path.split(".") if k]
    if not keys:
        raise ValueError(f"override {spec!r} has an empty key path")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            raise ValueError(f"override {spec!r}: {k} is not a table")
    node[keys[-1]] = value


def required_env_keys(rm: ResolvedModel, registry: dict | None = None) -> list[str]:
    reg = registry or load_registry()
    return list(reg.get("providers", {}).get(rm.provider, {}).get("env_keys", []))


def settings_diff(recorded: dict, current: dict) -> list[str]:
    """Human-readable differences between a run's recorded model settings and a fresh resolution."""
    out = []
    keys = sorted(set(recorded) | set(current))
    for k in keys:
        if k in ("notes", "display"):
            continue
        if recorded.get(k) != current.get(k):
            out.append(
                f"{k}: recorded {json.dumps(recorded.get(k), sort_keys=True)} vs now {json.dumps(current.get(k), sort_keys=True)}"
            )
    return out


def from_settings(settings: dict) -> ResolvedModel:
    """Rebuild a resolved model from the settings a run recorded (so resume never depends on the registry)."""
    fields = {k: settings[k] for k in ResolvedModel.__dataclass_fields__ if k in settings}
    return ResolvedModel(**fields)
