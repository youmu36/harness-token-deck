CREATE TABLE vendor(id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60),
    status TEXT NOT NULL CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL CHECK(length(created_at)=24 AND substr(created_at,24,1)='Z'),
    seq INTEGER NOT NULL UNIQUE CHECK(seq>0)) STRICT;
CREATE UNIQUE INDEX vendor_active_name ON vendor(name) WHERE status='active';
-- Historical IDs survive physical deletion of vendors without vouchers.
CREATE TABLE vendor_link(seq INTEGER PRIMARY KEY REFERENCES history_seq(id), group_id TEXT NOT NULL,
    vendor_id TEXT, effective_at TEXT NOT NULL CHECK(length(effective_at)=24 AND substr(effective_at,24,1)='Z')) STRICT;
CREATE INDEX vendor_link_timeline ON vendor_link(group_id,effective_at,seq);
CREATE TABLE wallet_entry(id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendor,
    kind TEXT NOT NULL CHECK(kind IN ('top_up','consumption','consumption_delta','adjustment')),
    amount_pico TEXT NOT NULL CHECK(length(amount_pico) BETWEEN 1 AND 40 AND amount_pico NOT GLOB '*[^0-9-]*'),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    seq INTEGER NOT NULL UNIQUE CHECK(seq>0), operation_id TEXT,
    usage_harness TEXT CHECK(usage_harness IN ('codex','zcode','dsh')), usage_key TEXT,
    related_entry_id TEXT REFERENCES wallet_entry, recalc_batch_id TEXT, note TEXT NOT NULL DEFAULT '',
    unpriced INTEGER NOT NULL DEFAULT 0 CHECK(unpriced IN (0,1)),
    incomplete INTEGER NOT NULL DEFAULT 0 CHECK(incomplete IN (0,1)),
    CHECK((usage_harness IS NULL)=(usage_key IS NULL)),
    CHECK(kind NOT IN ('consumption','consumption_delta') OR usage_key IS NOT NULL)) STRICT;
CREATE UNIQUE INDEX wallet_consumption_once ON wallet_entry(usage_harness,usage_key) WHERE kind='consumption';
CREATE UNIQUE INDEX wallet_operation_once ON wallet_entry(operation_id)
    WHERE kind='top_up' OR (kind='adjustment' AND note='calibration');
CREATE UNIQUE INDEX wallet_offset_once ON wallet_entry(related_entry_id) WHERE note='late_offset';
CREATE INDEX wallet_vendor ON wallet_entry(vendor_id,seq);
CREATE TABLE calibration(entry_id TEXT PRIMARY KEY REFERENCES wallet_entry,
    target_pico TEXT NOT NULL CHECK(length(target_pico) BETWEEN 1 AND 40 AND target_pico NOT GLOB '*[^0-9-]*'),
    before_pico TEXT NOT NULL CHECK(length(before_pico) BETWEEN 1 AND 40 AND before_pico NOT GLOB '*[^0-9-]*'),
    watermark_seq INTEGER NOT NULL CHECK(watermark_seq>=0),
    at TEXT NOT NULL CHECK(length(at)=24 AND substr(at,24,1)='Z')) STRICT;
CREATE TABLE wallet_total(vendor_id TEXT PRIMARY KEY REFERENCES vendor ON DELETE CASCADE,
    r TEXT NOT NULL CHECK(length(r) BETWEEN 1 AND 40 AND r NOT GLOB '*[^0-9-]*'),
    c TEXT NOT NULL CHECK(length(c) BETWEEN 1 AND 40 AND c NOT GLOB '*[^0-9-]*'),
    a TEXT NOT NULL CHECK(length(a) BETWEEN 1 AND 40 AND a NOT GLOB '*[^0-9-]*'),
    last_seq INTEGER NOT NULL CHECK(last_seq>=0)) STRICT;
CREATE TABLE vendor_legacy_record(id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendor,
    kind TEXT NOT NULL CHECK(kind='calibration'),
    at TEXT NOT NULL CHECK(length(at)=24 AND substr(at,24,1)='Z'),
    amount_pico TEXT NOT NULL CHECK(length(amount_pico) BETWEEN 1 AND 40 AND amount_pico NOT GLOB '*[^0-9-]*'),
    source_json TEXT NOT NULL) STRICT;
CREATE TRIGGER wallet_entry_update BEFORE UPDATE ON wallet_entry BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER wallet_entry_delete BEFORE DELETE ON wallet_entry BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_update BEFORE UPDATE ON calibration BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER calibration_delete BEFORE DELETE ON calibration BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER vendor_link_update BEFORE UPDATE ON vendor_link BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER vendor_link_delete BEFORE DELETE ON vendor_link BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER vendor_legacy_update BEFORE UPDATE ON vendor_legacy_record BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER vendor_legacy_delete BEFORE DELETE ON vendor_legacy_record BEGIN SELECT RAISE(ABORT,'append_only'); END;
