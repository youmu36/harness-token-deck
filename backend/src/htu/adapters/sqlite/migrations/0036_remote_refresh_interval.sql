-- Rebuild with FK enforcement disabled by the migration runner, then verify every
-- reference before commit. Observations, notices, credentials and financial rows stay intact.
CREATE TABLE remote_account_next(
    id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL UNIQUE REFERENCES vendor,
    adapter TEXT NOT NULL CHECK(adapter='newapi'),
    base_url TEXT NOT NULL,
    auth_mode TEXT NOT NULL CHECK(auth_mode IN ('api_key','access_token','billing')),
    user_id TEXT,
    credential_ref TEXT NOT NULL,
    quota_per_unit TEXT,
    refresh_minutes INTEGER NOT NULL CHECK(refresh_minutes BETWEEN 1 AND 60),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    site_type TEXT NOT NULL DEFAULT 'auto' CHECK(site_type IN ('auto','newapi','sub2api','deepseek'))
) STRICT;
INSERT INTO remote_account_next SELECT * FROM remote_account;
DROP TABLE remote_account;
ALTER TABLE remote_account_next RENAME TO remote_account;
