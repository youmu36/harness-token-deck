"""Additive features deliberately outside the checksummed 0.4.1 schema contract.

No released table, migration checksum or generation changes. Old binaries ignore
these tables; all new projections read current shared facts after a downgrade.
"""

import sqlite3


def install(db: sqlite3.Connection) -> None:
    statements = (
        "CREATE TABLE IF NOT EXISTS local_wallet_posting("
        "id INTEGER PRIMARY KEY,vendor_id TEXT NOT NULL REFERENCES vendor(id),"
        "occurred_at TEXT NOT NULL,r TEXT NOT NULL,c TEXT NOT NULL,a TEXT NOT NULL,"
        "note TEXT NOT NULL,original_id TEXT,operation_id TEXT NOT NULL UNIQUE) STRICT",
        "CREATE INDEX IF NOT EXISTS local_wallet_posting_vendor ON local_wallet_posting(vendor_id,id DESC)",
        "CREATE TABLE IF NOT EXISTS local_wallet_total("
        "vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,r TEXT NOT NULL,c TEXT NOT NULL,"
        "a TEXT NOT NULL) STRICT",
        "CREATE INDEX IF NOT EXISTS local_wallet_posting_original "
        "ON local_wallet_posting(vendor_id,original_id)",
        "CREATE TABLE IF NOT EXISTS remote_usage_hint("
        "account_id TEXT PRIMARY KEY REFERENCES remote_account(id) ON DELETE CASCADE,"
        "observed_at TEXT NOT NULL,today_pico TEXT,total_pico TEXT) STRICT",
        "CREATE TABLE IF NOT EXISTS local_wallet_metadata("
        "vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,"
        "past_r TEXT NOT NULL,past_c TEXT NOT NULL) STRICT",
        "INSERT OR IGNORE INTO local_wallet_metadata "
        "SELECT vendor_id,past_top_up,past_consumption FROM vendor_accounting",
        "CREATE TABLE IF NOT EXISTS output_daily_speed("
        "day TEXT NOT NULL,timezone TEXT NOT NULL,harness TEXT NOT NULL,model_key TEXT NOT NULL,"
        "display_name TEXT NOT NULL,tps TEXT NOT NULL,samples INTEGER NOT NULL,"
        "PRIMARY KEY(day,timezone,harness,model_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS output_daily_speed_model ON output_daily_speed(model_key,day,harness)",
        "CREATE TABLE IF NOT EXISTS output_rank_speed("
        "harness TEXT NOT NULL,model_key TEXT NOT NULL,total_tps TEXT NOT NULL,days INTEGER NOT NULL,"
        "PRIMARY KEY(harness,model_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS output_rank_speed_model ON output_rank_speed(model_key,harness)",
        "CREATE TABLE IF NOT EXISTS wallet_inheritance("
        "vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,payload TEXT NOT NULL) STRICT",
        "CREATE TABLE IF NOT EXISTS wallet_inherited_entry("
        "vendor_id TEXT NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,id TEXT NOT NULL,"
        "occurred_at TEXT NOT NULL,posted_at TEXT NOT NULL,kind TEXT NOT NULL,"
        "r TEXT NOT NULL,c TEXT NOT NULL,a TEXT NOT NULL,note TEXT NOT NULL,original_id TEXT,"
        "interval_id TEXT,available TEXT NOT NULL,PRIMARY KEY(vendor_id,id)) STRICT",
        "CREATE INDEX IF NOT EXISTS wallet_inherited_entry_page "
        "ON wallet_inherited_entry(vendor_id,posted_at DESC,id DESC)",
        "CREATE INDEX IF NOT EXISTS wallet_inherited_entry_month "
        "ON wallet_inherited_entry(vendor_id,occurred_at)",
        "CREATE TABLE IF NOT EXISTS wallet_inherited_daily("
        "vendor_id TEXT NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,day TEXT NOT NULL,"
        "timezone TEXT NOT NULL,c TEXT NOT NULL,PRIMARY KEY(vendor_id,day,timezone)) STRICT",
        "CREATE TABLE IF NOT EXISTS session_hit_rate("
        "harness TEXT NOT NULL,session_key TEXT NOT NULL,display_name TEXT NOT NULL,"
        "started_at TEXT NOT NULL,last_received_at TEXT NOT NULL,input_tokens TEXT NOT NULL,"
        "cache_tokens TEXT NOT NULL,missing_input INTEGER NOT NULL,missing_cache INTEGER NOT NULL,"
        "PRIMARY KEY(harness,session_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS session_hit_rate_activity "
        "ON session_hit_rate(harness,last_received_at DESC)",
    )
    for statement in statements:
        db.execute(statement)
    from htd.adapters.sqlite.session_speed import install as install_session_speed

    install_session_speed(db)
    from htd.adapters.sqlite.explorer_storage import install as install_explorer

    install_explorer(db)
    from htd.adapters.sqlite.output_scope import install as install_scoped_output

    install_scoped_output(db)
    from htd.adapters.sqlite.zcode_speed_repair import install as install_zcode_repair

    install_zcode_repair(db)
    from htd.adapters.sqlite.wallet_flows import install as install_flows
    from htd.adapters.sqlite.wallet_inheritance import inherit
    from htd.adapters.sqlite.wallet_pending import install as install_pending

    install_flows(db)
    from htd.adapters.sqlite.wallet_recalc_journal import install as install_recalc_journal

    install_recalc_journal(db)
    install_pending(db)
    inherit(db)
    from htd.adapters.sqlite.wallet_flows import seed_recent

    seed_recent(db)
    from htd.adapters.sqlite.wallet_recovery import install as install_wallet_recovery

    install_wallet_recovery(db)
