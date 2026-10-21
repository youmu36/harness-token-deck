CREATE TABLE source(harness TEXT PRIMARY KEY CHECK(harness IN ('codex','zcode','dsh')),
    path TEXT NOT NULL, started_at TEXT CHECK(started_at IS NULL OR
        (length(started_at)=24 AND substr(started_at,24,1)='Z')),
    last_scan_at TEXT CHECK(last_scan_at IS NULL OR (length(last_scan_at)=24 AND substr(last_scan_at,24,1)='Z')),
    status TEXT NOT NULL CHECK(status IN ('pending','unavailable','baselining','watching','degraded')),
    diagnostics_json TEXT NOT NULL) STRICT;
CREATE TABLE checkpoint(harness TEXT NOT NULL CHECK(harness IN ('codex','zcode','dsh')),
    source_key TEXT NOT NULL CHECK(length(source_key)>0), state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL CHECK(length(updated_at)=24 AND substr(updated_at,24,1)='Z'),
    PRIMARY KEY(harness,source_key)) STRICT;
CREATE TABLE identity(harness TEXT NOT NULL CHECK(harness IN ('codex','zcode','dsh')),
    dedupe_key TEXT NOT NULL CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    billed INTEGER NOT NULL CHECK(billed IN (0,1)),
    first_seen_at TEXT NOT NULL CHECK(length(first_seen_at)=24 AND substr(first_seen_at,24,1)='Z'),
    origin TEXT NOT NULL CHECK(origin IN ('live','history','legacy')),
    PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID;
CREATE TABLE inbox(harness TEXT NOT NULL, dedupe_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    event_json TEXT NOT NULL, collected_at TEXT NOT NULL CHECK(length(collected_at)=24 AND substr(collected_at,24,1)='Z'),
    PRIMARY KEY(harness,dedupe_key), FOREIGN KEY(harness,dedupe_key) REFERENCES identity) STRICT;
CREATE INDEX inbox_order ON inbox(occurred_at,harness,dedupe_key);
CREATE TABLE ledger(harness TEXT NOT NULL CHECK(harness IN ('codex','zcode','dsh')),
    dedupe_key TEXT NOT NULL CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    session_id TEXT, request_id TEXT NOT NULL, model_id TEXT, model_key TEXT NOT NULL,
    display_name TEXT, source_kind TEXT CHECK(source_kind IS NULL OR
        (harness='codex' AND source_kind IN ('desktop','desktop_subagent'))),
    status TEXT NOT NULL CHECK((harness='zcode' AND status IN ('completed','error','cancelled')) OR
        (harness IN ('codex','dsh') AND status='reported')),
    input INTEGER CHECK(input IS NULL OR input BETWEEN 0 AND 9007199254740991),
    cache_read INTEGER CHECK(cache_read IS NULL OR cache_read BETWEEN 0 AND 9007199254740991),
    cache_write INTEGER CHECK(cache_write IS NULL OR cache_write BETWEEN 0 AND 9007199254740991),
    output INTEGER CHECK(output IS NULL OR output BETWEEN 0 AND 9007199254740991),
    reasoning INTEGER CHECK(reasoning IS NULL OR reasoning BETWEEN 0 AND 9007199254740991),
    issues_json TEXT NOT NULL, price_group_id TEXT, price_version_id TEXT, price_revision_id TEXT,
    rule_applied TEXT NOT NULL CHECK(rule_applied IN ('none','base','peak','tier')),
    pico TEXT NOT NULL CHECK(length(pico) BETWEEN 1 AND 40 AND pico NOT GLOB '*[^0-9-]*'),
    incomplete INTEGER NOT NULL CHECK(incomplete IN (0,1)), unpriced INTEGER NOT NULL CHECK(unpriced IN (0,1)),
    billed_at TEXT NOT NULL CHECK(length(billed_at)=24 AND substr(billed_at,24,1)='Z'),
    CHECK(cache_read IS NULL OR input IS NULL OR cache_read<=input),
    CHECK(cache_write IS NULL OR input IS NULL OR cache_write<=input),
    CHECK(cache_read IS NULL OR cache_write IS NULL OR input IS NULL OR cache_read+cache_write<=input),
    CHECK(reasoning IS NULL OR output IS NULL OR reasoning<=output),
    CHECK((model_id IS NULL AND model_key='') OR (model_id IS NOT NULL AND model_key<>'')),
    PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID;
CREATE INDEX ledger_time ON ledger(occurred_at,harness);
CREATE INDEX ledger_session ON ledger(harness,session_id,occurred_at);
CREATE INDEX ledger_group ON ledger(price_group_id,occurred_at);
CREATE INDEX ledger_statistics ON ledger(harness,COALESCE(session_id,''),occurred_at,dedupe_key);
