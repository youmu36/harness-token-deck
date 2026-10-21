-- Supplemental source evidence never changes historical costs or request identities.
CREATE TABLE usage_annotation(
    harness TEXT NOT NULL,
    dedupe_key BLOB NOT NULL,
    compacted INTEGER NOT NULL DEFAULT 0 CHECK(compacted IN (0,1)),
    cache_read INTEGER CHECK(cache_read IS NULL OR cache_read BETWEEN 0 AND 9007199254740991),
    PRIMARY KEY(harness,dedupe_key),
    FOREIGN KEY(harness,dedupe_key) REFERENCES identity(harness,dedupe_key) ON DELETE CASCADE
) STRICT, WITHOUT ROWID;

ALTER TABLE output_live ADD COLUMN timing_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE output_live ADD COLUMN timed_output TEXT NOT NULL DEFAULT '0';
ALTER TABLE output_live ADD COLUMN decode_ms TEXT NOT NULL DEFAULT '0';
ALTER TABLE output_pending ADD COLUMN timed_output TEXT NOT NULL DEFAULT '0';
ALTER TABLE output_pending ADD COLUMN decode_ms TEXT NOT NULL DEFAULT '0';
ALTER TABLE output_reference ADD COLUMN timed_output TEXT NOT NULL DEFAULT '0';
ALTER TABLE output_reference ADD COLUMN decode_ms TEXT NOT NULL DEFAULT '0';
