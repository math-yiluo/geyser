"""Invariants checked by tests after every scenario and fault injection (DESIGN.md section 19).

``check`` returns human-readable violations; an empty list means every invariant
that the ledger can witness holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from geyser.core import tasks, wait_graph
from geyser.core.ledger import loads
from geyser.core.models import required_condition_keys

if TYPE_CHECKING:
    from geyser.hub import Hub


def check(hub: Hub) -> list[str]:
    problems: list[str] = []
    with hub.read() as txn:
        # 1. One current task and at most one live activation per worker.
        for row in txn.all("SELECT agent_id, COUNT(*) AS n FROM activations WHERE state IN "
                           "('planned', 'running', 'draining') GROUP BY agent_id HAVING n > 1"):
            problems.append(f"{row['agent_id']} has {row['n']} live activations")
        for row in txn.all("SELECT a.id, a.current_task FROM agents a WHERE a.current_task "
                           "IS NOT NULL"):
            membership = txn.scalar("SELECT state FROM memberships WHERE task_id = ? AND "
                                    "agent_id = ?", (row["current_task"], row["id"]))
            if membership not in ("active", "waiting"):
                problems.append(f"{row['id']} has current task {row['current_task']} without "
                                "a live membership")
        # 2. At most 1 + collaborators executors; used places never reopen.
        limit = 1 + hub.config.teams.collaborators
        for row in txn.all("SELECT task_id, COUNT(*) AS n FROM memberships GROUP BY task_id "
                           "HAVING n > ?", (limit,)):
            problems.append(f"{row['task_id']} had {row['n']} executors (limit {limit})")
        # 3. One valid reviewer execution per review; reviewers authored none of it.
        for row in txn.all("SELECT r.id, r.reviewer, c.authors FROM reviews r JOIN candidates c "
                           "ON c.id = r.candidate_id WHERE r.reviewer IS NOT NULL"):
            if row["reviewer"] in loads(row["authors"], []):
                problems.append(f"{row['id']} is reviewed by an author")
            live = txn.scalar("SELECT COUNT(*) FROM activations WHERE review_id = ? AND state "
                              "IN ('planned', 'running', 'draining')", (row["id"],))
            if live > 1:
                problems.append(f"{row['id']} has {live} live reviewer executions")
        # 4. A review starts only after an unqualified machine acceptance.
        for row in txn.all("SELECT r.id, r.candidate_id FROM reviews r WHERE r.reviewer IS NOT "
                           "NULL"):
            verdicts = txn.all("SELECT verdict FROM validations WHERE candidate_id = ? AND "
                               "phase = 'done'", (row["candidate_id"],))
            if not any(v["verdict"] == "accepted" for v in verdicts):
                problems.append(f"{row['id']} started without a machine acceptance")
        # 5. Publications and commits correspond; published candidates were accepted.
        committed = txn.all("SELECT * FROM publications WHERE state = 'committed'")
        commits = {row["commit_id"] for row in committed}
        if len(commits) != len(committed):
            problems.append("two publication records share a commit")
        for row in txn.all("SELECT id FROM candidates WHERE state = 'published'"):
            review = txn.scalar("SELECT state FROM reviews WHERE candidate_id = ?", (row["id"],))
            if review != "accepted":
                problems.append(f"{row['id']} is published without an accepted review")
        head = hub.library.repo.head()
        if head is not None and committed:
            latest = committed[-1]["commit_id"]
            if head != latest and not txn.scalar(
                    "SELECT 1 FROM publications WHERE state = 'intent'"):
                problems.append(f"the Library head {head[:12]} is not the latest publication")
        # 6. Completed tasks have evidence for every original and current condition key.
        for row in txn.all("SELECT * FROM declarations WHERE state = 'finalized'"):
            original = tasks.load_contract(hub.ctx, txn, row["task_id"], 0)
            current = tasks.load_contract(hub.ctx, txn, row["task_id"], row["revision"])
            keys = set(required_condition_keys(original, current))
            covered = set(loads(row["evidence"], {}))
            if not keys <= covered:
                problems.append(f"{row['id']} misses evidence for {sorted(keys - covered)}")
        # 7. A waiting member has no running task activation.
        for row in txn.all("SELECT m.agent_id, m.task_id FROM memberships m WHERE m.state = "
                           "'waiting'"):
            live = txn.scalar("SELECT COUNT(*) FROM activations WHERE agent_id = ? AND "
                              "task_id = ? AND purpose = 'task' AND state = 'running'",
                              (row["agent_id"], row["task_id"]))
            if live:
                problems.append(f"{row['agent_id']} runs a task turn while waiting on "
                                f"{row['task_id']}")
        # 11. Nothing autonomous starts after Task 0 completed, until the operator resumes.
        completed = txn.scalar("SELECT seq FROM events WHERE kind = 'project.completed' ORDER BY "
                               "seq DESC LIMIT 1")
        if completed:
            resumed = txn.scalar("SELECT seq FROM events WHERE kind = 'run.state' AND seq > ? "
                                 "AND data LIKE '%running%' LIMIT 1", (completed,))
            bound = resumed or 1 << 62
            late = txn.scalar("SELECT COUNT(*) FROM events WHERE seq > ? AND seq < ? AND kind "
                              "IN ('agent.created', 'task.joined')", (completed, bound))
            if late:
                problems.append(f"{late} claims or agent creations after Task 0 completed")
        # 14. Every recorded pause names its end, and no tasks wait on each other in a cycle.
        for row in txn.all("SELECT subject, actor FROM events WHERE kind = 'task.paused' "
                           "AND data NOT LIKE '%\"predicate\":{%'"):
            problems.append(f"{row['actor']}'s pause of {row['subject']} names nothing that "
                            "ends it")
        for cycle in wait_graph.cycles(hub.ctx, txn):
            problems.append("tasks wait on each other: " + " → ".join(cycle))
    return problems
