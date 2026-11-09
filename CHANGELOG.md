# Changelog

## Unreleased — 2026-11-09

General mechanisms brought over from FormalGeyser's design of 2026-11-07 and 11-08, where
its Linux pilot showed the need for them.

- **Waits name their end.** `pause` requires a predicate, and a predicate waits for
  another task's results (`subscriptions`: a task and the `need`, in words), the waiter's
  own submission (`publications`, `validations`, `completion_declaration_id`), or an
  answer (`answers`, `external_response_id`). A pause takes no `requirements` any more;
  stored waits keep theirs and wake as before. `contribute` waits on its candidate's
  outcome as a `publications` term, so an evidence-only acceptance, a return or a
  deferral now ends that wait too. A member with nothing left to do leaves the task.
- **The Waker.** A new system identity. After publications, one tool-less decision covers
  every subscribed wait whose task published something not yet judged for it, and answers
  one boolean per pair; publications in quick succession share a decision. Five failed
  asks wake every wait the decision covered and tell the operator. A subscribed task's
  completion or retirement ends the wait too.
- **Answer deadline.** A wait for an answer ends at `waits.answer_deadline_minutes` if
  none comes.
- **No wait cycles.** A subscription to one's own task, or one that would close a cycle
  of tasks waiting on each other, is refused naming the cycle; so is a revision whose new
  requirement would close one. Invariant 14 checks it after every scenario.
- **Message kinds.** `talk_send` takes `kind`: `notice` (the default), `question`, or
  `answer` naming its question, which only the agent asked may send, to the asker. A
  notice wakes no worker. Questions, answers and operator messages also wake an idle
  worker for one message turn in a new conversation; before, an idle worker's messages
  waited for its next task.
- **Invited back.** `invite_back` (`task.invite_back`, `geyser invite-back`): a current
  executor or the operator brings back a worker that left a task.
- **Requirements name no policy.** A `policy` in a stored or supplied requirement is
  ignored.
- **Choices before a run.** No role has a default model or effort: workers per backend,
  reviewers, the Integrator (batch mode), the Dispatcher and the Waker each have their
  own (`[codex]`, `[claude]`, `[reviewers]`, `[integrator]`, `[decisions]`, `[waker]`),
  and so does the answer deadline (`[waits]`). `run.start`, `run.resume`, `agent.resume`
  and `group.resume` are refused until all are chosen; `geyser choose` (`/v1/choices`)
  sets them at runtime, and the status lists what is missing. `assignment.reviewer_backend`
  and `assignment.integrator_backend` are replaced by the roles' `backend`.
- **Moving a worker.** `agent.move` (`geyser move`) moves a worker to another backend: at
  once without a turn in flight, otherwise when it ends; its work continues in a new
  conversation there. The Integrator takes its role's backend at its next batch.
- **The Integrator's group.** `integrator` is a control group of its own; paused, neither
  batches nor direct integration go on.
- The cheat sheet lists the fields of small object arguments, so `pause` shows what a
  predicate can name. Older ledgers gain the new columns when opened.

## 0.1.0 — 2026-11-06

First implementation of DESIGN.md (milestones M0 to M5 of PLAN.md).

- Core: SQLite ledger with typed sequential IDs, idempotent receipts and audit events;
  tasks with revisions (cosmetic, additive, material) and the original conditions kept
  as `original:<key>`; teams with one-time recruitment; waits and predicates; candidates
  with machine validation and independent review; completion with one feedback
  opportunity; talk, operator messages and requests; run, agent and group controls;
  account holds; invariants.
- Content: candidates frozen from manifest-listed paths against the Library base; the
  Library as a Git repository moved only by expected-head updates; three-way workspace
  refresh; drafts, shared records and sources; a starter `wiki/README.md`.
- Integration: direct mode (mechanical three-way merge; conflicts return the candidate)
  and batch mode (the Integrator's prepare, resolve, validate, publish, defer, retry);
  publication with a durable intent recovered exactly after a crash.
- Runtime: activations with fencing tokens, model-facing views with deltas, standing
  instructions with a generated cheat sheet, conservative and aggressive rotation with
  handoff checks, drained operations committed at quiescence, steering, transcripts.
- Backends: Codex (`codex app-server`), Claude Code (Agent SDK bridge speaking the same
  protocol), and a scripted backend for tests. Process host: cgroups of a delegated
  systemd subtree on Linux.
- Scheduler: bounded admission passes, assignment and recruitment decisions, validation
  retries with backoff, restart recovery (`closed_unknown`, no blind replay).
- Operator: loopback API with a token, dashboard, `geyser` CLI.
- Contribute now waits for the candidate's outcome: the task resumes once the candidate
  is published, or at once if it is rejected, withdrawn or invalidated.

### Dashboard

- Rewritten in FormalGeyser's look (light and dark), as three static files under a
  content security policy. Agents view: each turn shows what it received, with the
  delivery state of every item, the agent's messages, and its tool calls folded with
  their results; requests from the agent are pinned above; the operator's queued
  messages can be withdrawn.
- New task view with two layers (DESIGN.md section 15.4): Publication, the tree of
  where each task was published from; Dependencies and waits, the links from what is
  needed to what needs it, with deadlocks and the tasks that wait on the operator
  (directly or through other tasks) called out. `GET /v1/task-graph` serves both.
- Transcripts record each turn's exact input and the messages steered into it.

### Robustness fixes from the first review

- Every service step is isolated, and validations, decisions and integration run in
  supervised loops of their own, so a slow or failing step never blocks admission.
  Startup recovery is retried with backoff.
- Drained commits are idempotent (a contribution records its candidate when it
  registers it; each commit ends its activation in the same transaction) and an
  unexpected error is retried, then abandoned, instead of stalling the service.
- An operator cancellation of a completion declaration resumes the declarer; a
  teammate that pauses or leaves loses its feedback opportunity.
- A material revision removes invalidated members from an open batch, and
  publication refuses members that are no longer accepted.
- Direct integration backs off per candidate; publication intents are finished before
  anything else is integrated.
- Steering ends with the backend turn; deliveries with an unknown outcome are queued
  again as possibly delivered; input that never reached a backend is released.
- Pending decisions respect run controls, holds and Task 0 completion; decision
  failures always count; message turns of ordinary workers count toward the
  concurrency limit; mutating pack operations are fenced and idempotent; one
  unevaluable waiter or task is skipped and reported instead of blocking the rest.
