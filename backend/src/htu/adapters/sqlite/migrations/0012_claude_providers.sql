-- Extend Harness checks with an atomic table rebuild; historical records are copied intact.

CREATE TABLE checkpoint_extended(harness TEXT NOT NULL CHECK(harness IN ('claude','codex','zcode','dsh')),
    source_key TEXT NOT NULL CHECK(length(source_key)>0), state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL CHECK(length(updated_at)=24 AND substr(updated_at,24,1)='Z'),
    PRIMARY KEY(harness,source_key)) STRICT;

INSERT INTO checkpoint_extended ("harness","source_key","state_json","updated_at") SELECT "harness","source_key","state_json","updated_at" FROM checkpoint;

DROP TABLE checkpoint;

ALTER TABLE checkpoint_extended RENAME TO checkpoint;

CREATE TABLE diagnostic_log_extended(id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    harness TEXT CHECK(harness IN ('claude','codex','zcode','dsh')), code TEXT NOT NULL,
    detail TEXT NOT NULL CHECK(length(detail)<=500)) STRICT;

INSERT INTO diagnostic_log_extended ("id","occurred_at","harness","code","detail") SELECT "id","occurred_at","harness","code","detail" FROM diagnostic_log;

DROP TABLE diagnostic_log;

ALTER TABLE diagnostic_log_extended RENAME TO diagnostic_log;

CREATE INDEX diagnostic_time ON diagnostic_log(occurred_at,id);

CREATE TABLE harness_selection_extended(harness TEXT NOT NULL CHECK(harness IN ('claude','codex','zcode','dsh')),
    group_id TEXT NOT NULL REFERENCES price_group,
    effective_at TEXT NOT NULL CHECK(length(effective_at)=24 AND substr(effective_at,24,1)='Z'),
    seq INTEGER PRIMARY KEY) STRICT;

INSERT INTO harness_selection_extended ("harness","group_id","effective_at","seq") SELECT "harness","group_id","effective_at","seq" FROM harness_selection;

DROP TABLE harness_selection;

ALTER TABLE harness_selection_extended RENAME TO harness_selection;

CREATE TRIGGER selection_delete BEFORE DELETE ON harness_selection BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER selection_update BEFORE UPDATE ON harness_selection BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE identity_extended(harness TEXT NOT NULL CHECK(harness IN ('claude','codex','zcode','dsh')),
    dedupe_key TEXT NOT NULL CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    billed INTEGER NOT NULL CHECK(billed IN (0,1)),
    first_seen_at TEXT NOT NULL CHECK(length(first_seen_at)=24 AND substr(first_seen_at,24,1)='Z'),
    origin TEXT NOT NULL CHECK(origin IN ('live','history','legacy')),
    PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID;

INSERT INTO identity_extended ("harness","dedupe_key","occurred_at","billed","first_seen_at","origin") SELECT "harness","dedupe_key","occurred_at","billed","first_seen_at","origin" FROM identity;

DROP TABLE identity;

ALTER TABLE identity_extended RENAME TO identity;

CREATE TABLE ledger_extended (
    harness TEXT NOT NULL CHECK(harness IN ('claude','codex','zcode','dsh')),
    dedupe_key TEXT NOT NULL CHECK(length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    session_id TEXT, request_id TEXT NOT NULL, model_id TEXT, model_key TEXT NOT NULL,
    display_name TEXT, source_kind TEXT CHECK(source_kind IS NULL OR
        (harness='codex' AND source_kind IN ('desktop','desktop_subagent'))),
    status TEXT NOT NULL CHECK((harness='zcode' AND status IN ('completed','error','cancelled')) OR
        (harness IN ('claude','codex','dsh') AND status='reported')),
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

INSERT INTO ledger_extended ("harness","dedupe_key","occurred_at","session_id","request_id","model_id","model_key","display_name","source_kind","status","input","cache_read","cache_write","output","reasoning","issues_json","price_group_id","price_version_id","price_revision_id","rule_applied","pico","incomplete","unpriced","billed_at") SELECT "harness","dedupe_key","occurred_at","session_id","request_id","model_id","model_key","display_name","source_kind","status","input","cache_read","cache_write","output","reasoning","issues_json","price_group_id","price_version_id","price_revision_id","rule_applied","pico","incomplete","unpriced","billed_at" FROM ledger;

DROP TABLE ledger;

ALTER TABLE ledger_extended RENAME TO ledger;

CREATE INDEX ledger_group ON ledger(price_group_id,occurred_at);

CREATE INDEX ledger_session ON ledger(harness,session_id,occurred_at);

CREATE INDEX ledger_statistics ON ledger(harness,COALESCE(session_id,''),occurred_at,dedupe_key);

CREATE INDEX ledger_time ON ledger(occurred_at,harness);

CREATE TABLE recalc_batch_extended(id TEXT PRIMARY KEY, harness TEXT NOT NULL CHECK(harness IN ('claude','codex','zcode','dsh')),
    start_at TEXT NOT NULL CHECK(length(start_at)=24 AND substr(start_at,24,1)='Z'),
    end_at TEXT NOT NULL CHECK(length(end_at)=24 AND substr(end_at,24,1)='Z'),
    group_id TEXT NOT NULL, version_id TEXT NOT NULL, record_count INTEGER NOT NULL CHECK(record_count>=0),
    old_pico TEXT NOT NULL CHECK(length(old_pico) BETWEEN 1 AND 40 AND old_pico NOT GLOB '*[^0-9-]*'),
    new_pico TEXT NOT NULL CHECK(length(new_pico) BETWEEN 1 AND 40 AND new_pico NOT GLOB '*[^0-9-]*'),
    unpriced_count INTEGER NOT NULL CHECK(unpriced_count>=0), incomplete_count INTEGER NOT NULL CHECK(incomplete_count>=0),
    fingerprint TEXT NOT NULL, committed_at TEXT NOT NULL CHECK(length(committed_at)=24 AND substr(committed_at,24,1)='Z'),
    operation_id TEXT NOT NULL UNIQUE) STRICT;

INSERT INTO recalc_batch_extended ("id","harness","start_at","end_at","group_id","version_id","record_count","old_pico","new_pico","unpriced_count","incomplete_count","fingerprint","committed_at","operation_id") SELECT "id","harness","start_at","end_at","group_id","version_id","record_count","old_pico","new_pico","unpriced_count","incomplete_count","fingerprint","committed_at","operation_id" FROM recalc_batch;

DROP TABLE recalc_batch;

ALTER TABLE recalc_batch_extended RENAME TO recalc_batch;

CREATE TRIGGER recalc_batch_delete BEFORE DELETE ON recalc_batch BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER recalc_batch_update BEFORE UPDATE ON recalc_batch BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE source_extended(harness TEXT PRIMARY KEY CHECK(harness IN ('claude','codex','zcode','dsh')),
    path TEXT NOT NULL, started_at TEXT CHECK(started_at IS NULL OR
        (length(started_at)=24 AND substr(started_at,24,1)='Z')),
    last_scan_at TEXT CHECK(last_scan_at IS NULL OR (length(last_scan_at)=24 AND substr(last_scan_at,24,1)='Z')),
    status TEXT NOT NULL CHECK(status IN ('pending','unavailable','baselining','watching','degraded')),
    diagnostics_json TEXT NOT NULL, unmetered_json TEXT) STRICT;

INSERT INTO source_extended ("harness","path","started_at","last_scan_at","status","diagnostics_json","unmetered_json") SELECT "harness","path","started_at","last_scan_at","status","diagnostics_json","unmetered_json" FROM source;

DROP TABLE source;

ALTER TABLE source_extended RENAME TO source;

CREATE TABLE wallet_entry_extended(id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendor,
    kind TEXT NOT NULL CHECK(kind IN ('top_up','consumption','consumption_delta','adjustment')),
    amount_pico TEXT NOT NULL CHECK(length(amount_pico) BETWEEN 1 AND 40 AND amount_pico NOT GLOB '*[^0-9-]*'),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    seq INTEGER NOT NULL UNIQUE CHECK(seq>0), operation_id TEXT,
    usage_harness TEXT CHECK(usage_harness IN ('claude','codex','zcode','dsh')), usage_key TEXT,
    related_entry_id TEXT REFERENCES wallet_entry, recalc_batch_id TEXT, note TEXT NOT NULL DEFAULT '',
    unpriced INTEGER NOT NULL DEFAULT 0 CHECK(unpriced IN (0,1)),
    incomplete INTEGER NOT NULL DEFAULT 0 CHECK(incomplete IN (0,1)),
    CHECK((usage_harness IS NULL)=(usage_key IS NULL)),
    CHECK(kind NOT IN ('consumption','consumption_delta') OR usage_key IS NOT NULL)) STRICT;

INSERT INTO wallet_entry_extended ("id","vendor_id","kind","amount_pico","occurred_at","seq","operation_id","usage_harness","usage_key","related_entry_id","recalc_batch_id","note","unpriced","incomplete") SELECT "id","vendor_id","kind","amount_pico","occurred_at","seq","operation_id","usage_harness","usage_key","related_entry_id","recalc_batch_id","note","unpriced","incomplete" FROM wallet_entry;

DROP TABLE wallet_entry;

ALTER TABLE wallet_entry_extended RENAME TO wallet_entry;

CREATE UNIQUE INDEX wallet_consumption_once ON wallet_entry(usage_harness,usage_key) WHERE kind='consumption';

CREATE UNIQUE INDEX wallet_offset_once ON wallet_entry(related_entry_id) WHERE note='late_offset';

CREATE UNIQUE INDEX wallet_operation_once ON wallet_entry(operation_id)
    WHERE kind='top_up' OR (kind='adjustment' AND note='calibration');

CREATE INDEX wallet_vendor ON wallet_entry(vendor_id,seq);

CREATE TRIGGER wallet_entry_delete BEFORE DELETE ON wallet_entry
    WHEN htu_allow_compaction()=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TRIGGER wallet_entry_update BEFORE UPDATE ON wallet_entry BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE provider_alias(harness TEXT NOT NULL CHECK(harness IN ('claude','codex','zcode','dsh')), identifier TEXT NOT NULL CHECK(length(identifier) BETWEEN 1 AND 512), vendor_id TEXT NOT NULL REFERENCES vendor ON DELETE CASCADE, PRIMARY KEY(harness,identifier)) STRICT;
