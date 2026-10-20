CREATE TABLE wallet_rollup(
    vendor_id TEXT NOT NULL REFERENCES vendor, bucket TEXT NOT NULL,
    consumption TEXT NOT NULL, adjustment TEXT NOT NULL, count INTEGER NOT NULL,
    first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
    unpriced_count INTEGER NOT NULL, incomplete_count INTEGER NOT NULL,
    PRIMARY KEY(vendor_id,bucket)
) STRICT, WITHOUT ROWID;
DROP TRIGGER wallet_entry_delete;
CREATE TRIGGER wallet_entry_delete BEFORE DELETE ON wallet_entry
    WHEN htu_allow_compaction()=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;
