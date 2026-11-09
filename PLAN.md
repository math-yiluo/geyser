# Geyser plan

Status: 2026-11-05. This file holds what DESIGN.md does not: the milestones, how
Geyser is built and tested, how FormalGeyser moves onto it, the open questions, and
the measurements from FormalGeyser's runs that justify many of Geyser's defaults.
DESIGN.md describes behavior. This file changes as the work progresses. Section
numbers refer to DESIGN.md unless they name a section of this file.

## 1. Milestones

| Milestone | Scope | Done when |
| --- | --- | --- |
| M0 Core | Models, ledger, commands with receipts, task and team state machines, waits, gates, completion; gateway with capabilities; scripted backend; fake host | The first scenario passes end to end with scripted agents, invariants checked |
| M1 Content | Instance layout, snapshots and blobs, staging and archive, Git Library, direct integration, Wiki, workspace refresh | Scenarios publish real commits; crash at every boundary recovers |
| M2 Runtime | Linux process host, Codex adapter, activation runner, views and deltas, sandbox probe, operator API and CLI | One worker completes a small Task 0 on a live backend, with explicit operator authorization |
| M3 Coordination | Assignment and recruitment decisions, review dispatch, talk, steering, message turns, requests, completion feedback | A multi-worker run with reviews and messages completes a Task 0 |
| M4 Robustness | Recovery, backoff, account holds, run, agent and group controls, rotation and handoff, Claude adapter, dashboard | Fault-injection suite passes; a run survives a Hub restart |
| M5 Extensions | Batch integration, long validations (`queued`), drafts, shared records, sources, `general.command@1` | Batch scenarios pass; a pack with a service validates |
| M6 FormalGeyser | Lean pack on Geyser, compatibility settings, parity tests | FormalGeyser's synthetic suite passes on Geyser |

## 2. Engineering and testing

- Python 3.13 and uv, with the same dependency families as FormalGeyser (FastAPI,
  uvicorn, pydantic 2, httpx, typer, rich, jsonschema, portalocker), so FormalGeyser
  can depend on Geyser without new kinds of dependency. Versions are locked.
- English for code, documentation, prompts and messages. Text files use LF.
- The core is deterministic: injected clock and IDs, scripted backend, fake process
  host. Its tests run on any OS and never call a model.
- Scenario tests drive whole stories with scripted agents: publish, assign, stage,
  validate, review, publish, complete; a blocking handoff; recruitment; rejection
  and correction; a material revision; a message wake; an account hold; pause after
  turn; Task 0 shutdown.
- Fault injection crashes the Hub at every commit boundary of those scenarios and
  checks recovery and the invariants of section 19.
- Real-process tests (systemd scopes, sandbox probes, real backends without paid
  turns) are opt-in and need the Linux host.
- Validation: `uv run pytest -q`, `uv run ruff check src tests`,
  `uv run mypy src/geyser`.

## 3. Building FormalGeyser on Geyser

### 3.1 Mapping

| FormalGeyser today | In Geyser | In the Lean pack |
| --- | --- | --- |
| TaskHub (`formal_geyser.coordination`) | The Hub | — |
| `taskhub_actions`, `taskhub_call` | The gateway, `gateway.prefix = "taskhub"` | `lean_check`, `lean_registry_list`, `resource_pdf` |
| Tasks, Task 0, revisions, completion | Core (section 5) | — |
| Workers, reviewers, Integrator, Dispatcher, decisions | Core (section 6) | — |
| Recruitment and assignment decisions | Core | — |
| Waits that name their end, the Waker, the answer deadline, wait-cycle refusal | Core (section 12) | How a publication is described to the Waker (to define) |
| Peer notices, questions and answers; invitation back | Core (sections 6.2, 11.2) | — |
| Choices before a run, backend moves, the Integrator's group | Core (sections 6.1, 15.1) | FormalGeyser keeps reviewers, the Integrator and decisions on Codex by setting their roles' backend |
| Rotation, handoff, journal, state file | Core (section 7) | — |
| Peer talk, correspondence, requests | Core (section 11) | — |
| Artifacts: definition, statement, proof | Contribution kinds | `lean.definition`, `lean.statement`, `lean.proof` with the pairing rule |
| `lean.*@1` acceptance policies | Policies | Validators backed by the Lean service; `check_tree` = Lake build and kernel replay |
| Semantic review with literature correspondence | Review gate | `lean-review.md` |
| Integrator batches (5 items, 13 minutes) | `integration.mode = "batch"` | — |
| Public notes and `wiki/README.md` | Core Wiki (section 10.1) | Starter pages |
| Literature and central-library pointers | Core sources (section 10.4) | Pointer index, `wiki/literature.md` |
| Public drafts | Core drafts | Pairing rule for drafts |
| Definition and proof DAGs | Shared records | Their schema, checker and merge |
| The Lean service | — | The pack's validation service |
| Pinned initialization, `.dependencies/` | `prepare_instance` and the readiness gate | The pins |
| Codex and Claude backends, account holds, group controls | Core | — |
| Dashboard, `taskctl` | Core dashboard, `geyser` | `formalizectl` |

### 3.2 The port

1. Geyser reaches parity on synthetic scenarios with the general pack (milestones
   M0 to M5).
2. The Lean pack is extracted from FormalGeyser's `registry.py`, `schemas.py`,
   `publication.py`, `verification.py`, `lake_build.py`, `lean_service/`,
   `bootstrap*.py` and its guides, against the interfaces of section 17.
3. FormalGeyser depends on a pinned Geyser release and keeps two compatibility
   settings: the state directory `.task-based/` and the gateway prefix `taskhub`.
   It adopts Geyser's sequential IDs. A migrated instance numbers its existing
   objects in creation order, its root task first as `task-0`, and keeps the old
   UUIDs as aliases that lookups accept and historical receipts retain.
4. FormalGeyser's own scenario tests run against the new stack before its
   `coordination` package is removed.
5. Migrating a live instance is a separate operator decision. Historical receipts
   stay read-only evidence. Auto-lean-test-1 is never resumed.

## 4. Not yet implemented

Geyser 0.1.0 implements milestones M0 to M5 and passes its scenario, fault-injection,
API and adapter tests; on 2026-11-09 it gained FormalGeyser's newer general mechanisms
(waits that name their end, the Waker, message kinds, invitation back, choices before a
run, backend moves, the Integrator's group). These parts of DESIGN.md, and of
FormalGeyser's newer design, are not yet implemented or verified:

- **Live runs.** The Codex and Claude Code adapters are ported from FormalGeyser and
  tested against a fake app-server, but no live turn has run on Geyser. The "done when"
  criteria of M2 to M4 that need a live backend wait for an explicit operator
  authorization on the Linux host.
- **The native sandbox probe** (section 16). Before admission only host-side checks run
  (WSL interop off, the workspace exists); the permission profile itself is enforced by
  Codex and Claude Code but not probed with a real sandboxed command.
- **Workspace reclamation** of retired workers (section 6.9). Workspaces are kept.
- **Usage per sign-in profile** (section 14). Usage is recorded per backend.
- **Queued validations** (section 8.3) are supported through the pack hook
  `resume_validation`; the general pack never queues.
- **The dashboard** is minimal: it has no task search, no revision history, no
  operator buttons for claim, declaration cancel, validation retry, invitation back or
  moving a worker, and no panel for the choices, backends, profiles or rotation (the
  API and the CLI have them; the status bar lists the choices still missing). The task
  view's layout is a simple layered drawing meant for tens of linked tasks, not
  thousands.
- **Pack descriptions of publications for the Waker.** The Waker sees each published
  candidate's kind, introduction and paths. FormalGeyser shows its own (declarations,
  statement, entry module); a pack hook for that is not defined yet.
- **Bounded storage and core transfer** from FormalGeyser (released conversations
  compressed, Library copies of idle workers at rest, integration checkouts reclaimed
  when their batch lets them go, shared-record revisions pruned; `export-core` and
  `import-core`) are not ported yet. They matter for long runs and large Libraries.
- **M6**, FormalGeyser on Geyser, happens in the FormalGeyser repository.

## 5. Open questions

None at present. Decided on 2026-11-05 and recorded in DESIGN.md:

- `direct` is the default integration mode, and FormalGeyser sets `batch`
  (section 9.2).
- No cap on tasks (sections 1.3 and 5.3).
- One collaborator per task (section 6.2).
- Reviewers have the same read access as every agent (sections 4.2 and 8.4).
- No Windows-native live runs and no products that are not files (section 1.3).

## 6. Evidence from FormalGeyser

These measurements come from FormalGeyser's runs, as recorded in its DESIGN.md and,
for the rows from 2026-11-07 on, its CHANGELOG.

| Observation | Rule it supports |
| --- | --- |
| 1,177 selection helpers each forked the whole worker conversation (median 166k tokens on the first request), about 400M tokens in all | Decisions are tool-less, fresh and minimal (6.5) |
| Forked reviewers started at a median of 175k tokens and used about three times the tokens of fresh reviewers | No forks; reviewers start fresh (7.6, 8.4) |
| 501 turns resumed after more than an hour sent 74M uncached tokens on their first requests | Conservative rotation after 40 idle minutes (7.5) |
| 91 notices to finished reviewers resumed their whole conversations, about 65M tokens | Questions start a new conversation from the review report (8.4) |
| Reviews opened before machine validation waited 13 to 22 hours or rebuilt candidates | Review only after machine acceptance (8.3) |
| Agents looked up operation schemas 4,272 times | A cheat sheet in the standing instructions (7.3) |
| Inputs kept across compaction were 38.5% of all input | Text stays in files; inputs stay short (7.3) |
| One admission pass ran over 40 minutes while no worker recovered | Bounded admission passes (6.6) |
| 723 never-retired workers held 44 GB after three days | Idle retirement and verified reclamation (6.9) |
| Hashing a multi-gigabyte Library on coordination threads starved the event loop | File work off the writer and the loop (4.3) |
| A drain that never finished lost five rejections for two reviews | Drained commits at quiescence (13.4, 14) |
| Worker 0 ran one conversation for five days through 41 compactions | Rotation and handoff files (7.4, 7.5) |
| Most pilot waits named a requirement that often could not hold (a later version, a renamed declaration, another kind of output), and many pauses named nothing and waited for a message | Waits name their end; subscriptions judged by the Waker (12) |
| All 937 task requirements in the pilot's waits named the task's acceptance policy, while proof tasks delivered 6,839 definition and 1,286 statement candidates | Requirements name no policy (5.6) |
| Peer messages woke workers that were waiting for something else | Notices wake no worker; questions and answers do (11.3) |
| The pause rule sent members with nothing left to do away, and nothing could bring them back | Invitation back (6.2) |
| On 11-07, 543 of 603 Codex workers asked to move to Claude could not be admitted, and 75 opened new Codex conversations | Moving a worker at its next activation, counted on the new account (6.1) |
| Holding the Integrator meant holding the Codex reviewers too | The Integrator's own control group (15.1) |
| Unchosen roles silently ran on defaults, and reviewers and the Integrator followed the workers' model | Choices before a run (15.1) |
