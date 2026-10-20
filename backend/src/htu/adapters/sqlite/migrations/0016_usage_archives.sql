CREATE TABLE usage_archive(
    id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,
    harness TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    records INTEGER NOT NULL,
    models_json TEXT NOT NULL,
    payload BLOB NOT NULL
) STRICT;
CREATE INDEX usage_archive_time ON usage_archive(start_at,end_at,harness);
CREATE INDEX usage_archive_month ON usage_archive(month,harness);
