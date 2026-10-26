"""Load a coherent pricing catalog while sharing identical immutable model snapshots."""

import sqlite3

from htd.adapters.sqlite.pricing_codec import decode_rule, decode_switch
from htd.domain.common.time import parse_instant
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.model import (
    EditorHints,
    HarnessSelection,
    ModelRate,
    PriceGroup,
    PriceRevision,
    PriceVersion,
    VersionActivation,
)
from htd.domain.usage.model import Harness


def load_catalog(connection: sqlite3.Connection, *, metadata_only: bool = False) -> Catalog:
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute("BEGIN")
    try:
        groups = tuple(
            PriceGroup(
                row["id"],
                row["name"],
                bool(row["disabled"]),
                parse_instant(row["created_at"]),
                parse_instant(row["deleted_at"])
                if "deleted_at" in row.keys() and row["deleted_at"]  # noqa: SIM118 (sqlite3.Row)
                else None,
            )
            for row in connection.execute("SELECT * FROM price_group ORDER BY seq")
        )
        versions = tuple(
            PriceVersion(
                row["id"],
                row["group_id"],
                row["number"],
                row["label"],
                parse_instant(row["created_at"]),
                bool(row["disabled"]),
                parse_instant(row["deleted_at"]) if row["deleted_at"] else None,
            )
            for row in connection.execute(
                "SELECT v.*,COALESCE(l.label,'') label,COALESCE(s.disabled,0) "
                "disabled,s.deleted_at FROM price_version v "
                "LEFT JOIN price_version_label l ON l.version_id=v.id LEFT JOIN "
                "price_version_status s ON s.version_id=v.id ORDER BY v.seq"
            )
        )
        model_cache: dict[tuple[str | None, ...], ModelRate] = {}
        models: dict[str, list[ModelRate]] = {}
        cursor = connection.cursor()
        cursor.row_factory = None
        if not metadata_only:
            cursor.execute(
                "SELECT revision_id,model_id,display_name,input,cache,output,rule_json,cache_write "
                "FROM price_revision_model ORDER BY revision_id,model_key"
            )
        for row in cursor:
            key = row[1:]
            if key not in model_cache:
                model_cache[key] = ModelRate(
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    decode_rule(row[6]),
                    row[7],
                )
            models.setdefault(row[0], []).append(model_cache[key])
        cursor.close()
        revisions = tuple(
            PriceRevision(
                row["id"],
                row["version_id"],
                parse_instant(row["effective_at"]),
                row["seq"],
                tuple(models.get(row["id"], ())),
            )
            for row in connection.execute("SELECT * FROM price_revision ORDER BY seq")
        )
        activations = tuple(
            VersionActivation(
                row["group_id"], row["version_id"], parse_instant(row["effective_at"]), row["seq"]
            )
            for row in connection.execute("SELECT * FROM version_activation ORDER BY seq")
        )
        selections = tuple(
            HarnessSelection(
                Harness(row["harness"]), row["group_id"], parse_instant(row["effective_at"]), row["seq"]
            )
            for row in connection.execute("SELECT * FROM harness_selection ORDER BY seq")
        )
        hints = tuple(
            EditorHints(row["version_id"], row["model_id"], row["multiplier"])
            for row in (
                () if metadata_only else connection.execute("SELECT * FROM editor_hint ORDER BY rowid")
            )
        )
        switches = tuple(
            decode_switch(row[0])
            for row in (
                ()
                if metadata_only
                else connection.execute(
                    "SELECT result_json FROM operation WHERE command='pricing.switch' ORDER BY rowid"
                )
            )
        )
        revision = int(connection.execute("SELECT value FROM revision WHERE scope='pricing'").fetchone()[0])
        sequence = int(
            connection.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='history_seq'),0)"
            ).fetchone()[0]
        )
        return Catalog(
            groups, versions, revisions, activations, selections, hints, switches, revision, sequence
        )
    finally:
        if own_transaction:
            connection.rollback()
