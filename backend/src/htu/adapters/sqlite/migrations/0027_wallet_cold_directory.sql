CREATE TABLE wallet_archive(
    id INTEGER PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendor(id), utc_day TEXT NOT NULL CHECK(length(utc_day)=10),
    start_at TEXT NOT NULL CHECK(length(start_at)=24), end_at TEXT NOT NULL CHECK(length(end_at)=24 AND end_at>=start_at),
    first_seq INTEGER NOT NULL CHECK(first_seq>0), last_seq INTEGER NOT NULL CHECK(last_seq>=first_seq),
    activity_count INTEGER NOT NULL CHECK(activity_count>=0), rollup_count INTEGER NOT NULL CHECK(rollup_count>=0),
    correction_count INTEGER NOT NULL CHECK(correction_count BETWEEN 0 AND activity_count),
    consumption TEXT NOT NULL, adjustment TEXT NOT NULL, unpriced_count INTEGER NOT NULL,
    incomplete_count INTEGER NOT NULL CHECK(incomplete_count>=0), payload BLOB NOT NULL, checksum TEXT NOT NULL CHECK(length(checksum)=64),
    encoding INTEGER NOT NULL CHECK(encoding=1), CHECK(activity_count+rollup_count BETWEEN 1 AND 500)
) STRICT;
CREATE INDEX wallet_archive_time ON wallet_archive(vendor_id,start_at,end_at);
CREATE INDEX wallet_archive_partition ON wallet_archive(vendor_id,utc_day);
CREATE INDEX wallet_archive_page ON wallet_archive(vendor_id,last_seq DESC,first_seq) WHERE correction_count>0;
CREATE TRIGGER wallet_archive_update BEFORE UPDATE ON wallet_archive WHEN htu_allow_compaction()=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER wallet_archive_delete BEFORE DELETE ON wallet_archive WHEN htu_allow_compaction()=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TABLE wallet_day_offset(
    offset_minutes INTEGER PRIMARY KEY CHECK(offset_minutes BETWEEN -840 AND 840),
    target INTEGER NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0, cold_id INTEGER NOT NULL DEFAULT 0,
    hot_done INTEGER NOT NULL DEFAULT 0 CHECK(hot_done IN (0,1)), ready INTEGER NOT NULL DEFAULT 0 CHECK(ready IN (0,1))
) STRICT;
CREATE TABLE wallet_day(
    vendor_id TEXT NOT NULL REFERENCES vendor(id), offset_minutes INTEGER NOT NULL REFERENCES wallet_day_offset,
    day TEXT NOT NULL, first_seq INTEGER NOT NULL, amount_pico TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY(vendor_id,offset_minutes,day)
) STRICT, WITHOUT ROWID;
CREATE INDEX wallet_day_page ON wallet_day(vendor_id,offset_minutes,first_seq DESC);
CREATE TRIGGER wallet_day_insert AFTER INSERT ON wallet_activity WHEN NEW.kind='consumption' BEGIN
    UPDATE wallet_sequence SET value=MAX(value,NEW.seq) WHERE id=1;
    INSERT INTO wallet_day SELECT NEW.vendor_id,o.offset_minutes,
        date(NEW.occurred_at,printf('%+d minutes',o.offset_minutes)),NEW.seq,NEW.amount_pico,NEW.count
      FROM wallet_day_offset o WHERE o.ready=1 OR NEW.seq<=o.last_seq OR NEW.seq>o.target
      ON CONFLICT(vendor_id,offset_minutes,day) DO UPDATE SET
        first_seq=MIN(first_seq,excluded.first_seq),amount_pico=htu_pico_add(amount_pico,excluded.amount_pico),count=count+excluded.count;
END;
CREATE TRIGGER wallet_day_update AFTER UPDATE ON wallet_activity WHEN NEW.kind='consumption' BEGIN
    INSERT INTO wallet_day SELECT NEW.vendor_id,o.offset_minutes,
        date(NEW.occurred_at,printf('%+d minutes',o.offset_minutes)),NEW.seq,
        htu_pico_sub(NEW.amount_pico,OLD.amount_pico),NEW.count-OLD.count
      FROM wallet_day_offset o WHERE o.ready=1 OR NEW.seq<=o.last_seq OR NEW.seq>o.target
      ON CONFLICT(vendor_id,offset_minutes,day) DO UPDATE SET
        first_seq=MIN(first_seq,excluded.first_seq),amount_pico=htu_pico_add(amount_pico,excluded.amount_pico),count=count+excluded.count;
END;
