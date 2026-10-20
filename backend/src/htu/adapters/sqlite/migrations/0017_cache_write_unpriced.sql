ALTER TABLE model_base_rate ADD COLUMN cache_write TEXT;
ALTER TABLE price_revision_model ADD COLUMN cache_write TEXT;
CREATE INDEX ledger_unpriced ON ledger(occurred_at,harness,price_group_id,price_version_id,model_key)
    WHERE unpriced=1;
