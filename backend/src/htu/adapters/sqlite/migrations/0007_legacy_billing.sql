CREATE TABLE ledger_migration (
    harness TEXT NOT NULL CHECK(harness IN ('codex','zcode','dsh')),
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
    rule_applied TEXT NOT NULL CHECK(rule_applied IN ('none','base','peak','tier','legacy')),
    pico TEXT NOT NULL CHECK(length(pico) BETWEEN 1 AND 40 AND pico NOT GLOB '*[^0-9-]*'),
    incomplete INTEGER NOT NULL CHECK(incomplete IN (0,1)), unpriced INTEGER NOT NULL CHECK(unpriced IN (0,1)),
    billed_at TEXT NOT NULL CHECK(length(billed_at)=24 AND substr(billed_at,24,1)='Z'),
    CHECK(cache_read IS NULL OR input IS NULL OR cache_read<=input),
    CHECK(cache_write IS NULL OR input IS NULL OR cache_write<=input),
    CHECK(cache_read IS NULL OR cache_write IS NULL OR input IS NULL OR cache_read+cache_write<=input),
    CHECK(reasoning IS NULL OR output IS NULL OR reasoning<=output),
    CHECK((model_id IS NULL AND model_key='') OR (model_id IS NOT NULL AND model_key<>'')),
    PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID;
INSERT INTO ledger_migration SELECT * FROM ledger;
DROP TABLE ledger;
ALTER TABLE ledger_migration RENAME TO ledger;
CREATE INDEX ledger_time ON ledger(occurred_at,harness);
CREATE INDEX ledger_session ON ledger(harness,session_id,occurred_at);
CREATE INDEX ledger_group ON ledger(price_group_id,occurred_at);
CREATE INDEX ledger_statistics ON ledger(harness,COALESCE(session_id,''),occurred_at,dedupe_key);
