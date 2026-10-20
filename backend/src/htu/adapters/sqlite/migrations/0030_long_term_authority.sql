-- Published natural-day counters replace only the source rows retired in the same transaction.
CREATE TABLE usage_daily(
    harness TEXT NOT NULL,
    business_day TEXT NOT NULL,
    vendor_id TEXT NOT NULL,
    model_key TEXT NOT NULL,
    timezone TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    payload BLOB NOT NULL,
    active_minutes TEXT NOT NULL,
    missing_capabilities TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    model_id TEXT,
    records TEXT NOT NULL,
    PRIMARY KEY(harness,business_day,vendor_id,model_key)
) STRICT, WITHOUT ROWID;
CREATE INDEX usage_daily_time ON usage_daily(start_at,end_at,harness,model_key);
CREATE TRIGGER usage_daily_insert AFTER INSERT ON usage_daily BEGIN
 INSERT INTO usage_day_revision VALUES(new.harness,substr(new.start_at,1,10),1)
 ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
 INSERT INTO usage_day_revision VALUES(new.harness,substr(new.end_at,1,10),1)
 ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_daily_update AFTER UPDATE ON usage_daily BEGIN
 INSERT INTO usage_day_revision VALUES(new.harness,substr(new.start_at,1,10),1)
 ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
 INSERT INTO usage_day_revision VALUES(new.harness,substr(new.end_at,1,10),1)
 ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
INSERT OR IGNORE INTO preference VALUES('longTermRetention','"forever"');
-- The old setting now limits fine detail; authority is preserved before retirement.
INSERT OR IGNORE INTO preference VALUES('retention','"forever"');

UPDATE meta SET value='3' WHERE key='storage_generation';

CREATE INDEX wallet_quality_latest ON wallet_entry(usage_harness,usage_key,seq DESC);
CREATE INDEX vendor_link_attribution ON vendor_link(group_id,effective_at DESC,seq DESC);
CREATE TRIGGER account_endpoint_update BEFORE UPDATE ON account_endpoint BEGIN SELECT RAISE(ABORT,'immutable_account_evidence'); END;
CREATE TRIGGER account_endpoint_delete BEFORE DELETE ON account_endpoint BEGIN SELECT RAISE(ABORT,'immutable_account_evidence'); END;
