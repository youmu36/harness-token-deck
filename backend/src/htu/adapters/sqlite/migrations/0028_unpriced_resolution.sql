-- Historical spending is editable metadata, independent of vouchers and balances.
ALTER TABLE vendor_accounting ADD COLUMN past_consumption TEXT NOT NULL DEFAULT '0'
    CHECK(past_consumption<>'' AND past_consumption NOT GLOB '*[^0-9]*');

-- Decisions apply only to the selected retained requests, never to future arrivals.
CREATE TABLE unpriced_resolution(
    harness TEXT NOT NULL,
    dedupe_key BLOB NOT NULL,
    ignored INTEGER NOT NULL DEFAULT 0 CHECK(ignored IN (0,1)),
    group_id TEXT REFERENCES price_group(id),
    version_id TEXT REFERENCES price_version(id),
    model_json TEXT,
    multiplier TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(harness,dedupe_key),
    FOREIGN KEY(harness,dedupe_key) REFERENCES ledger(harness,dedupe_key) ON DELETE CASCADE
) STRICT;
