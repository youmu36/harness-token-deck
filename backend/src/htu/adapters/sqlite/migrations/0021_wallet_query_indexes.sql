-- Corrections and page vouchers must not scan the minute-consumption projection.
CREATE INDEX wallet_activity_corrections ON wallet_activity(vendor_id,occurred_at,seq)
WHERE kind='adjustment';
CREATE INDEX wallet_activity_other_page ON wallet_activity(vendor_id,seq DESC)
WHERE kind<>'consumption';
