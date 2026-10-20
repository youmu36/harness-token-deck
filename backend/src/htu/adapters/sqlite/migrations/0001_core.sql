CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE schema_migration(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL
    CHECK(length(applied_at)=24 AND substr(applied_at,24,1)='Z'), checksum TEXT NOT NULL) STRICT;
CREATE TABLE revision(scope TEXT PRIMARY KEY, value INTEGER NOT NULL CHECK(value>=0)) STRICT;
INSERT INTO revision VALUES ('preferences',0),('pricing',0),('wallet',0),('sources',0),('usage',0);
CREATE TABLE operation(id TEXT PRIMARY KEY, command TEXT NOT NULL, result_json TEXT NOT NULL,
    at TEXT NOT NULL CHECK(length(at)=24 AND substr(at,24,1)='Z')) STRICT;
CREATE TABLE preference(key TEXT PRIMARY KEY, value_json TEXT NOT NULL) STRICT;
CREATE TABLE runtime_state(key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE diagnostic_log(id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL CHECK(length(occurred_at)=24 AND substr(occurred_at,24,1)='Z'),
    harness TEXT CHECK(harness IN ('codex','zcode','dsh')), code TEXT NOT NULL,
    detail TEXT NOT NULL CHECK(length(detail)<=500)) STRICT;
CREATE INDEX diagnostic_time ON diagnostic_log(occurred_at,id);
