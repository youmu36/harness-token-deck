-- Historical funding is editable metadata, never a voucher or wallet balance.
CREATE TABLE vendor_accounting(
    vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,
    past_top_up TEXT NOT NULL DEFAULT '0' CHECK(past_top_up<>'' AND past_top_up NOT GLOB '*[^0-9]*'),
    balance_initialized INTEGER NOT NULL DEFAULT 0 CHECK(balance_initialized IN (0,1))
) STRICT;
-- Preserve established remote sessions and balances on upgrade.
INSERT INTO vendor_accounting(vendor_id,balance_initialized) SELECT id,1 FROM vendor;
