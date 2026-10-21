-- Select a model before sorting or streaming its detail/minute projections.
CREATE INDEX ledger_model_time ON ledger(model_key,occurred_at,harness);
CREATE INDEX usage_rollup_model_time ON usage_rollup(model_key,bucket,harness);
