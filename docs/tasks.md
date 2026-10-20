# Scenarios, tasks and rubrics

## Scenarios and turns

A scenario is one contract negotiation: a contract, two parties, the represented side's commercial context and,
in most scenarios, its playbook. Each scenario yields ten tasks.

- **Turn 1 (opening redline).** Five attorneys independently reviewed the same clean contract for the same side,
  each producing a redline and a rubric set. The five tasks share one input, so a model's Turn 1 redline is graded
  against all five rubric sets: the benchmark scores it against the range of defensible attorney approaches rather
  than one reference.
- **Turn 2 (response).** Five more tasks start from one of those Turn 1 redlines, now seen from the other side: the
  model represents the counterparty and responds to an existing markup, accepting, rejecting, countering or
  introducing new edits.

The benchmark has 35 scenarios (350 tasks). Agreement types are Master Services Agreements, IP licensing
agreements, research partnership agreements, marketing agreements and commercial leases; 30 scenarios are governed
by US law and five by the law of England and Wales; playbooks come at three levels (none, a single-agreement
playbook, a multi-contract playbook). The public dataset holds 21 scenarios (210 tasks); 14 scenarios (140 tasks)
are held out as a private set. The split was chosen by scenario so that both sets match the benchmark's mix of
agreement types, playbook levels and jurisdictions, with the models ranking the same way on both.

## Rubrics

Each attorney completed the redline before writing roughly 10 to 20 rubric items for it. A rubric item names the
contract section, the **required action** (Edit, Preserve, Comment or Escalate; on Turn 2, Edit splits into Reject,
Counter and New Edit, and Preserve into Accept and Leave Untouched), a **priority** (High, Medium or Low, weighted
3, 2 or 1), an **evaluation dimension** (Issue Prioritization, Negotiation Restraint, Situational Judgment,
Escalation Judgment or Standard Position Execution), the condition the redline must satisfy, and the attorney's
justification. Every item carries a positive weight.

## Task layout

```
tasks/<task name>/
  task.toml              Harbor task definition: [task] name and description, [metadata], container and verifier settings
  instruction.md         the brief: who the agent represents, the author string to sign with, the toolkit, the grounding files
  environment/
    Dockerfile           python:3.12-slim with the redlining toolkit; copies app/ and skills/ into the image
    app/contract.docx    the Word document to redline (Turn 2: with the counterparty's tracked-change markup)
    app/grounding/       the represented side's commercial context and playbook, as PDF or Word files
    skills/contract-redliner/   scripts the agent uses to read the contract and write tracked changes, comments, escalations
  tests/
    test.sh, judge.py, ...      the grader (validity gate, change list, judge panel)
    rubrics.json                the attorney's rubric items for this task
    baseline/contract.docx      the input, kept for the grader's diff
references/<task name>.docx    the attorney's own redline for the task
data/tasks.parquet, data/rubrics.parquet   one row per task, one row per rubric item
registry.json                  the Harbor dataset manifest (`contractbench-public@1.0`)
```

`task.toml [metadata]` holds the scenario, agreement type, jurisdiction, playbook level, turn, side, the parties,
the author string the agent must use, the attorney letter, `responds_to` (for Turn 2, the Turn 1 task whose redline
it answers), a numeric `expert_id` identifying the attorney across tasks, the rubric counts and total weight, and
the split.

## Task names

`<agreement>-<party>-<counterparty>-t<turn>-<attorney>`, for example `ip-whithill-hartsong-t1-a` and
`ip-whithill-hartsong-t2-c`.

- `agreement`: `msa`, `ip`, `rpa` (research partnership), `mktg`, `lease`.
- The two party words name the scenario; the Turn 1 side comes first, so both turns of a scenario share the prefix.
- `t1-a` to `t1-e` are the five attorneys' Turn 1 tasks. A Turn 2 task carries the letter of the Turn 1 redline it
  answers.

## Inputs and the one-rollout-per-input protocol

Because a scenario's five Turn 1 tasks are byte-identical apart from `tests/rubrics.json`, the runner treats the
`-t1-a` task as the scenario's Turn 1 input, runs it, and grades the deliverable against all five rubric sets.
Every Turn 2 task is its own input. The public set therefore has 21 + 105 = 126 inputs; three attempts per input
give 378 rollouts and 630 grades per model.

## Inside the container

Each task image is `python:3.12-slim` with python-docx, lxml and the toolkit installed; the document sits at
`/app/contract.docx`, the grounding files under `/app/grounding/`, the skill under `/skills` (Harbor mounts it for
the agent). The environment variable `ESCALATION_PREFIX` names the comment prefix for escalations. The agent runs
under Harbor's opencode agent with the per-model settings in `configs/models.toml`; the redlined document is
captured as the trial's artifact.

## Anonymization

All contracts, grounding documents and redlines are fictional material written by attorneys for this benchmark;
the parties, people and events in them are invented. Personal identifiers of the attorneys who produced the
redlines and rubrics were stripped from the files and the metadata; each attorney is recorded only as a numeric
`expert_id`.
