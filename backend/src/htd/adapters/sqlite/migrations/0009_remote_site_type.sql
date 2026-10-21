ALTER TABLE remote_account ADD COLUMN site_type TEXT NOT NULL DEFAULT 'auto'
    CHECK(site_type IN ('auto','newapi','sub2api'));
