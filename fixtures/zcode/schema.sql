CREATE TABLE model_usage (
    id TEXT NOT NULL UNIQUE,
    session_id TEXT,
    logical_request_id TEXT,
    attempt_index INTEGER NOT NULL DEFAULT 0,
    model_id TEXT,
    status TEXT NOT NULL,
    completed_at INTEGER,
    raw_usage_json TEXT
);
