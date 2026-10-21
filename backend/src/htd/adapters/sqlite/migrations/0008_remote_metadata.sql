ALTER TABLE remote_observation ADD COLUMN currency TEXT;
ALTER TABLE remote_observation ADD COLUMN quota_per_unit TEXT;
ALTER TABLE remote_observation ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE remote_observation ADD COLUMN currency_uncertain INTEGER NOT NULL DEFAULT 1 CHECK(currency_uncertain IN (0,1));
ALTER TABLE remote_observation ADD COLUMN display_in_currency INTEGER CHECK(display_in_currency IS NULL OR display_in_currency IN (0,1));
ALTER TABLE remote_observation ADD COLUMN status_code INTEGER;
ALTER TABLE remote_observation ADD COLUMN retry_after_seconds INTEGER;
