-- One durable staging waterline per account, independent of the rotating poll cache.
INSERT INTO revision VALUES('output',0);
CREATE TABLE token_activity(
    harness TEXT NOT NULL,
    model_key TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    PRIMARY KEY(harness,model_key)
) STRICT;
CREATE INDEX token_activity_harness_time ON token_activity(harness,last_received_at DESC);
CREATE TABLE account_pending(
    segment_id TEXT PRIMARY KEY REFERENCES account_segment(id),
    start_endpoint TEXT NOT NULL REFERENCES account_endpoint(id),
    end_endpoint TEXT NOT NULL REFERENCES account_endpoint(id),
    changed_at TEXT NOT NULL,
    local_changed_at TEXT NOT NULL,
    reviewed_at TEXT
) STRICT;
INSERT INTO account_pending
 SELECT s.id,e.id,e.id,e.observed_at,e.observed_at,NULL FROM account_segment s
 JOIN account_endpoint e ON e.id=(SELECT id FROM account_endpoint
 WHERE segment_id=s.id ORDER BY observed_at DESC LIMIT 1)
 WHERE s.kind='remote' AND s.ended_at IS NULL;

CREATE TABLE account_pending_batch(
    id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES account_segment(id),
    account_id TEXT NOT NULL,
    start_endpoint TEXT NOT NULL REFERENCES account_endpoint(id),
    end_endpoint TEXT NOT NULL REFERENCES account_endpoint(id),
    ready_at TEXT NOT NULL,
    estimate TEXT NOT NULL,
    e_watermark INTEGER NOT NULL
) STRICT;
CREATE INDEX account_pending_batch_due ON account_pending_batch(ready_at);
CREATE TABLE account_pending_member(
    harness TEXT NOT NULL,
    dedupe_key BLOB NOT NULL,
    batch_id TEXT NOT NULL REFERENCES account_pending_batch(id) ON DELETE CASCADE,
    PRIMARY KEY(harness,dedupe_key,batch_id),
    FOREIGN KEY(harness,dedupe_key) REFERENCES inbox(harness,dedupe_key) ON DELETE CASCADE
) STRICT;
CREATE INDEX account_pending_member_batch ON account_pending_member(batch_id);

CREATE TABLE output_live(
    harness TEXT NOT NULL,
    model_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    last_output_at TEXT,
    buckets_json TEXT NOT NULL,
    output_tokens TEXT NOT NULL,
    active_minutes INTEGER NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(harness,model_key)
) STRICT;
CREATE INDEX output_live_pending ON output_live(last_received_at) WHERE finalized=0;
CREATE TABLE output_pending(
    id TEXT PRIMARY KEY,
    harness TEXT NOT NULL,
    model_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    last_output_at TEXT,
    output_tokens TEXT NOT NULL,
    active_minutes INTEGER NOT NULL
) STRICT;
CREATE INDEX output_pending_due ON output_pending(last_received_at);
CREATE TABLE output_reference(
    day TEXT NOT NULL,
    timezone TEXT NOT NULL,
    harness TEXT NOT NULL,
    model_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    output_tokens TEXT NOT NULL,
    active_minutes INTEGER NOT NULL,
    samples INTEGER NOT NULL,
    last_output_at TEXT NOT NULL,
    PRIMARY KEY(day,timezone,harness,model_key)
) STRICT;
CREATE INDEX output_reference_model_day ON output_reference(model_key,day,harness);
