CREATE TABLE vendor_site(vendor_id TEXT PRIMARY KEY REFERENCES vendor ON DELETE CASCADE,
    url TEXT NOT NULL CHECK(length(url) BETWEEN 1 AND 2048)) STRICT;
CREATE INDEX vendor_site_url ON vendor_site(url);
DELETE FROM preference WHERE key='autoMatchProvider';

CREATE TABLE remote_account_extended(id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL UNIQUE REFERENCES vendor,
    adapter TEXT NOT NULL CHECK(adapter='newapi'), base_url TEXT NOT NULL,
    auth_mode TEXT NOT NULL CHECK(auth_mode IN ('api_key','access_token','billing')),
    user_id TEXT, credential_ref TEXT NOT NULL, quota_per_unit TEXT,
    refresh_minutes INTEGER NOT NULL CHECK(refresh_minutes BETWEEN 5 AND 60),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    site_type TEXT NOT NULL DEFAULT 'auto' CHECK(site_type IN ('auto','newapi','sub2api','deepseek'))) STRICT;
INSERT INTO remote_account_extended SELECT * FROM remote_account;
DROP TABLE remote_account;
ALTER TABLE remote_account_extended RENAME TO remote_account;

ALTER TABLE usage_rollup ADD COLUMN cache_bust INTEGER NOT NULL DEFAULT 0 CHECK(cache_bust IN (0,1));
ALTER TABLE usage_rollup ADD COLUMN large_miss INTEGER NOT NULL DEFAULT 0 CHECK(large_miss IN (0,1));
CREATE TABLE reconciliation_notice(account_id TEXT PRIMARY KEY REFERENCES remote_account ON DELETE CASCADE,
    vendor_id TEXT NOT NULL REFERENCES vendor ON DELETE CASCADE,
    from_at TEXT NOT NULL, observed_at TEXT NOT NULL, amount_pico TEXT NOT NULL) STRICT;
