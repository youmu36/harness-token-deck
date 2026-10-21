CREATE TABLE account_interval_revision(
 interval_id TEXT NOT NULL REFERENCES account_interval(id),
 revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL,
 posted_at TEXT NOT NULL,
 PRIMARY KEY(interval_id,revision)
) STRICT;
INSERT INTO account_interval_revision
 SELECT i.id,i.revision,json_object('R',i.r,'C',i.c,'A',i.a,'E',i.e,'method',i.method,'watermark',i.e_watermark),e.observed_at
 FROM account_interval i JOIN account_endpoint e ON e.id=i.end_endpoint;
