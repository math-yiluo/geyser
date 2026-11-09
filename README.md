# Geyser

Geyser is a framework in which LLM agents self-organize and divide a large task among
themselves. Any agent can publish a self-contained subtask with acceptance conditions;
each open subtask goes to a suitable idle agent or a new one; all agents share a product
Library, a Wiki of lessons learned and direct messaging. Coordination follows fixed rules
enforced by a deterministic Hub, not a central orchestrator.

- [DESIGN.md](DESIGN.md) describes the behavior and is authoritative.
- [PLAN.md](PLAN.md) holds the milestones, the port of FormalGeyser, and the evidence
  behind many defaults.
- [CHANGELOG.md](CHANGELOG.md) records behavior changes.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/).
- Git 2.40 or later (`git merge-tree --write-tree --merge-base`).
- For live runs: a Linux host with a systemd user manager (a WSL 2 distribution on
  Windows, with WSL interop turned off), and at least one backend: the `codex` CLI, or
  Claude Code through the `claude` extra (`uv sync --extra claude`, plus `bwrap` and
  `socat`). The core and the test suite run on any OS.

## Quick start

```bash
uv sync
uv run geyser init my-project --name "My project" --backend codex
cd my-project
uv run geyser setup --title "Write a report" --goal "Write reports/summary.md summarizing ..." --acceptance "reports/summary.md exists and covers ..."
uv run geyser choose
uv run geyser serve
```

`geyser choose` lists what must be chosen before a run: every role's model and effort
and the answer deadline. Choose each, for example
`geyser choose workers.codex --model <model> --effort high` and
`geyser choose answer_deadline --minutes 120`, or write them in `geyser.toml`.

In another terminal:

```bash
uv run geyser dashboard
uv run geyser run start
```

`geyser serve` never starts model work by itself; live execution starts only with
`geyser run start` (or the dashboard's Start button), and only once every choice is
made. Other commands: `status`, `agents`, `tasks`,
`run pause|pause-after-turn|resume|stop`, `agent pause|resume <id>`,
`group pause-after-turn|resume <backend>[.workers|.reviewers]|integrator`,
`message <agent> <text>`, `answer <request> <text>`, `backend <name>`,
`move <worker> <backend>`, `invite-back <task> <worker> <reason>`, `choose`, `doctor`.

Configuration lives in `geyser.toml`; DESIGN.md gives each setting's meaning and default
where the behavior is described.

## Package map

| Package | Contents |
| --- | --- |
| `geyser.core` | The SQLite ledger and the deterministic state machines: tasks, teams, agents, waits and the wait graph, the Waker's ledger side, candidates and reviews, completion, messages, controls, the operator's choices, activations, invariants. |
| `geyser.content` | Git plumbing, the Library and workspaces, candidate capture, staging, drafts, shared records, sources. |
| `geyser.integration` | Direct integration, batch integration, publication with a durable intent. |
| `geyser.gateway` | The two model-facing tools and the operation catalog with server-side capabilities and receipts. |
| `geyser.runtime` | The activation runner, model-facing views, standing instructions, handoff and state files, backends (Codex, Claude Code, scripted). |
| `geyser.host` | Owned process trees: cgroups of a delegated systemd subtree on Linux, an in-memory fake for tests. |
| `geyser.scheduler` | Admission passes, assignment and recruitment decisions, validation jobs, recovery. |
| `geyser.domains` | The domain pack boundary and the built-in general pack. |
| `geyser.api`, `geyser.cli` | The loopback operator API, the dashboard (agents' turns, and the two-layer task view of `geyser.core.relations`) and the `geyser` command. |

## Development

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src/geyser
```

The tests drive whole stories with scripted agents through the real service steps,
check the invariants of DESIGN.md section 19 after each, and crash the Hub before and
after its commits to check recovery. `tests/fake_codex.py` stands in for
`codex app-server` to test the Codex adapter without a model.
