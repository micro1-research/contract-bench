# ContractBench

ContractBench measures how well an AI agent reviews and redlines a commercial contract the way an in-house attorney
would: deciding where to intervene, what to change, what to leave alone, when to ask the counterparty a question,
and when to escalate a point to its own side instead of deciding it. Every task is a real Word document, a set of
grounding documents (the represented side's commercial context and, in most scenarios, its playbook) and a brief.
The agent works directly in the `.docx` with tracked changes and comments, and is graded against pass-or-fail
criteria written by the attorney who reviewed the same contract.

This repository is the runner: it downloads the task dataset from Hugging Face, runs a model over the tasks with
[Harbor](https://github.com/laude-institute/harbor), grades the redlines with the benchmark's judge panel and
computes the leaderboard and surgicality metrics. The tasks themselves live in the dataset
[`micro1-inc/ContractBench`](https://huggingface.co/datasets/micro1-inc/ContractBench).

## Quick start

Prerequisites: Python 3.11 or later, Docker (or Colima), [Harbor](https://github.com/laude-institute/harbor) 0.18
or later (`uv tool install harbor`), and Git LFS if you clone the dataset with git rather than fetching it.

```bash
pip install -e ".[grade]"            # the runner plus what the dataset's grader imports (python-docx, lxml, litellm)
cp .env.example .env                 # fill in the provider keys you need
contractbench fetch                  # downloads the dataset (tag v1.0) into ./benchmark
contractbench preflight --model opus-5-5 --build
```

`preflight` checks Harbor, Docker, the dataset (every document opens, no Git LFS pointer files, the Turn 1 siblings
share one input) and the keys; `--build` builds one task image and runs the no-op agent through it.

## Reproducing

```bash
contractbench run    --run-id oct --model opus-5-5 --set public --until-complete
contractbench grade  --run-id oct
contractbench metrics --run-id oct --compare benchmark/leaderboard.csv
```

- `run` executes one Harbor job per model over the set's inputs (one per Turn 1 scenario, every Turn 2 task), three
  attempts per input, with the harness settings recorded in `configs/models.toml` for the leaderboard models (any
  `provider/model` id works; see `--override`). If the process dies, `contractbench resume --run-id oct` continues
  the same job: finished attempts are kept, interrupted trials are re-run. Infrastructure failures and timeouts are
  re-run a bounded number of times; a model's own failures (an unchanged document, a redline that fails the validity
  gate) count as attempts and are never replaced.
- `grade` runs the dataset's own grader on every counted attempt against every rubric set that applies, with the
  three-judge panel, in parallel, skipping grades that already exist.
- `metrics` writes `runs/oct/metrics/`: `leaderboard.csv` and `.md`, per-turn scores, slices by action, priority,
  dimension, agreement type, playbook and jurisdiction, the spread across rubric sets, failure counts, cost, and the
  surgicality and edit-volume tables. `--compare` prints the deltas against a published leaderboard.

Harbor's job trees under `jobs/` are ephemeral; everything the metrics need is collected under `runs/<run-id>/`.
Runs are not deterministic (agent sampling, judge models), so expect small deltas between reproductions.

## Metrics

- **Task score** = earned rubric weight / total rubric weight, where High, Medium and Low items weigh 3, 2 and 1.
  Each rubric item is judged Pass or Fail by a strict majority of three judge models from different providers
  (Claude Opus 4.8, GPT-5.5, Gemini 3.5 Flash).
- **Model score** on a task set = the mean task score over all graded rollouts of the set's tasks, pooled across
  turns; three rollouts per input, each Turn 1 rollout graded against the scenario's five rubric sets.
- **Completion rate** = counted rollouts that produced a gradeable redline / counted rollouts.
- **Spread across rubric sets** = the standard deviation of a Turn 1 rollout's five scores, averaged per scenario and
  then over scenarios.
- **Surgicality** = the share of a redline's tracked-change events that are inline edits (smaller than 30% of the
  paragraph's untouched text), pooled per contract and averaged over the Turn 1 contracts; the attorneys' value comes
  from the published reference redlines.

`docs/scoring.md` states every formula, the validity gate and the failure taxonomy.

## Benchmark coverage

A scenario is one contract negotiation. Each scenario yields ten tasks:

- **Turn 1 (opening redline).** Five attorneys independently reviewed the same clean contract for the same side,
  each producing a redline and a rubric set. The five tasks share one input; a model's Turn 1 redline is graded
  against all five rubric sets, so the score reflects the range of defensible attorney approaches rather than one
  reference.
- **Turn 2 (response).** Five more tasks start from one of those Turn 1 redlines, seen from the other side: the model
  represents the counterparty and responds to an existing markup.

The benchmark has 35 scenarios (350 tasks) across five agreement types, with and without playbooks, under US and
English law. The public set on Hugging Face holds 21 scenarios (210 tasks); 14 scenarios (140 tasks) are held out
as a private set. The published leaderboard reports both.

## The task dataset

Each Hugging Face entry is a complete Harbor task. The pieces of `tasks/<task name>/` and their roles:

| Path | Role |
| --- | --- |
| `task.toml` | Harbor task definition: metadata (scenario, turn, attorney, split, parties, author string), container and verifier settings |
| `instruction.md` | the brief the agent receives |
| `environment/app/contract.docx` | the document to redline (on Turn 2, with the counterparty's markup) |
| `environment/app/grounding/` | the represented side's commercial context and playbook |
| `environment/skills/contract-redliner/` | the scripts the agent uses to read the document and write tracked changes, comments and escalations |
| `tests/judge.py` and modules | the grader: validity gate, change list, judge panel |
| `tests/rubrics.json` | the attorney's rubric items for this task |
| `tests/baseline/contract.docx` | the input, kept for the grader's diff |

Beside the tasks, `references/<task name>.docx` holds the attorney's own redline for each task, and
`data/tasks.parquet` one row per task for browsing (the dataset viewer).

Task names read `<agreement>-<party>-<counterparty>-t<turn>-<attorney>`, for example `ip-whithill-hartsong-t1-c`
(IP licensing, Turn 1, attorney C) and `ip-whithill-hartsong-t2-c` (the Turn 2 task that answers attorney C's
redline). The five `t1-a` to `t1-e` tasks of a scenario are byte-identical except for `tests/rubrics.json`, which
is why the runner executes a scenario's Turn 1 once and grades the result five times.

## Inside a task container

Each task builds a `python:3.12-slim` image with the document under `/app/contract.docx`, the grounding files under
`/app/grounding/`, and the contract-redliner skill under `/skills`. The agent is told which party it represents and
which author string to sign its marks with. The redlined document is captured as the trial's artifact.

## Documentation

- [docs/tasks.md](docs/tasks.md): scenarios, turns, the rubric taxonomy, the task layout and naming.
- [docs/scoring.md](docs/scoring.md): gate, change list, judge panel, task score, counting rule, metrics.
- [docs/running.md](docs/running.md): command reference, run layout, resume semantics, sizing and cost notes.
- `configs/models.toml` and `configs/protocol.toml`: the leaderboard harness settings and the protocol constants.

## Repository map

```
configs/            model registry and protocol constants
docs/               tasks, scoring and running guides
schemas/            JSON schemas for the files the runner writes
src/contractbench/  the package: dataset, models, keys, runner, collect, classify, grade, metrics, surgicality, cli
tests/              offline tests on synthetic tasks, documents and job trees
benchmark/ jobs/ runs/ .env    created locally, never committed
```

## License

The code in this repository is released under the MIT License. The dataset is released separately under
CC BY 4.0.
