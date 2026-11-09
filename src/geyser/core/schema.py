"""The ledger schema.

The ledger holds coordination: claims, memberships, waits, receipts, delivery
state, gate states and indexes (DESIGN.md section 4.1). Content lives in native
files whose hashes the ledger records. JSON columns hold small structured values.
"""

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    kind TEXT PRIMARY KEY,
    next INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    subject TEXT,
    actor   TEXT,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_subject ON events (subject, seq);

-- Idempotent commands and native-call receipts (section 13.4).
CREATE TABLE IF NOT EXISTS receipts (
    key            TEXT PRIMARY KEY,
    operation      TEXT NOT NULL,
    actor          TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    state          TEXT NOT NULL,          -- committed | draining
    reply          TEXT,
    activation_id  TEXT,
    at             TEXT NOT NULL
);

-- Tasks (section 5).
CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    number             INTEGER NOT NULL UNIQUE,
    title              TEXT NOT NULL,
    kind               TEXT NOT NULL,
    policy             TEXT,
    priority           INTEGER NOT NULL,
    state              TEXT NOT NULL,      -- open | set_aside | active | waiting |
                                           -- completing | completed | retired
    revision           INTEGER NOT NULL,
    publisher          TEXT NOT NULL,
    parent_task        TEXT,
    repair_of          TEXT,
    recruitment        TEXT NOT NULL DEFAULT 'undecided',  -- undecided | yes | no
    completed_revision INTEGER,
    completed_at       TEXT,
    evidence_valid     INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_revisions (
    task_id    TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    hash       TEXT NOT NULL,
    author     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    change     TEXT NOT NULL,              -- initial | cosmetic | additive | material
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);

-- Team places (section 6.2) and memberships.
CREATE TABLE IF NOT EXISTS opportunities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    role       TEXT NOT NULL,              -- executor | collaborator
    state      TEXT NOT NULL,              -- open | filled | cancelled
    invitation TEXT,
    created_at TEXT NOT NULL,
    filled_by  TEXT,
    closed_at  TEXT
);
CREATE INDEX IF NOT EXISTS opportunities_open ON opportunities (state, task_id);

CREATE TABLE IF NOT EXISTS memberships (
    task_id       TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    role          TEXT NOT NULL,           -- executor | collaborator
    state         TEXT NOT NULL,           -- active | waiting | left | ended
    plan          TEXT,
    checkpoint    TEXT,
    predicate     TEXT,
    wait_reason   TEXT,
    waiting_since TEXT,
    joined_at     TEXT NOT NULL,
    ended_at      TEXT,
    PRIMARY KEY (task_id, agent_id)
);

-- Agents (section 6) and their conversations.
CREATE TABLE IF NOT EXISTS agents (
    id                       TEXT PRIMARY KEY,
    kind                     TEXT NOT NULL,     -- worker | reviewer | integrator
    number                   INTEGER NOT NULL,
    backend                  TEXT NOT NULL,
    state                    TEXT NOT NULL,     -- active | parked | retired
    current_task             TEXT,
    current_review           TEXT,
    current_batch            TEXT,
    intent                   TEXT NOT NULL DEFAULT 'none',
                                                -- none | start | continue | resume
    fence                    INTEGER NOT NULL DEFAULT 0,
    conversation_id          TEXT,
    conversation_task        TEXT,
    conversation_started_at  TEXT,
    conversation_last_turn   TEXT,
    conversation_digest      TEXT,
    conversation_start_tokens INTEGER,
    conversation_tokens      INTEGER,
    rotation_requested       INTEGER NOT NULL DEFAULT 0,
    review_wait              TEXT,              -- a reviewer's pause (section 8.4)
    move_to                  TEXT,              -- a requested backend move (section 6.1)
    view_baseline            TEXT,
    failures                 INTEGER NOT NULL DEFAULT 0,
    failure_signature        TEXT,
    backoff_until            TEXT,
    created_at               TEXT NOT NULL,
    last_activation_at       TEXT,
    retired_at               TEXT
);

CREATE TABLE IF NOT EXISTS activations (
    id               TEXT PRIMARY KEY,
    agent_id         TEXT NOT NULL,
    purpose          TEXT NOT NULL,   -- task | message | review | answer | integration
    task_id          TEXT,
    review_id        TEXT,
    batch_id         TEXT,
    state            TEXT NOT NULL,   -- planned | running | draining | ended
    outcome          TEXT,            -- completed | interrupted | failed | closed_unknown |
                                      -- abandoned
    fence            INTEGER NOT NULL,
    scope            TEXT,
    conversation_id  TEXT,
    new_conversation INTEGER NOT NULL DEFAULT 0,
    drain            TEXT,
    detail           TEXT,
    planned_at       TEXT NOT NULL,
    started_at       TEXT,
    ended_at         TEXT
);
CREATE INDEX IF NOT EXISTS activations_live ON activations (state, agent_id);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    activation_id TEXT,
    agent_id      TEXT,
    backend       TEXT NOT NULL,
    account       TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    requests      INTEGER NOT NULL DEFAULT 0,
    at            TEXT NOT NULL
);

-- Candidates, validations and reviews (section 8).
CREATE TABLE IF NOT EXISTS candidates (
    id               TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,
    task_revision    INTEGER NOT NULL,
    kind             TEXT NOT NULL,
    policy           TEXT NOT NULL,
    submitter        TEXT NOT NULL,
    authors          TEXT NOT NULL,
    paths            TEXT NOT NULL DEFAULT '[]',
    replaces         TEXT,
    corrected_by     TEXT,
    base_commit      TEXT NOT NULL,
    manifest_hash    TEXT NOT NULL,
    state            TEXT NOT NULL,   -- validating | reviewing | accepted | rejected |
                                      -- withdrawn | invalidated | returned | published
    reason           TEXT,
    required         INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    decided_at       TEXT,
    published_commit TEXT,
    publication_id   TEXT,
    batch_id         TEXT,
    deferred         TEXT              -- batch deferral reason (section 9.2)
);
CREATE INDEX IF NOT EXISTS candidates_by_task ON candidates (task_id, state);

CREATE TABLE IF NOT EXISTS validations (
    id              TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    policy          TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    phase           TEXT NOT NULL,    -- pending | running | queued | done
    verdict         TEXT,
    summary         TEXT,
    report_path     TEXT,
    next_attempt_at TEXT,
    created_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS validations_by_candidate ON validations (candidate_id, attempt);

CREATE TABLE IF NOT EXISTS reviews (
    id                TEXT PRIMARY KEY,
    candidate_id      TEXT NOT NULL UNIQUE,
    reviewer          TEXT,
    state             TEXT NOT NULL,  -- waiting | open | accepted | rejected | cancelled
    verdict           TEXT,
    report_path       TEXT,
    integration_notes TEXT,
    created_at        TEXT NOT NULL,
    decided_at        TEXT
);

-- Publication (section 9.3).
CREATE TABLE IF NOT EXISTS publications (
    id            TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,      -- direct | batch | import
    members       TEXT NOT NULL,
    parent_commit TEXT,
    commit_id     TEXT,
    state         TEXT NOT NULL,      -- intent | committed | abandoned
    created_at    TEXT NOT NULL,
    committed_at  TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,        -- open | published | empty | abandoned
    notes_only  INTEGER NOT NULL DEFAULT 0,
    members     TEXT NOT NULL,
    deferred    TEXT NOT NULL DEFAULT '[]',
    final_tree  TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workspace_bases (
    agent_id     TEXT PRIMARY KEY,
    commit_id    TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);

-- Completion (section 5.7).
CREATE TABLE IF NOT EXISTS declarations (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    declarer      TEXT NOT NULL,
    evidence      TEXT NOT NULL,
    state         TEXT NOT NULL,      -- awaiting_feedback | deciding | finalized |
                                      -- continued | cancelled
    feedback_from TEXT,
    feedback      TEXT,
    resolutions   TEXT,
    created_at    TEXT NOT NULL,
    decided_at    TEXT
);

-- Communication (section 11).
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,        -- talk | operator
    sender      TEXT NOT NULL,
    sender_role TEXT NOT NULL,        -- operator | agent | unknown
    form        TEXT,                 -- talk: notice | question | answer (section 11.2)
    answers     TEXT,                 -- an answer: the question it answers
    recipient   TEXT NOT NULL,
    body_path   TEXT NOT NULL,
    body_hash   TEXT NOT NULL,
    summary     TEXT NOT NULL,
    client_key  TEXT,
    withdrawn   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_client_key
    ON messages (sender, client_key) WHERE client_key IS NOT NULL;

-- What the Waker already judged for each subscribed wait (section 12).
CREATE TABLE IF NOT EXISTS subscription_marks (
    agent_id TEXT NOT NULL,
    task_id  TEXT NOT NULL,           -- the waiter's task
    provider TEXT NOT NULL,           -- the subscribed task
    since    TEXT NOT NULL,           -- when the wait began: one wait, one mark
    through  TEXT NOT NULL,           -- the newest publication time already judged
    PRIMARY KEY (agent_id, task_id, provider, since)
);

CREATE TABLE IF NOT EXISTS inbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL,
    item          TEXT NOT NULL,      -- message | notice
    ref           TEXT,
    task_id       TEXT,
    summary       TEXT NOT NULL,
    wakes         INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL,      -- pending | prepared | delivered | uncertain | dropped
    activation_id TEXT,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT
);
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox (agent_id, state);

CREATE TABLE IF NOT EXISTS requests (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,       -- question | material
    key          TEXT NOT NULL UNIQUE,
    asker        TEXT NOT NULL,
    task_id      TEXT,
    revision     INTEGER,
    title        TEXT NOT NULL,
    body_path    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state        TEXT NOT NULL,       -- open | answered | resolved
    answer_path  TEXT,
    answered_by  TEXT,
    created_at   TEXT NOT NULL,
    answered_at  TEXT
);

CREATE TABLE IF NOT EXISTS request_waiters (
    request_id TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    PRIMARY KEY (request_id, agent_id)
);

-- Decisions (section 6.3, 6.5).
CREATE TABLE IF NOT EXISTS decisions (
    id             TEXT PRIMARY KEY,
    purpose        TEXT NOT NULL,     -- assignment | recruitment | wake
    task_id        TEXT NOT NULL,     -- '' for a wake decision, which covers many waits
    opportunity_id INTEGER,
    task_revision  INTEGER NOT NULL,
    state          TEXT NOT NULL,     -- pending | running | done | failed | unknown | obsolete
    attempts       INTEGER NOT NULL DEFAULT 0,
    offered        TEXT,
    answer         TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT
);

-- Operator control (section 15) and account holds (section 14).
CREATE TABLE IF NOT EXISTS controls (
    scope TEXT PRIMARY KEY,           -- run | agent:<id> | group:<backend>[.<role>s] |
                                      -- group:integrator
    state TEXT NOT NULL,              -- running | paused | pause_after_turn | stopped
    at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holds (
    account TEXT PRIMARY KEY,         -- the backend name
    reason  TEXT NOT NULL,
    at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_notices (
    signature TEXT PRIMARY KEY,
    subject   TEXT,
    message   TEXT NOT NULL,
    count     INTEGER NOT NULL,
    first_at  TEXT NOT NULL,
    last_at   TEXT NOT NULL
);

-- Shared knowledge (section 10).
CREATE TABLE IF NOT EXISTS drafts (
    task_id    TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    author     TEXT NOT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);

CREATE TABLE IF NOT EXISTS records (
    name       TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    author     TEXT NOT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, revision)
);

CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    title         TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""

ADDED_COLUMNS = (
    ("agents", "move_to", "TEXT"),
    ("messages", "form", "TEXT"),
    ("messages", "answers", "TEXT"),
)
"""Columns added after a table first shipped: a ledger created earlier gains them."""
