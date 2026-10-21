CREATE TABLE collection_activity (
    id INTEGER PRIMARY KEY CHECK(id=1),
    generation INTEGER NOT NULL DEFAULT 0,
    last_at TEXT
);
INSERT INTO collection_activity(id) VALUES(1);
CREATE TABLE reconciliation_session (
    account_id TEXT PRIMARY KEY REFERENCES remote_account(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL DEFAULT -1,
    remote_changed_at TEXT NOT NULL,
    remote_amount TEXT NOT NULL,
    settled_amount TEXT,
    settled_at TEXT
);
