-- A retained presentation journal separates original spending from corrections.
CREATE TABLE wallet_activity(
 seq INTEGER PRIMARY KEY,
 vendor_id TEXT NOT NULL REFERENCES vendor(id),
 occurred_at TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('consumption','adjustment','top_up')),
 amount_pico TEXT NOT NULL,
 count INTEGER NOT NULL,
 note TEXT NOT NULL,
 source_id TEXT,
 related_seq INTEGER
);
CREATE UNIQUE INDEX wallet_activity_minute ON wallet_activity(vendor_id,occurred_at) WHERE kind='consumption';
CREATE INDEX wallet_activity_time ON wallet_activity(vendor_id,occurred_at,seq);
CREATE INDEX wallet_activity_page ON wallet_activity(vendor_id,seq);
