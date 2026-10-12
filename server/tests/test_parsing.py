import pytest

from aobs_server.model import StrictJSONError, loads_strict


@pytest.mark.parametrize(
    "text",
    [
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": -Infinity}',
        '{"a": 1e400}',  # Python would silently produce inf
        '{"a": 1, "a": 2}',  # duplicate key
        '{"o": {"k": 1, "k": 1}}',  # nested duplicate, even with equal values
        '{"a": "\\ud800"}',  # lone surrogate in a value
        '{"\\udfff": 1}',  # lone surrogate in a key
        '{"a": ',  # malformed
        "",
    ],
)
def test_rejects_non_strict_json(text: str) -> None:
    with pytest.raises(StrictJSONError):
        loads_strict(text)


def test_rejects_invalid_utf8_bytes() -> None:
    with pytest.raises(StrictJSONError, match="UTF-8"):
        loads_strict(b'{"a": "\xff"}')


def test_accepts_paired_surrogates_and_normal_json() -> None:
    assert loads_strict('{"emoji": "\\ud83d\\ude00", "n": 1.5, "i": 2, "b": true, "z": null}') == {
        "emoji": "\U0001f600",
        "n": 1.5,
        "i": 2,
        "b": True,
        "z": None,
    }


def test_depth_limit() -> None:
    assert loads_strict("[" * 60 + "]" * 60) is not None
    with pytest.raises(StrictJSONError, match="nesting"):
        loads_strict("[" * 100 + "]" * 100)


def test_pathological_depth_does_not_crash() -> None:
    with pytest.raises(StrictJSONError):
        loads_strict("[" * 200_000 + "]" * 200_000)
