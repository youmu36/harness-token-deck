CREATE TABLE price_group(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    disabled INTEGER NOT NULL CHECK(disabled IN (0,1)),
    created_at TEXT NOT NULL CHECK(length(created_at)=24 AND substr(created_at,24,1)='Z'),
    seq INTEGER NOT NULL) STRICT;
CREATE TABLE price_version(id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES price_group,
    number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 4294967295),
    created_at TEXT NOT NULL CHECK(length(created_at)=24 AND substr(created_at,24,1)='Z'),
    seq INTEGER NOT NULL, UNIQUE(group_id,number)) STRICT;
CREATE TABLE price_version_label(version_id TEXT PRIMARY KEY REFERENCES price_version, label TEXT NOT NULL) STRICT;
CREATE TABLE price_revision(id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES price_version,
    effective_at TEXT NOT NULL CHECK(length(effective_at)=24 AND substr(effective_at,24,1)='Z'),
    seq INTEGER NOT NULL UNIQUE, fingerprint TEXT NOT NULL) STRICT;
CREATE TABLE price_revision_model(revision_id TEXT NOT NULL REFERENCES price_revision,
    model_key TEXT NOT NULL, model_id TEXT NOT NULL, display_name TEXT NOT NULL,
    input TEXT, cache TEXT, output TEXT, rule_json TEXT, PRIMARY KEY(revision_id,model_key)) STRICT;
CREATE TABLE version_activation(group_id TEXT NOT NULL REFERENCES price_group,
    version_id TEXT NOT NULL REFERENCES price_version,
    effective_at TEXT NOT NULL CHECK(length(effective_at)=24 AND substr(effective_at,24,1)='Z'),
    seq INTEGER PRIMARY KEY) STRICT;
CREATE TABLE harness_selection(harness TEXT NOT NULL CHECK(harness IN ('codex','zcode','dsh')),
    group_id TEXT NOT NULL REFERENCES price_group,
    effective_at TEXT NOT NULL CHECK(length(effective_at)=24 AND substr(effective_at,24,1)='Z'),
    seq INTEGER PRIMARY KEY) STRICT;
CREATE TABLE history_seq(id INTEGER PRIMARY KEY AUTOINCREMENT) STRICT;
CREATE TABLE model_pool(model_key TEXT PRIMARY KEY, model_id TEXT NOT NULL,
    added_at TEXT NOT NULL CHECK(length(added_at)=24 AND substr(added_at,24,1)='Z')) STRICT;
CREATE TABLE model_excluded(model_key TEXT PRIMARY KEY, model_id TEXT NOT NULL,
    at TEXT NOT NULL CHECK(length(at)=24 AND substr(at,24,1)='Z')) STRICT;
CREATE TABLE model_base_rate(model_key TEXT PRIMARY KEY REFERENCES model_pool ON DELETE CASCADE,
    input TEXT, cache TEXT, output TEXT) STRICT;
CREATE TABLE editor_hint(version_id TEXT NOT NULL REFERENCES price_version, model_key TEXT NOT NULL,
    model_id TEXT NOT NULL, multiplier TEXT NOT NULL, PRIMARY KEY(version_id,model_key)) STRICT;
CREATE TRIGGER price_group_delete BEFORE DELETE ON price_group
    WHEN htd_allow_group_delete(OLD.id)=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER price_version_update BEFORE UPDATE ON price_version BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER price_version_delete BEFORE DELETE ON price_version
    WHEN htd_allow_group_delete(OLD.group_id)=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER price_revision_update BEFORE UPDATE ON price_revision BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER price_revision_delete BEFORE DELETE ON price_revision
    WHEN htd_allow_group_delete((SELECT group_id FROM price_version WHERE id=OLD.version_id))=0
    BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER price_model_update BEFORE UPDATE ON price_revision_model BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER price_model_delete BEFORE DELETE ON price_revision_model
    WHEN htd_allow_group_delete((SELECT v.group_id FROM price_version v JOIN price_revision r
        ON r.version_id=v.id WHERE r.id=OLD.revision_id))=0
    BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER activation_update BEFORE UPDATE ON version_activation BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER activation_delete BEFORE DELETE ON version_activation
    WHEN htd_allow_group_delete(OLD.group_id)=0 BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER selection_update BEFORE UPDATE ON harness_selection BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER selection_delete BEFORE DELETE ON harness_selection BEGIN SELECT RAISE(ABORT,'append_only'); END;
