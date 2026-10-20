-- New readers accept both old JSON and bounded versioned frames during backfill.
CREATE TABLE usage_hour_model(
    harness TEXT NOT NULL, bucket TEXT NOT NULL, model_key TEXT NOT NULL,
    part INTEGER NOT NULL CHECK(part>=0), payload BLOB NOT NULL,
    PRIMARY KEY(harness,bucket,model_key,part)
) STRICT, WITHOUT ROWID;
CREATE INDEX usage_hour_model_filter ON usage_hour_model(model_key,bucket,harness);
CREATE INDEX usage_hour_time ON usage_hour_model(bucket,harness,model_key);
INSERT OR IGNORE INTO usage_hour_dirty SELECT harness,bucket FROM usage_hour;
ALTER TABLE usage_archive ADD COLUMN model_key TEXT;
ALTER TABLE usage_archive ADD COLUMN encoding INTEGER NOT NULL DEFAULT 1;
ALTER TABLE usage_archive ADD COLUMN minute_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_archive ADD COLUMN hours_payload BLOB;
ALTER TABLE usage_archive ADD COLUMN checksum TEXT;
CREATE INDEX usage_archive_model ON usage_archive(model_key,start_at,harness) WHERE model_key IS NOT NULL;
CREATE INDEX usage_archive_source_model ON usage_archive(harness,model_key,start_at) WHERE model_key IS NOT NULL;
CREATE INDEX usage_archive_legacy ON usage_archive(start_at,end_at,harness) WHERE model_key IS NULL;

ALTER TABLE recalc_item ADD COLUMN payload BLOB;
ALTER TABLE recalc_batch ADD COLUMN changed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recalc_batch ADD COLUMN unchanged_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE recalc_batch ADD COLUMN identity_fingerprint TEXT NOT NULL DEFAULT '';
UPDATE recalc_batch SET changed_count=record_count;
DROP TRIGGER recalc_item_update;
CREATE TRIGGER recalc_item_update BEFORE UPDATE ON recalc_item
    WHEN htu_allow_compaction()=0 OR NEW.batch_id<>OLD.batch_id OR NEW.harness<>OLD.harness
      OR NEW.dedupe_key<>OLD.dedupe_key
      OR htu_audit_equivalent(OLD.old_json,OLD.new_json,OLD.payload,NEW.old_json,NEW.new_json,NEW.payload)=0
    BEGIN SELECT RAISE(ABORT,'append_only'); END;

CREATE TABLE wallet_sequence(id INTEGER PRIMARY KEY CHECK(id=1),value INTEGER NOT NULL CHECK(value>=0)) STRICT;
INSERT INTO wallet_sequence SELECT 1,COALESCE(MAX(n),0) FROM (
    SELECT seq n FROM wallet_entry UNION ALL SELECT last_seq FROM wallet_rollup
    UNION ALL SELECT last_seq FROM wallet_total UNION ALL SELECT watermark_seq FROM calibration
    UNION ALL SELECT seq FROM wallet_activity UNION ALL SELECT related_seq FROM wallet_activity);
CREATE TRIGGER wallet_sequence_append AFTER INSERT ON wallet_entry BEGIN
    UPDATE wallet_sequence SET value=MAX(value,NEW.seq) WHERE id=1;
END;
CREATE TRIGGER wallet_sequence_rollup AFTER INSERT ON wallet_rollup BEGIN
    UPDATE wallet_sequence SET value=MAX(value,NEW.last_seq) WHERE id=1;
END;
CREATE TABLE wallet_summary(
    vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,
    unpriced INTEGER NOT NULL DEFAULT 0 CHECK(unpriced>=0),
    incomplete INTEGER NOT NULL DEFAULT 0 CHECK(incomplete>=0), calibrated_at TEXT
) STRICT;
INSERT INTO wallet_summary(vendor_id) SELECT id FROM vendor;
UPDATE wallet_summary SET
    unpriced=COALESCE((SELECT SUM(unpriced_count) FROM wallet_rollup WHERE vendor_id=wallet_summary.vendor_id),0)
        +COALESCE((SELECT SUM(unpriced) FROM (SELECT vendor_id,unpriced,ROW_NUMBER() OVER
          (PARTITION BY usage_harness,usage_key ORDER BY seq DESC) n FROM wallet_entry
          WHERE kind IN ('consumption','consumption_delta')) q WHERE q.vendor_id=wallet_summary.vendor_id AND n=1),0),
    incomplete=COALESCE((SELECT SUM(incomplete_count) FROM wallet_rollup WHERE vendor_id=wallet_summary.vendor_id),0)
        +COALESCE((SELECT SUM(incomplete) FROM (SELECT vendor_id,incomplete,ROW_NUMBER() OVER
          (PARTITION BY usage_harness,usage_key ORDER BY seq DESC) n FROM wallet_entry
          WHERE kind IN ('consumption','consumption_delta')) q WHERE q.vendor_id=wallet_summary.vendor_id AND n=1),0),
    calibrated_at=(SELECT MAX(at) FROM (SELECT occurred_at at FROM wallet_entry WHERE
        vendor_id=wallet_summary.vendor_id AND note='calibration' UNION ALL SELECT at FROM vendor_legacy_record
        WHERE vendor_id=wallet_summary.vendor_id));
CREATE TRIGGER wallet_summary_vendor AFTER INSERT ON vendor BEGIN
    INSERT INTO wallet_summary(vendor_id) VALUES(NEW.id);
END;
CREATE INDEX wallet_quality_lookup ON wallet_entry(usage_harness,usage_key,seq DESC)
    WHERE kind IN ('consumption','consumption_delta');
CREATE TRIGGER wallet_summary_append AFTER INSERT ON wallet_entry BEGIN
    UPDATE wallet_summary SET
      unpriced=unpriced+CASE WHEN NEW.kind IN ('consumption','consumption_delta') THEN NEW.unpriced
        -COALESCE((SELECT unpriced FROM wallet_entry WHERE usage_harness=NEW.usage_harness
         AND usage_key=NEW.usage_key AND kind IN ('consumption','consumption_delta') AND seq<NEW.seq ORDER BY seq DESC LIMIT 1),0) ELSE 0 END,
      incomplete=incomplete+CASE WHEN NEW.kind IN ('consumption','consumption_delta') THEN NEW.incomplete
        -COALESCE((SELECT incomplete FROM wallet_entry WHERE usage_harness=NEW.usage_harness
         AND usage_key=NEW.usage_key AND kind IN ('consumption','consumption_delta') AND seq<NEW.seq ORDER BY seq DESC LIMIT 1),0) ELSE 0 END,
      calibrated_at=CASE WHEN NEW.note='calibration' THEN MAX(COALESCE(calibrated_at,''),NEW.occurred_at) ELSE calibrated_at END
    WHERE vendor_id=NEW.vendor_id;
END;
CREATE TRIGGER wallet_summary_legacy AFTER INSERT ON vendor_legacy_record BEGIN
    UPDATE wallet_summary SET calibrated_at=MAX(COALESCE(calibrated_at,''),NEW.at) WHERE vendor_id=NEW.vendor_id;
END;
