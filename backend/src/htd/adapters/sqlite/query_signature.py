"""Source/day and relevant target revisions keep unrelated financial reads out of usage keys."""

import sqlite3
from datetime import timedelta
from typing import Any

from htd.domain.common.time import Instant, parse_instant
from htd.domain.wallet.calibration import DIMENSIONS


def signature(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> tuple[tuple[Any, ...], ...]:
    result: list[tuple[Any, ...]] = []
    remote = query.get("costBasis") == "remote"
    start, end = parse_instant(query["start"]), parse_instant(query["end"])
    sources = query.get("harnesses", [])
    clause = " AND harness IN (" + ",".join("?" for _ in sources) + ")" if sources else ""
    days = [
        tuple(row)
        for row in db.execute(
            "SELECT harness,day,value FROM usage_day_revision WHERE day>=? AND day<=?"
            + clause
            + " ORDER BY harness,day",
            ((start.value - timedelta(days=1)).date().isoformat(), end.value.date().isoformat(), *sources),
        )
    ]
    if days and start.value >= at.value - timedelta(days=62):
        result.extend(("usage_day", *row) for row in days)
    else:
        result.append(tuple(db.execute("SELECT scope,value FROM revision WHERE scope='usage'").fetchone()))
    scopes = ["quality"] if "quality" in query.get("sections", ["quality"]) else []
    if remote:
        scopes += ["account", "wallet", "sources"]
    if scopes:
        result.extend(
            tuple(row)
            for row in db.execute(
                "SELECT scope,value FROM revision WHERE scope IN ("
                + ",".join("?" for _ in scopes)
                + ") ORDER BY scope",
                scopes,
            )
        )
    if not remote and set(query.get("sections", ["totals"])) != {"quality"}:
        filters = ""
        arguments: list[str] = [query["end"], query["start"]]
        vendors = query.get("vendorIds", [])
        if vendors:
            filters += " AND t.vendor_id IN (" + ",".join("?" for _ in vendors) + ")"
            arguments.extend(vendors)
        for name in DIMENSIONS:
            if query.get(name):
                filters += (
                    " AND EXISTS(SELECT 1 FROM json_each(b.scope_json,?) j WHERE j.value IN ("
                    + ",".join("?" for _ in query[name])
                    + "))"
                )
                arguments.extend(("$." + name, *query[name]))
        result.extend(
            ("cost_bucket", *tuple(row))
            for row in db.execute(
                "SELECT b.id,b.revision FROM calibration_bucket b "
                "JOIN calibration_target t ON t.id=b.target_id "
                "WHERE t.state='active' AND b.coverage_start<? AND b.coverage_end>?"
                + filters
                + " ORDER BY b.id",
                arguments,
            )
        )
    return tuple(result)
