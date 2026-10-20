-- Rebuildable projections. Dirty hours always fall back to authoritative minutes.
CREATE TABLE usage_hour(harness TEXT NOT NULL, bucket TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(harness,bucket));
CREATE TABLE usage_hour_dirty(harness TEXT NOT NULL, bucket TEXT NOT NULL, PRIMARY KEY(harness,bucket));
INSERT INTO usage_hour_dirty SELECT DISTINCT harness,substr(bucket,1,13)||':00:00.000Z' FROM usage_rollup;
ALTER TABLE usage_archive ADD COLUMN hours_json TEXT;
CREATE TRIGGER usage_hour_insert AFTER INSERT ON usage_rollup BEGIN
INSERT OR IGNORE INTO usage_hour_dirty VALUES(NEW.harness,substr(NEW.bucket,1,13)||':00:00.000Z');
END;
CREATE TRIGGER usage_hour_delete AFTER DELETE ON usage_rollup BEGIN
INSERT OR IGNORE INTO usage_hour_dirty VALUES(OLD.harness,substr(OLD.bucket,1,13)||':00:00.000Z');
END;
CREATE TRIGGER usage_hour_update AFTER UPDATE ON usage_rollup BEGIN
INSERT OR IGNORE INTO usage_hour_dirty VALUES(OLD.harness,substr(OLD.bucket,1,13)||':00:00.000Z');
INSERT OR IGNORE INTO usage_hour_dirty VALUES(NEW.harness,substr(NEW.bucket,1,13)||':00:00.000Z');
END;
