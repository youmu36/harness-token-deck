CREATE TABLE unpriced_job(
 id TEXT PRIMARY KEY,pricing_revision INTEGER NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,
 recalculated INTEGER NOT NULL DEFAULT 0,cost_delta TEXT NOT NULL DEFAULT '0',complete INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE TABLE unpriced_job_member(
 job_id TEXT NOT NULL REFERENCES unpriced_job(id),harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(job_id,harness,dedupe_key)
) STRICT,WITHOUT ROWID;
CREATE INDEX unpriced_job_pending ON unpriced_job_member(job_id,status,harness,dedupe_key);
