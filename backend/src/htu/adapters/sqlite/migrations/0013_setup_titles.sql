-- Existing used ledgers have already completed onboarding; new empty ledgers show the guide.
INSERT OR IGNORE INTO preference(key,value_json)
SELECT 'guideSeen','true' WHERE EXISTS(SELECT 1 FROM source)
    OR EXISTS(SELECT 1 FROM preference) OR EXISTS(SELECT 1 FROM ledger);

-- No usage/token rows are synthesized for a successful unmetered title request.
CREATE TABLE title_request(
    dedupe_key TEXT PRIMARY KEY CHECK(length(dedupe_key)=64),
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    price_group_id TEXT, vendor_id TEXT, wallet_entry_id TEXT,
    CHECK((vendor_id IS NULL)=(wallet_entry_id IS NULL))
) STRICT;
CREATE INDEX title_pending ON title_request(occurred_at) WHERE wallet_entry_id IS NULL;
