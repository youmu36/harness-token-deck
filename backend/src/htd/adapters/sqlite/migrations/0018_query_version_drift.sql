-- Query invalidation follows the changed source/day, including maintenance and late writes.
CREATE TABLE usage_day_revision(harness TEXT NOT NULL, day TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY(harness,day));
CREATE TABLE price_version_status(version_id TEXT PRIMARY KEY REFERENCES price_version(id), disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)), deleted_at TEXT);
CREATE TABLE remote_billing_drift(account_id TEXT PRIMARY KEY REFERENCES remote_account(id) ON DELETE CASCADE, amount_pico TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TRIGGER ledger_query_insert AFTER INSERT ON ledger BEGIN
INSERT INTO usage_day_revision VALUES(NEW.harness,substr(NEW.occurred_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER ledger_query_delete AFTER DELETE ON ledger BEGIN
INSERT INTO usage_day_revision VALUES(OLD.harness,substr(OLD.occurred_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER ledger_query_update AFTER UPDATE ON ledger BEGIN
INSERT INTO usage_day_revision VALUES(OLD.harness,substr(OLD.occurred_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
INSERT INTO usage_day_revision VALUES(NEW.harness,substr(NEW.occurred_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_rollup_query_insert AFTER INSERT ON usage_rollup BEGIN
INSERT INTO usage_day_revision VALUES(NEW.harness,substr(NEW.bucket,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_rollup_query_delete AFTER DELETE ON usage_rollup BEGIN
INSERT INTO usage_day_revision VALUES(OLD.harness,substr(OLD.bucket,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_rollup_query_update AFTER UPDATE ON usage_rollup BEGIN
INSERT INTO usage_day_revision VALUES(OLD.harness,substr(OLD.bucket,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
INSERT INTO usage_day_revision VALUES(NEW.harness,substr(NEW.bucket,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_archive_query_insert AFTER INSERT ON usage_archive BEGIN
INSERT INTO usage_day_revision VALUES(NEW.harness,substr(NEW.start_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_archive_query_delete AFTER DELETE ON usage_archive BEGIN
INSERT INTO usage_day_revision VALUES(OLD.harness,substr(OLD.start_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
CREATE TRIGGER usage_archive_query_update AFTER UPDATE ON usage_archive BEGIN
INSERT INTO usage_day_revision VALUES(OLD.harness,substr(OLD.start_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
INSERT INTO usage_day_revision VALUES(NEW.harness,substr(NEW.start_at,1,10),1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1;
END;
