-- Local wallet vouchers remain immutable estimates. Account evidence has its own lifetime.
INSERT INTO revision VALUES ('account',0),('quality',0),('display',0),('models',0),('ownership',0),('auxiliary',0);
CREATE TABLE credential_reference_journal(
    reference TEXT PRIMARY KEY,
    account_id TEXT NOT NULL
) STRICT;
INSERT INTO credential_reference_journal SELECT credential_ref,id FROM remote_account;
CREATE TABLE account_segment(
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendor(id),
    kind TEXT NOT NULL CHECK(kind IN ('legacy','local','remote')),
    identity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    anchor_r TEXT NOT NULL,
    anchor_c TEXT NOT NULL,
    anchor_a TEXT NOT NULL,
    initial_balance TEXT NOT NULL,
    r TEXT NOT NULL DEFAULT '0',
    c TEXT NOT NULL DEFAULT '0',
    a TEXT NOT NULL DEFAULT '0',
    methods TEXT NOT NULL DEFAULT '[]'
) STRICT;
CREATE UNIQUE INDEX account_segment_active ON account_segment(vendor_id) WHERE ended_at IS NULL;
INSERT INTO account_segment(id,vendor_id,kind,identity,started_at,anchor_r,anchor_c,anchor_a,initial_balance)
 SELECT 'legacy:'||v.id,v.id,'legacy','legacy-local-v1',v.created_at,'0','0','0','0' FROM vendor v;
CREATE TABLE account_endpoint(
    id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES account_segment(id),
    observed_at TEXT NOT NULL,
    b TEXT NOT NULL,
    u TEXT,
    unit_version TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(segment_id,observed_at)
) STRICT;
CREATE INDEX account_endpoint_order ON account_endpoint(segment_id,observed_at DESC);
CREATE TABLE account_interval(
    id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES account_segment(id),
    start_endpoint TEXT NOT NULL REFERENCES account_endpoint(id),
    end_endpoint TEXT NOT NULL REFERENCES account_endpoint(id),
    e TEXT NOT NULL,
    e_watermark INTEGER NOT NULL,
    r TEXT NOT NULL,
    c TEXT NOT NULL,
    a TEXT NOT NULL,
    raw_inflow TEXT NOT NULL,
    unresolved TEXT NOT NULL,
    method TEXT NOT NULL,
    algorithm TEXT NOT NULL DEFAULT 'account-v1',
    revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE(segment_id,start_endpoint),
    UNIQUE(segment_id,end_endpoint)
) STRICT;
CREATE INDEX account_interval_segment ON account_interval(segment_id,id);
CREATE TRIGGER account_interval_mutated AFTER UPDATE ON account_interval BEGIN
 INSERT INTO runtime_state(key,value) VALUES('account_interval_mutation:'||new.segment_id,'1')
 ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT);
END;
CREATE TABLE account_posting(
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendor(id),
    segment_id TEXT REFERENCES account_segment(id),
    interval_id TEXT REFERENCES account_interval(id),
    occurred_at TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    r TEXT NOT NULL,
    c TEXT NOT NULL,
    a TEXT NOT NULL,
    note TEXT NOT NULL,
    original_id TEXT,
    operation_id TEXT NOT NULL UNIQUE,
    matched TEXT NOT NULL DEFAULT '0',
    revision INTEGER NOT NULL DEFAULT 1
) STRICT;
CREATE INDEX account_posting_vendor ON account_posting(vendor_id,posted_at DESC,id);
CREATE INDEX account_posting_match ON account_posting(segment_id,occurred_at);
CREATE TABLE account_balance_override(
    vendor_id TEXT PRIMARY KEY REFERENCES vendor(id),
    endpoint_id TEXT NOT NULL REFERENCES account_endpoint(id),
    balance TEXT NOT NULL,
    posted_at TEXT NOT NULL
) STRICT;
ALTER TABLE remote_observation ADD COLUMN cumulative_used TEXT;
ALTER TABLE remote_observation ADD COLUMN usage_scope TEXT;
ALTER TABLE remote_observation ADD COLUMN original_currency TEXT;
ALTER TABLE remote_observation ADD COLUMN conversion_rule TEXT;
CREATE TABLE source_identity_fact(
    harness TEXT NOT NULL,
    dedupe_key BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    conflicted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(harness,dedupe_key)
) STRICT;
CREATE TABLE wallet_daily_close(
    vendor_id TEXT NOT NULL REFERENCES vendor(id),
    day TEXT NOT NULL,
    timezone TEXT NOT NULL,
    original TEXT NOT NULL,
    current TEXT NOT NULL,
    seq INTEGER NOT NULL,
    count INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(vendor_id,day,timezone)
) STRICT;
CREATE TABLE wallet_daily_correction(
    id INTEGER PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendor(id),
    day TEXT NOT NULL,
    timezone TEXT NOT NULL,
    before_pico TEXT NOT NULL,
    after_pico TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    revision INTEGER NOT NULL
) STRICT;
ALTER TABLE wallet_daily_correction ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
CREATE TABLE wallet_daily_dirty(vendor_id TEXT NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,
    day TEXT NOT NULL, timezone TEXT NOT NULL, PRIMARY KEY(vendor_id,day,timezone)) STRICT;
-- At most one indexed source day is expanded per backfill batch, independently of startup.
CREATE TABLE wallet_daily_backfill(id INTEGER PRIMARY KEY CHECK(id=1),last_vendor TEXT NOT NULL DEFAULT '',
    last_at TEXT NOT NULL DEFAULT '',cold_id INTEGER NOT NULL DEFAULT 0,hot_done INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0) STRICT;
INSERT INTO wallet_daily_backfill(id) VALUES(1);
ALTER TABLE title_request ADD COLUMN model_id TEXT;
ALTER TABLE title_request ADD COLUMN cost_pico TEXT NOT NULL DEFAULT '1000000000';
ALTER TABLE title_request ADD COLUMN fee_version TEXT NOT NULL DEFAULT 'codex-title-0.001usd-v1';
ALTER TABLE title_request ADD COLUMN attribution_revision INTEGER NOT NULL DEFAULT -1;
CREATE INDEX title_window ON title_request(occurred_at);
CREATE INDEX title_attribution ON title_request(attribution_revision,occurred_at) WHERE wallet_entry_id IS NULL;
INSERT OR IGNORE INTO preference VALUES('accountingTimezone','"Asia/Shanghai"');
INSERT OR IGNORE INTO preference VALUES('unpricedNoticeDismissed','""');

CREATE INDEX ledger_pending_prices ON ledger(harness,occurred_at) WHERE unpriced=1;
CREATE INDEX wallet_estimate_interval ON wallet_entry(vendor_id,occurred_at) WHERE kind IN ('consumption','consumption_delta');
