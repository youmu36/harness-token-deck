"""Persist validated catalog commands as immutable history deltas within a writer transaction."""

import sqlite3

from htd.adapters.sqlite.pricing_codec import encode_switch, fingerprint, rule_json
from htd.adapters.sqlite.pricing_load import load_catalog
from htd.adapters.sqlite.writer import UnitOfWork
from htd.domain.common.errors import (
    OPERATION_ID_REUSED,
    PRICING_INVALID,
    PRICING_REVISION_CONFLICT,
    DomainError,
)
from htd.domain.common.time import format_instant
from htd.domain.pricing import rules
from htd.domain.pricing.catalog import Catalog
from htd.domain.usage.model_key import model_key


class PricingRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load(self) -> Catalog:
        return load_catalog(self.connection)

    def save(self, work: UnitOfWork, old: Catalog, new: Catalog) -> None:
        if work.connection is not self.connection or not self.connection.in_transaction:
            raise RuntimeError("Catalog writes require the owning transaction")
        if work.revision("pricing") != old.revision:
            raise DomainError(PRICING_REVISION_CONFLICT, "价格目录已更新，请刷新后重试")
        if new is old:
            return
        if new.revision not in (old.revision, old.revision + 1):
            raise DomainError(PRICING_INVALID, "每个事务只能保存一次价格命令")
        current = self.load()
        if current != old:
            raise DomainError(PRICING_REVISION_CONFLICT, "价格目录快照已变化")
        prior = {plan.operation_id for plan in old.switches}
        for plan in new.switches:
            if (
                plan.operation_id not in prior
                and self.connection.execute(
                    "SELECT 1 FROM operation WHERE id=?", (plan.operation_id,)
                ).fetchone()
            ):
                raise DomainError(OPERATION_ID_REUSED, "该操作标识已用于另一项命令", "operation_id")
        old_groups = {group.id: group for group in old.groups}
        new_groups = {group.id: group for group in new.groups}
        removed = old_groups.keys() - new_groups.keys()
        if removed:
            raise DomainError(PRICING_INVALID, "价格组历史不可物理删除")
        self._check_history(old, new, removed)
        group_seq = int(self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM price_group").fetchone()[0])
        for group in new.groups:
            if group.id not in old_groups:
                group_seq += 1
                self.connection.execute(
                    "INSERT INTO price_group(id,name,disabled,created_at,seq) VALUES (?,?,?,?,?)",
                    (group.id, group.name, int(group.disabled), format_instant(group.created_at), group_seq),
                )
            elif group != old_groups[group.id]:
                if group.created_at != old_groups[group.id].created_at:
                    raise DomainError(PRICING_INVALID, "价格组创建时间不可修改")
                self.connection.execute(
                    "UPDATE price_group SET name=?,disabled=?,deleted_at=? WHERE id=?",
                    (
                        group.name,
                        int(group.disabled),
                        format_instant(group.deleted_at) if group.deleted_at else None,
                        group.id,
                    ),
                )
        versions = {version.id: version for version in old.versions}
        version_seq = int(
            self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM price_version").fetchone()[0]
        )
        for version in new.versions:
            if version.id not in versions:
                version_seq += 1
                self.connection.execute(
                    "INSERT INTO price_version VALUES (?,?,?,?,?)",
                    (
                        version.id,
                        version.group_id,
                        version.number,
                        format_instant(version.created_at),
                        version_seq,
                    ),
                )
            if version.id not in versions or version.label != versions[version.id].label:
                self.connection.execute(
                    "INSERT INTO price_version_label VALUES (?,?) "
                    "ON CONFLICT(version_id) DO UPDATE SET label=excluded.label",
                    (version.id, version.label),
                )
            if version.id not in versions or (version.disabled, version.deleted_at) != (
                versions[version.id].disabled,
                versions[version.id].deleted_at,
            ):
                self.connection.execute(
                    "INSERT INTO price_version_status VALUES(?,?,?) ON CONFLICT(version_id)"
                    " DO UPDATE SET "
                    "disabled=excluded.disabled,deleted_at=excluded.deleted_at",
                    (
                        version.id,
                        int(version.disabled),
                        format_instant(version.deleted_at) if version.deleted_at else None,
                    ),
                )
        revision_ids = {revision.id for revision in old.revisions}
        added = [revision for revision in new.revisions if revision.id not in revision_ids]
        activations = [a for a in new.activations if a not in old.activations]
        selections = [s for s in new.selections if s not in old.selections]
        history = sorted([*added, *activations, *selections], key=lambda record: record.seq)
        for record in history:
            if record.seq <= old.sequence:
                raise DomainError(PRICING_INVALID, "历史序号不得重复或回退")
            self.connection.execute("INSERT INTO history_seq(id) VALUES (?)", (record.seq,))
        for revision in added:
            self.connection.execute(
                "INSERT INTO price_revision VALUES (?,?,?,?,?)",
                (
                    revision.id,
                    revision.version_id,
                    format_instant(revision.effective_at),
                    revision.seq,
                    fingerprint(revision.models),
                ),
            )
            self.connection.executemany(
                "INSERT INTO price_revision_model"
                "(revision_id,model_key,model_id,display_name,input,cache,output,rule_json,cache_write) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    (
                        revision.id,
                        model.key,
                        model.model_id,
                        model.display_name,
                        model.input,
                        model.cache,
                        model.output,
                        rule_json(model),
                        model.cache_write,
                    )
                    for model in revision.models
                ),
            )
        self.connection.executemany(
            "INSERT INTO version_activation VALUES (?,?,?,?)",
            ((a.group_id, a.version_id, format_instant(a.effective_at), a.seq) for a in activations),
        )
        self.connection.executemany(
            "INSERT INTO harness_selection VALUES (?,?,?,?)",
            ((s.harness.value, s.group_id, format_instant(s.effective_at), s.seq) for s in selections),
        )
        if new.hints != old.hints:
            self.connection.execute("DELETE FROM editor_hint")
            self.connection.executemany(
                "INSERT INTO editor_hint VALUES (?,?,?,?)",
                (
                    (hint.version_id, model_key(hint.model_id), hint.model_id, hint.multiplier)
                    for hint in new.hints
                ),
            )
        for plan in new.switches:
            if plan.operation_id not in prior:
                self.connection.execute(
                    "INSERT INTO operation VALUES (?,'pricing.switch',?,?)",
                    (plan.operation_id, encode_switch(plan), format_instant(plan.effective_at)),
                )
        if new.revision != old.revision:
            work.touch("pricing")

    def _referenced(self, table: str, group_id: str) -> bool:
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            return False
        column = "price_group_id" if table == "ledger" else "group_id"
        return (
            self.connection.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (group_id,)).fetchone()
            is not None
        )

    def _check_history(self, old: Catalog, new: Catalog, removed: set[str]) -> None:
        versions = {version.id: version for version in new.versions}
        for version in old.versions:
            if version.group_id not in removed and (
                version.id not in versions
                or (version.group_id, version.number, version.created_at)
                != (
                    versions[version.id].group_id,
                    versions[version.id].number,
                    versions[version.id].created_at,
                )
            ):
                raise DomainError(PRICING_INVALID, "版本创建信息不可修改或删除")
        removed_versions = {v.id for v in old.versions if v.group_id in removed}
        for previous, updated in (
            (old.revisions, new.revisions),
            (old.activations, new.activations),
            (old.selections, new.selections),
            (old.switches, new.switches),
        ):
            for record in previous:
                allowed = getattr(record, "version_id", None) in removed_versions
                if not allowed and record not in updated:
                    raise DomainError(PRICING_INVALID, "价格历史和操作回执只能追加")

    def delete_group(self, work: UnitOfWork, group_id: str) -> Catalog:
        old = self.load()
        new = rules.delete_group(
            old,
            group_id,
            ledger_referenced=self._referenced("ledger", group_id),
            recalc_referenced=self._referenced("recalc_batch", group_id),
        )
        self.save(work, old, new)
        return new
