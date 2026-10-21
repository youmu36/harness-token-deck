ALTER TABLE price_group ADD COLUMN deleted_at TEXT;
CREATE TABLE usage_rollup(
    harness TEXT NOT NULL, bucket TEXT NOT NULL, vendor_id TEXT NOT NULL,
    model_key TEXT NOT NULL, model_id TEXT, display_name TEXT,
    records INTEGER NOT NULL, input INTEGER NOT NULL, cache_read INTEGER NOT NULL,
    output INTEGER NOT NULL, rated_input INTEGER NOT NULL, rated_cache INTEGER NOT NULL,
    unknown_cache INTEGER NOT NULL, missing_usage INTEGER NOT NULL, unknown_model INTEGER NOT NULL,
    incomplete INTEGER NOT NULL, unpriced INTEGER NOT NULL, miss_input INTEGER NOT NULL,
    cache_loss INTEGER NOT NULL, pico TEXT NOT NULL,
    cache_write INTEGER NOT NULL, reasoning INTEGER NOT NULL,
    PRIMARY KEY(harness,bucket,vendor_id,model_key)
) STRICT, WITHOUT ROWID;
CREATE INDEX usage_rollup_time ON usage_rollup(bucket,harness);
CREATE TABLE cache_anchor(
    harness TEXT NOT NULL, session_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
    cache_read INTEGER NOT NULL, PRIMARY KEY(harness,session_id)
) STRICT, WITHOUT ROWID;
