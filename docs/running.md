# Running ContractBench

## Prerequisites

```bash
uv tool install harbor                 # Harbor 0.18 or later; Docker (or Colima) must be running
pip install -e ".[grade]"              # the runner and the grader's imports (python-docx 1.2.0, lxml, litellm)
cp .env.example .env                  # provider keys; never committed
contractbench fetch                    # the dataset, tag v1.0, into ./benchmark
```

Keys: the opencode agent receives only the key variables of the model's provider (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, `OPENROUTER_API_KEY`). The judge panel needs `ANTHROPIC_API_KEY`
and, for a full panel, `OPENAI_API_KEY` and `GEMINI_API_KEY`. `GROK_API_KEY` and `GOOGLE_API_KEY` are accepted as
aliases. Set `CONTRACTBENCH_DIR` to use a dataset directory other than `./benchmark`, `DOCKER_HOST` for a
non-default Docker socket, `HARBOR_BIN` for a Harbor binary off the PATH.

Troubleshooting: if `contractbench` fails with `ModuleNotFoundError: No module named 'contractbench'` right after a
successful install, the virtual environment's `.pth` files may carry the macOS hidden flag (folders synced by
iCloud get it), and Python 3.13 skips hidden `.pth` files; `chflags -R nohidden .venv` fixes it, or keep the
checkout outside synced folders.

## Commands

| Command | What it does | Key flags |
| --- | --- | --- |
| `contractbench fetch` | downloads the dataset from Hugging Face into `./benchmark` | `--revision v1.0`, `--dest DIR`, `--pattern GLOB` (repeatable, for a subset) |
| `contractbench preflight` | checks Harbor, Docker, the dataset and the keys | `--model M`, `--build` (one image and a no-op run), `--no-docker`, `--no-keys`, `--no-harbor` |
| `contractbench run` | starts one Harbor job per model over the set's inputs | `--run-id R`, `--model M` (repeatable), `--set public\|private\|full`, `--tasks GLOB`, `-k 3`, `-n 4`, `--override KEY=JSON`, `--output-token-max N`, `--until-complete`, `--dry-run` |
| `contractbench resume` | continues an interrupted run; finished attempts are kept | `--run-id R`, `--model M`, `--filter-error-type T`, `--until-complete` |
| `contractbench status` | per-model progress | `--run-id R` |
| `contractbench collect` | collects Harbor job trees into `runs/<run-id>/` | `--run-id R`, `--model M`, `--keep-logs` |
| `contractbench grade` | grades counted attempts with the judge panel | `--run-id R`, `--model M`, `--tasks GLOB`, `--parallel 8`, `--retries 2`, `--panel CSV`, `--gate-only`, `--estimate` |
| `contractbench metrics` | computes scores, the leaderboard and the surgicality tables | `--run-id R`, `--compare LEADERBOARD.csv`, `--eval-results`, `--no-surgicality` |

Every command takes `--dataset DIR` to point at a dataset directory other than `./benchmark` or `$CONTRACTBENCH_DIR`.

`--model` takes a registry alias (`opus-5-5`, `gpt-6-astra`, `gemini-3-8-flash`, `grok-4-7`, `kimi-k3`,
`glm-5-3`, `deepseek-v4-pro`) or any `provider/model` id; unknown ids get the provider defaults from
`configs/models.toml`. `--override provider.options.path=JSON` edits the opencode options of the resolved model,
for example `--override reasoningEffort='"high"'`.

## A run, step by step

1. `run` creates `runs/<run-id>/run.json` (dataset snapshot, protocol, the resolved settings of every model) and
   starts one Harbor job per model over the set's inputs: the `-t1-a` task of each scenario and every Turn 2 task,
   `-k` attempts each (the protocol's 3 by default). Harbor writes the job under `jobs/<run-id>/`.
2. When Harbor exits, `run` collects and classifies the trials. Interrupted or infrastructure-failed trials are
   re-run with `harbor job resume` (same job, finished trials untouched); inputs still short of their attempts get a
   top-up job. With `--until-complete` this loop continues until every input has its attempts or its re-run budget
   is spent.
3. `resume --run-id R` does the same loop for an existing run, for example after the machine was shut down.
   Settings are read from `run.json`, so a resume cannot drift from the original configuration; `run` with different
   settings on the same run id is refused.
4. `grade` grades every counted successful attempt against every rubric set that applies (five for a Turn 1 input,
   one for Turn 2), eight grades in parallel by default (24 judge calls in flight). Grades that already exist for
   the same deliverable are skipped, so `grade` can be re-run at any time. `--estimate` prints the number of grades
   and judge calls without calling anything.
5. `metrics` writes `runs/<run-id>/metrics/` and prints the leaderboard.

## The run directory

```
runs/<run-id>/
  run.json                 dataset snapshot, protocol, model settings, waves (Harbor jobs) with their commands
  ledger.json, manifest.csv
  rollouts/<model>/<input task>/attempt<N>/
    contract.docx          the redlined document
    result.json            Harbor's trial result (tokens, cost, timings, exceptions)
    trial.json             the attempt's summary and classification
    gate/                  the validity gate's dry run: change_list.md, grade.json on a gate failure
  rollouts/<model>/<input task>/rerun/<job>__<trial>/trial.json      re-run trials, kept for the record
  grades/<model>/<target task>/attempt<N>/
    grade.json, reward.json, change_list.md, judges/*.json, judge.log, grade_meta.json
  metrics/
    leaderboard.csv, leaderboard.md, slices.csv, failures.csv, cost.csv, summary.json
    surgicality.csv, edit_volume.csv, eval_results/*.yaml (with --eval-results)
```

`jobs/` is ephemeral and can be deleted once a run is collected; the metrics read only `runs/`. Never edit files
under `benchmark/` between waves: Harbor fingerprints the task directories and refuses to resume a job whose tasks
changed. The runner grades from temporary copies for that reason.

## Attempts, failures and re-runs

Per model and input, finished trials are numbered in start order and the first three count. A model's own
failures count: an unchanged document (`length_stop`, `ended_in_thinking`, `no_edit_finished`) or a redline that
fails the validity gate (`gate_rejected`). Infrastructure failures (provider or sandbox errors, cancellations, a
missing deliverable) and, under the default policy, agent timeouts are re-run up to three times per input and
never replace a counted failure. `status` shows per-model progress; `failures.csv` the counts.

## Sizing and cost

Each trial container asks for 2 CPUs and 4 GB (`task.toml`); `-n 4` means about 8 CPUs and 16 GB per model. The
public set builds 126 task images that share every layer above the document copy; `preflight --build` warms the
shared layers. A full public run of one model is 378 rollouts (agent cost depends on the model) plus 630 panel
grades (three judge calls each; the change list and rubric items are the input).

## Validating against the published numbers

`contractbench metrics --compare benchmark/leaderboard.csv` prints, per model, the run's scores next to the
published ones and the delta. Reproductions differ by sampling, so treat small deltas as informational; the
comparison exits non-zero only when a filled column differs beyond the tolerance (default 5e-7), which is meant for
re-computations from the same documents and grades.
