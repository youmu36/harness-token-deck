from typing import cast
from uuid import NAMESPACE_DNS, NAMESPACE_URL, RFC_4122, UUID

import pytest
from htd.domain.common.errors import ID_INVALID, IDENTITY_INVALID, DomainError
from htd.domain.common.hashing import identity_key
from htd.domain.common.ids import deterministic_id, new_id


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (["s", "r"], "f8ed39c43dd1361d70a734cc7b601c7a9d7f92cfe879bc27f80f893a7b16360a"),
        (["会话", "请求"], "d8af46e0f82e073021f21cb0446b6dd2edd9d3086d64a2aa84b60a4a46cc866f"),
        (['a"b', "c\\d"], "1413961e2c1aaa10c7dafe4937c0ce6d884663923ee568cf0a4b0a48b1b5d310"),
    ],
)
def test_identity_fixed_vectors(parts: list[str | int], expected: str) -> None:
    assert identity_key(parts) == expected


def test_identity_preserves_type_order_whitespace_and_unicode() -> None:
    shapes: list[list[str | int]] = [
        ["s", "r", 1],
        ["s", "r", "1"],
        ["r", "s", 1],
        [" s ", "r", 1],
        ["ab", "c"],
        ["a", "bc"],
        ["é"],
        ["e\u0301"],
        ["s|r"],
        ["s", "r"],
    ]
    assert len({identity_key(parts) for parts in shapes}) == len(shapes)
    for parts in shapes:
        assert len(identity_key(parts)) == 64


@pytest.mark.parametrize("part", [True, False, None, 1.5, [], {}, "\ud800"])
def test_identity_rejects_invalid_parts(part: object) -> None:
    with pytest.raises(DomainError) as failure:
        identity_key([cast(str | int, part)])
    assert failure.value.code == IDENTITY_INVALID


def test_new_ids_are_canonical_random_uuid4() -> None:
    identifiers = {new_id() for _ in range(20)}
    assert len(identifiers) == 20
    for identifier in identifiers:
        parsed = UUID(identifier)
        assert parsed.version == 4
        assert parsed.variant == RFC_4122
        assert str(parsed) == identifier


def test_uuid5_fixed_vector_and_namespace() -> None:
    identifier = deterministic_id(NAMESPACE_URL, "s", "r")
    assert identifier == "23d1b9dc-eb2e-528e-942e-01023dc41eb2"
    assert UUID(identifier).version == 5
    assert deterministic_id(str(NAMESPACE_URL), "s", "r") == identifier
    assert deterministic_id(NAMESPACE_DNS, "s", "r") != identifier
    assert deterministic_id(NAMESPACE_URL, "s", 1) != deterministic_id(NAMESPACE_URL, "s", "1")
    assert deterministic_id(NAMESPACE_URL, "ab", "c") != deterministic_id(NAMESPACE_URL, "a", "bc")


def test_invalid_namespace_is_structured_error() -> None:
    with pytest.raises(DomainError) as failure:
        deterministic_id("not-a-uuid", "s")
    assert failure.value.code == ID_INVALID
