# Scoring

This page states how a rollout becomes a number: the validity gate, the evidence the judges read, the panel vote,
the task score, which rollouts count, and how model-level metrics are formed. The grader lives inside every task
(`tests/judge.py` and its modules); the runner only orchestrates it.

## 1. Validity gate

Before any judge is called, the grader compares the returned document with the task's input
(`tests/baseline/contract.docx`). A rollout fails the gate when:

1. the file is missing or does not open as a Word document;
2. the base text was changed outside tracked changes (a tolerance of max(50 characters, 1% of the base text)
   absorbs rendering noise; `CONTRACTBENCH_GATE_BASE_TEXT_TOLERANCE` can raise it);
3. the counterparty's existing markup was altered (Turn 2);
4. there is no net new work by the declared author after discounting no-op edits.

Nine further integrity flags (forged revision markers, duplicate revision ids, counterparty fabrication, style
changes, and so on) are recorded and become hard failures under `GATE_STRICT=1`.

A gate failure scores 0 and counts as a completed attempt that failed (see section 5).

## 2. The change list

For a document that passes the gate, the grader renders a change list: the model's edits by section, how each
counterparty change was handled (accepted, rejected, countered, left pending), the comments and escalations the
model wrote, and the resulting text of each affected section. This record, not the raw document, is what every
judge reads, together with the rubric items.

## 3. Judge panel

Each rubric item is judged Pass or Fail by a panel of three models from different providers:
`anthropic/claude-opus-4-8`, `openai/gpt-5.5`, `gemini/gemini-3.5-flash` (configured in `configs/protocol.toml`
and in every task's `task.toml`). All rubric items of a task go to each judge in one call. The verdict is the strict
majority of the surviving judges; a judge whose call fails after retries is dropped, and if fewer than two survive
the grader falls back to a single judge and records that. The runner retries grades that come back short of a
full panel and flags the ones that stay short as degraded.

## 4. Task score

Every rubric item has a positive weight: 3 for High priority, 2 for Medium, 1 for Low. With W the total weight of a
task's items and E the weight of the items judged Pass,

    score = E / W

A task with items weighted 3, 2, 1 and 1 (W = 7) where the 3-point and one 1-point item pass scores 4 / 7 = 0.571.
The grader writes the value as `score.weighted` in `grade.json`; it is bounded in [0, 1] by construction.

## 5. Which rollouts count

The protocol runs three attempts per input. An input is a scenario's Turn 1 contract (one input shared by five
tasks) or a Turn 2 task.

| Outcome | Status | Counts as an attempt | Scored |
| --- | --- | --- | --- |
| Document changed and passes the gate | `success` | yes | yes |
| Document changed, gate failed | `gate_rejected` | yes | no (0) |
| Document unchanged, agent stopped at its output limit | `length_stop` | yes | no |
| Document unchanged, agent's last turn was reasoning only | `ended_in_thinking` | yes | no |
| Document unchanged, agent finished | `no_edit_finished` | yes | no |
| Agent timed out | `timeout` | no, re-run (policy) | |
| Provider or sandbox error, cancelled, no deliverable | `infra`, `no_deliverable`, `unfinished` | no, re-run | |

Finished attempts are numbered per input in start order; the first three count. Re-run statuses are retried at
most `max_reruns_per_input` times (3) and never replace a counted failure. The timeout policy defaults to re-run,
which is what the published runs did; `timeout_policy = "counted"` makes timeouts count as failures instead.

## 6. Model-level metrics

- **Model score on a task set** = mean of `score.weighted` over all scored grades of tasks in the set, pooled
  across turns. A Turn 1 rollout contributes five grades (one per sibling rubric set), a Turn 2 rollout one. The
  private, public and full columns of the leaderboard restrict the set by the tasks' `split`.
- **Completion rate** = counted attempts with status `success` / counted attempts.
- **Per-turn scores** = the same mean restricted to Turn 1 or Turn 2 grades.
- **Slices** (required action, Turn 2 sub-action, priority, evaluation dimension, agreement type, playbook,
  jurisdiction) = weighted pass rate, sum(weight x Pass) / sum(weight), over the rubric items of the scored
  grades.
- **Spread across rubric sets** = for each Turn 1 attempt graded on all five sibling sets, the population standard
  deviation of its five scores; averaged per scenario, then over scenarios; reported in points.
- **Cost and duration** = means over completed rollouts of the agent's reported cost and execution time.

## 7. Surgicality and edit volume

These describe the shape of a redline and are computed from the documents, not from judge verdicts.

For every paragraph (tables included), walk the tracked changes that are direct children of the paragraph in
order. A deletion immediately followed by an insertion by the same author (comment markers, bookmarks and proofing
flags in between are ignored) is one replace event; any other element flushes a pending deletion as its own event;
an unpaired insertion or deletion is one event. The event's size is max(deleted characters, inserted characters);
the paragraph's untouched length u is the number of characters outside any tracked change.

    inline(e) = 1 if size(e) < 0.30 x u, else 0 (u = 0 gives 0)
    surgicality(actor) = mean over Turn 1 contracts of ( sum inline / sum events ) over the actor's redlines of the contract

Models are profiled on their counted successful Turn 1 rollouts (marks signed by the task's author string);
attorneys on the scenario's five reference redlines (`references/`), whose author is the one absent from the input
document. The edit-volume table compares, per Turn 1 contract, redlined passages (maximal runs of consecutive
touched paragraphs), edits (events), average edit length, total edited characters and inline share, as means over
contracts with 95% t intervals; a Turn 2 row compares edits per Turn 2 task.

## 8. Reproducibility

Agent sampling and judge models make runs non-deterministic; two reproductions of the same model differ by small
amounts. The pipeline itself is deterministic given the collected documents and grades: `contractbench metrics` on
the same `runs/<run-id>/` always yields the same numbers, and `--compare` reports deltas against a published
leaderboard to six decimals.
