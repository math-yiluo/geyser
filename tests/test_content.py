"""Workspaces, capture rules, drafts, shared records and sources."""

from __future__ import annotations

import pytest

from geyser.core import agents, teams
from geyser.gateway import Gateway
from geyser.hub import Hub
from geyser.ids import Kind
from geyser.integration.direct import integrate_next
from tests.conftest import prepare_workspace, start_turn
from tests.test_core_flow import publish_task_zero


def worker_on_task_zero(hub: Hub) -> str:
    task_id = publish_task_zero(hub)
    with hub.transaction() as txn:
        worker = agents.create(hub.ctx, txn, Kind.WORKER)
        place = txn.scalar("SELECT id FROM opportunities WHERE task_id = ?", (task_id,))
        teams.fill(hub.ctx, txn, place, worker, by="test")
    prepare_workspace(hub, worker)
    return worker


def test_refresh_keeps_local_edits_and_reports_conflicts(hub: Hub) -> None:
    library = hub.library
    workspace = hub.layout.workspace("worker-x")
    base = library.checkout_workspace(workspace)
    repo = library.repo
    from geyser.content.git import Entry
    from geyser.integration import publication

    blobs = {"a.txt": Entry("100644", repo.write_blob(b"published a\n")),
             "b.txt": Entry("100644", repo.write_blob(b"published b\n"))}
    (workspace / "b.txt").write_text("my own b\n", encoding="utf-8")
    tree = repo.build_tree(base, blobs)
    publication.publish(hub, "direct", [], tree, base, "test publication")
    report = library.refresh_workspace(workspace, base)
    assert report.updated == ["a.txt"]
    assert report.conflicts == ["b.txt"]
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "my own b\n"
    copy = workspace / ".private" / "refresh" / report.head[:12] / "b.txt"
    assert copy.read_text(encoding="utf-8") == "published b\n"


async def test_capture_refuses_private_paths_links_and_wiki(hub: Hub, gateway: Gateway) -> None:
    worker = worker_on_task_zero(hub)
    actor = start_turn(hub, worker, task_id="task-0")
    workspace = hub.layout.workspace(worker)
    assert (workspace / "wiki" / "README.md").exists()  # The starter README.
    (workspace / "wiki" / "page.md").write_text("# Page\n", encoding="utf-8")
    refused = await gateway.call(actor, "stage", {"kind": "general.work",
                                                  "paths": [".private/notes"],
                                                  "introduction": "x"}, call_id="1")
    assert refused["error"] == "invalid_argument"
    wiki = await gateway.call(actor, "stage", {"kind": "general.work", "paths": ["wiki"],
                                               "introduction": "x"}, call_id="2")
    assert "wiki_submit" in wiki["message"]
    nothing = await gateway.call(actor, "stage", {"kind": "general.work", "paths": ["absent"],
                                                  "introduction": "x"}, call_id="3")
    assert "nothing to contribute" in nothing["message"]
    empty = workspace / "wiki" / "empty.md"
    empty.write_text("  \n", encoding="utf-8")
    refused_empty = await gateway.call(actor, "wiki_submit", {"paths": ["wiki/empty.md"],
                                                              "introduction": "x"}, call_id="3b")
    assert "is empty" in refused_empty["message"]
    empty.unlink()
    note = await gateway.call(actor, "wiki_submit", {"paths": ["wiki/page.md"],
                                                     "introduction": "A lesson."}, call_id="4")
    assert note["candidate"] == "cand-0" and note["policy"] == "core.note@1"
    report = await gateway.call(actor, "stage", {"kind": "general.report",
                                                 "paths": ["wiki/page.md"],
                                                 "introduction": "x"}, call_id="5")
    assert report["error"] == "invalid_argument"


async def test_deletions_are_published(hub: Hub, gateway: Gateway) -> None:
    worker = worker_on_task_zero(hub)
    actor = start_turn(hub, worker, task_id="task-0")
    workspace = hub.layout.workspace(worker)
    (workspace / "doc").mkdir()
    (workspace / "doc" / "x.txt").write_text("x\n", encoding="utf-8")
    first = await gateway.call(actor, "stage", {"kind": "general.work", "paths": ["doc"],
                                                "introduction": "x"}, call_id="1")
    assert first["changed"] == 1
    with hub.transaction() as txn:
        txn.execute("UPDATE candidates SET state = 'accepted', decided_at = ? WHERE id = ?",
                    (txn.now, first["candidate"]))
        txn.execute("UPDATE reviews SET state = 'accepted' WHERE candidate_id = ?",
                    (first["candidate"],))
    await integrate_next(hub)
    assert (hub.layout.library / "doc" / "x.txt").exists()
    with hub.transaction() as txn:
        txn.execute("UPDATE workspace_bases SET commit_id = ? WHERE agent_id = ?",
                    (hub.library.head(), worker))
    (workspace / "doc" / "x.txt").unlink()
    second = await gateway.call(actor, "stage", {"kind": "general.work", "paths": ["doc"],
                                                 "introduction": "remove"}, call_id="2")
    assert second["deleted"] == 1 and second["changed"] == 0


async def test_drafts_records_and_sources(hub: Hub, gateway: Gateway) -> None:
    worker = worker_on_task_zero(hub)
    actor = start_turn(hub, worker, task_id="task-0")
    draft_dir = hub.layout.private(worker) / "drafts" / "plan"
    draft_dir.mkdir(parents=True)
    (draft_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    shared = await gateway.call(actor, "draft_update", {"name": "plan"}, call_id="1")
    assert shared["draft_revision"] == 0
    stale = await gateway.call(actor, "draft_update", {"name": "plan"}, call_id="2")
    assert stale["error"] == "conflict"
    record = await gateway.call(actor, "record_update", {"name": "roadmap", "content": "{}",
                                                         "format": "json"}, call_id="3")
    assert record["revision"] == 0
    status = await gateway.call(actor, "record_status", {"name": "roadmap"}, call_id="4")
    assert status["revision"] == 0
    (hub.layout.sources / "paper.txt").write_text("content\n", encoding="utf-8")
    source = await gateway.call(actor, "source_register", {"path": "paper.txt",
                                                           "title": "A paper"}, call_id="5")
    assert source["source_id"] == "source-0"
    again = await gateway.call(actor, "source_register", {"path": "paper.txt",
                                                          "title": "A paper"}, call_id="6")
    assert again["source_id"] == "source-0" and again["new"] is False
    (hub.layout.sources / "paper.txt").write_text("second edition\n", encoding="utf-8")
    edition = await gateway.call(actor, "source_register", {"path": "paper.txt",
                                                            "title": "A paper"}, call_id="7")
    assert edition["source_id"] == "source-1"


@pytest.mark.parametrize("path", ["../escape", "/abs", "a\\b", "a/./b", ""])
def test_safe_relative_paths_refuse_escapes(path: str) -> None:
    from geyser.errors import InvalidArgument
    from geyser.layout import safe_relative_path

    with pytest.raises(InvalidArgument):
        safe_relative_path(path)
