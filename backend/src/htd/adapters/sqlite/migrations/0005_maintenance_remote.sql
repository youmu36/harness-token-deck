CREATE TABLE recalc_batch(id TEXT PRIMARY KEY, harness TEXT NOT NULL CHECK(harness IN ('codex','zcode','dsh')),
    start_at TEXT NOT NULL CHECK(length(start_at)=24 AND substr(start_at,24,1)='Z'),
    end_at TEXT NOT NULL CHECK(length(end_at)=24 AND substr(end_at,24,1)='Z'),
    group_id TEXT NOT NULL, version_id TEXT NOT NULL, record_count INTEGER NOT NULL CHECK(record_count>=0),
    old_pico TEXT NOT NULL CHECK(length(old_pico) BETWEEN 1 AND 40 AND old_pico NOT GLOB '*[^0-9-]*'),
    new_pico TEXT NOT NULL CHECK(length(new_pico) BETWEEN 1 AND 40 AND new_pico NOT GLOB '*[^0-9-]*'),
    unpriced_count INTEGER NOT NULL CHECK(unpriced_count>=0), incomplete_count INTEGER NOT NULL CHECK(incomplete_count>=0),
    fingerprint TEXT NOT NULL, committed_at TEXT NOT NULL CHECK(length(committed_at)=24 AND substr(committed_at,24,1)='Z'),
    operation_id TEXT NOT NULL UNIQUE) STRICT;
CREATE TABLE recalc_item(batch_id TEXT NOT NULL REFERENCES recalc_batch, harness TEXT NOT NULL,
    dedupe_key TEXT NOT NULL, old_json TEXT NOT NULL, new_json TEXT NOT NULL,
    PRIMARY KEY(batch_id,harness,dedupe_key)) STRICT;
CREATE TABLE retention_run(id TEXT PRIMARY KEY, retention TEXT NOT NULL CHECK(retention IN ('90','180','360','forever')),
    cutoff_at TEXT CHECK(cutoff_at IS NULL OR (length(cutoff_at)=24 AND substr(cutoff_at,24,1)='Z')),
    deleted_ledger INTEGER NOT NULL CHECK(deleted_ledger>=0), expired_inbox INTEGER NOT NULL CHECK(expired_inbox>=0),
    committed_at TEXT NOT NULL CHECK(length(committed_at)=24 AND substr(committed_at,24,1)='Z')) STRICT;
CREATE TABLE remote_account(id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL UNIQUE REFERENCES vendor,
    adapter TEXT NOT NULL CHECK(adapter='newapi'), base_url TEXT NOT NULL,
    auth_mode TEXT NOT NULL CHECK(auth_mode IN ('api_key','access_token','billing')),
    user_id TEXT, credential_ref TEXT NOT NULL, quota_per_unit TEXT,
    refresh_minutes INTEGER NOT NULL CHECK(refresh_minutes BETWEEN 5 AND 60),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1))) STRICT;
CREATE TABLE remote_observation(account_id TEXT NOT NULL REFERENCES remote_account ON DELETE CASCADE,
    fetched_at TEXT NOT NULL CHECK(length(fetched_at)=24 AND substr(fetched_at,24,1)='Z'),
    ok INTEGER NOT NULL CHECK(ok IN (0,1)),
    kind TEXT NOT NULL CHECK(kind IN ('token_remaining','account_remaining','subscription_remaining','unlimited','quota_units')),
    amount_pico TEXT CHECK(amount_pico IS NULL OR (length(amount_pico) BETWEEN 1 AND 40 AND amount_pico NOT GLOB '*[^0-9-]*')),
    raw_quota TEXT, error_code TEXT, error_message TEXT CHECK(error_message IS NULL OR length(error_message)<=200),
    PRIMARY KEY(account_id,fetched_at)) STRICT;
CREATE INDEX remote_observation_recent ON remote_observation(account_id,fetched_at DESC);
CREATE TRIGGER recalc_batch_update BEFORE UPDATE ON recalc_batch BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER recalc_batch_delete BEFORE DELETE ON recalc_batch BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER recalc_item_update BEFORE UPDATE ON recalc_item BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER recalc_item_delete BEFORE DELETE ON recalc_item BEGIN SELECT RAISE(ABORT,'append_only'); END;
