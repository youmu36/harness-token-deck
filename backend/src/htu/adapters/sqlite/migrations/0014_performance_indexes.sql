-- Avoid repeated wallet scans while compacting each expired request and its offsets.
CREATE INDEX wallet_related ON wallet_entry(related_entry_id) WHERE related_entry_id IS NOT NULL;
CREATE INDEX wallet_consumption_time ON wallet_entry(occurred_at) WHERE kind='consumption';
