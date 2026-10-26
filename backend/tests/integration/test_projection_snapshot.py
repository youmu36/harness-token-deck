"""Exact compressed projections decode only visible rows and retain bounded owned bytes."""

from typing import Any, cast

import pytest
from htd.adapters.sqlite.analysis_cache import AnalysisCache
from htd.adapters.sqlite.projection_snapshot import ProjectionSnapshot


def test_projection_pages_keep_exact_values_quality_filters_and_caller_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import htd.adapters.sqlite.projection_snapshot as module

    amount = str(10**28 + 123)
    models = [{"modelKey": f"模型-{index}", "picoUsd": amount} for index in range(5000)]
    quality = [{"id": str(index), "acknowledged": index % 2 == 0} for index in range(250)]
    original = {"models": models, "quality": quality, "totalModels": 5000, "totals": {"cost": amount}}
    snapshot = ProjectionSnapshot.build(original)
    assert snapshot.materialize() == original
    unpack = module._unpack  # pyright: ignore[reportPrivateUsage]
    decoded: list[int] = []

    def counted(value: bytes) -> Any:
        result = unpack(value)
        if isinstance(result, list):
            decoded.append(len(cast(list[Any], result)))
        return cast(Any, result)

    monkeypatch.setattr(module, "_unpack", counted)
    page = snapshot.materialize(
        {"offset": 4990, "limit": 20, "qualityStatus": "acknowledged", "qualityOffset": 100}
    )
    assert page["models"] == models[4990:]
    assert page["quality"] == quality[::2][100:]
    assert page["qualityTotal"] == 125 and page["qualityNextOffset"] is None
    assert page["nextOffset"] is None
    assert sum(decoded) < 200  # At most the two intersecting model/quality blocks, never 5000 models.
    page["totals"]["cost"] = "0"
    page["models"][0]["modelKey"] = "changed"
    assert snapshot.materialize()["totals"]["cost"] == amount
    assert snapshot.materialize()["models"] == models
    unacknowledged = snapshot.materialize({"qualityStatus": "unacknowledged"})
    assert unacknowledged["quality"] == quality[1::2][:100]
    assert unacknowledged["qualityNextOffset"] == 100


def test_encoded_cache_budget_expiry_eviction_and_completed_controls() -> None:
    cache = AnalysisCache(max_bytes=4096, capacity=2)
    for key in ("a", "b", "c"):
        future, owner = cache.claim(key)
        assert owner
        cache.finish(key, future, {"value": key * 1000}, cache.generation, expires_at=20)
    assert list(cache.entries) == ["b", "c"]
    assert 0 < cache.bytes <= 4096
    assert not cache.pending and not cache.controls and "a" not in cache.expires
    hit, owner = cache.claim("b", at=19)
    assert not owner and hit.result().expires_at == 20
    assert hit.result().materialize() == {"value": "b" * 1000}
    expired, owner = cache.claim("b", at=20)
    assert owner
    cache.finish("b", expired, {"new": 1}, cache.generation)
    assert "b" not in cache.expires
    assert expired.result().expires_at is None
    cache.clear()
    assert not cache.entries and cache.bytes == 0 and not cache.expires
    too_small = AnalysisCache(max_bytes=1)
    future, _ = too_small.claim("oversize")
    too_small.finish("oversize", future, {"value": "exact"}, too_small.generation)
    assert not too_small.entries and future.result().materialize() == {"value": "exact"}
    assert not too_small.pending and not too_small.controls


def test_small_results_and_monitor_policy_do_not_compress(monkeypatch: pytest.MonkeyPatch) -> None:
    import htd.adapters.sqlite.projection_snapshot as module

    def unexpected(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Small/monitor results must not spend CPU in zlib")

    monkeypatch.setattr(module.zlib, "compress", unexpected)
    monkeypatch.setattr(module.zlib, "decompress", unexpected)
    small = {"label": "精确数值", "amount": str(10**26 + 123)}
    assert ProjectionSnapshot.build(small).materialize() == small
    large_monitor = {"trend": [{"bucket": str(i), "value": str(10**25 + i)} for i in range(60)]}
    cache = AnalysisCache(capacity=2, max_bytes=1024**2, compress=False)
    pending, _ = cache.claim("monitor")
    cache.finish("monitor", pending, large_monitor, cache.generation)
    result = pending.result().materialize()
    assert result == large_monitor
    result["trend"].clear()
    assert pending.result().materialize() == large_monitor


@pytest.mark.parametrize("acknowledged", [False, True])
def test_uniform_quality_status_reuses_encoded_rows(acknowledged: bool) -> None:
    rows = [{"id": str(i), "acknowledged": acknowledged, "amount": str(10**24 + i)} for i in range(300)]
    snapshot = ProjectionSnapshot.build({"quality": rows})
    assert snapshot.quality is not None
    all_rows, confirmed, pending = snapshot.quality
    assert all_rows is (confirmed if acknowledged else pending)
    assert not (pending if acknowledged else confirmed).count
    assert (
        snapshot.materialize(
            {"qualityStatus": "acknowledged" if acknowledged else "unacknowledged", "qualityOffset": 100}
        )["quality"]
        == rows[100:200]
    )
