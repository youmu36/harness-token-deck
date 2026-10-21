from typing import cast

import pytest
from htd.domain.common.errors import MODEL_NAME_INVALID, DomainError
from htd.domain.usage.model_key import model_key


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("\ufeffMODEL\u0085 A", "modela"),
        (" FIXTURE- MODEL ", "fixture-model"),
        ("A\tB\nC\rD\vE\fF", "abcdef"),
        ("A\u00a0B\u1680C\u2003D\u2028E\u2029F\u202fG\u205fH\u3000I", "abcdefghi"),
        ("\ufeffA\ufeffB\ufeff", "ab"),
        ("Straße", "straße"),  # lower(), not casefold().
        ("模型-É", "模型-é"),
        ("E\u0301", "e\u0301"),  # No Unicode composition.
        ("A\u200bB", "a\u200bb"),  # Zero-width space is not str.isspace().
        ("GPT-5.6/LUNA:v1_2", "gpt-5.6/luna:v1_2"),
    ],
)
def test_model_key(name: str, expected: str) -> None:
    assert model_key(name) == expected
    assert model_key(expected) == expected


@pytest.mark.parametrize("name", ["", " \t\n", "\ufeff\u0085\u3000", None, 12, True, [], {}])
def test_invalid_model_name(name: object) -> None:
    with pytest.raises(DomainError) as failure:
        model_key(cast(str, name))
    assert (failure.value.code, failure.value.field) == (MODEL_NAME_INVALID, "model_id")
