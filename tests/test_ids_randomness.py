"""Task 4 acceptance cases for the randomness behind ``app/ids.py``
(TASKS.md item 4; PRD.md item 3).

``new_id()`` renders one draw of the stdlib generator — ``secrets.token_urlsafe(16)``
— as 22 URL-safe characters, so a link carries 128 bits of randomness, ids are
unguessable and pastes are not enumerable (PRD.md item 3; ARCHITECTURE.md
Parts, table row "Ids", and its decision row for the generator). Ids come from
``ids.new_id()`` and never from a counter or the text (AGENTS.md Conventions:
"never a counter, never derived from the text" / "never derived from the text").

These cases cover what the two existing files for this task leave open, all of
it at the scale the task names (1000 draws back to back, not a sample):

- tc101: the format holds for all 1000 draws, not just the 200-draw sample.
- tc102: the 1000 draws are unordered, which a counter or a sequence is not.
- tc103: two calls in a row give two different ids and two different objects.
- tc104: 1000 ids cost exactly 1000 generator calls of 16 bytes each, and the
  id is the generator's render unchanged.
- tc105: every id is ASCII and safe as a URL path segment, unescaped.
- tc106: with the wall clock frozen, 1000 ids are still all distinct — an id
  is not derived from time.
- tc107: a generator rendering standard base64 is detected as outside the
  URL-safe alphabet, so the alphabet and length cases cannot pass by vacuity.

``tests/test_ids.py`` and ``tests/test_ids_contract.py`` cover the same task's
returned type, the three checks the task spells out, the decode to 128 bits
and the delegation to ``secrets.token_urlsafe(16)``; nothing here repeats them
or weakens them. Cases that need a real paste — an id in a create response,
resolving on a link, the uniform 404 for a wrong but well-formed id, "no
endpoint lists or searches pastes" — need the store and the routes (TASKS.md
items 5-7) and are recorded as untestable on this task.
"""

from __future__ import annotations

import base64
import string
import time
from urllib.parse import urlsplit

import pytest

from app import ids

# The documented id: 22 characters of unpadded URL-safe base64 over 128 random
# bits (PRD.md's applied defaults; ARCHITECTURE.md Parts and its decision row).
DOCUMENTED_ID_LENGTH_CHARS = 22
DOCUMENTED_ID_BYTES = 16

# The draws the task and PRD.md item 3 put their check over.
BACK_TO_BACK_IDS = 1000

# The alphabet an unpadded URL-safe base64 render may use, and the characters
# only the standard alphabet or a padded render produces.
URL_SAFE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
URL_UNSAFE_BASE64_CHARS = frozenset("+/=")

# An instant to freeze the wall clock at while ids are drawn.
FROZEN_INSTANT = 1_700_000_000.0

# The host the returned link is opened on (README.md, ARCHITECTURE.md
# Verification: `--host 127.0.0.1 --port 8000`).
LINK_PREFIX = "http://127.0.0.1:8000/pastes/"


class _DistinctRenderingSecrets:
    """Stands in for ``secrets``, rendering a different value on every call.

    Installed in place of ``ids.secrets`` so the test can read how many draws
    were made and what each of them was asked for.
    """

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.renders: list[str] = []

    def token_urlsafe(self, nbytes: int) -> str:
        self.calls.append(nbytes)
        render = f"{len(self.renders):0{DOCUMENTED_ID_LENGTH_CHARS}d}"
        self.renders.append(render)
        return render


class _ConstantRenderingSecrets:
    """Stands in for ``secrets`` and returns one fixed render every time."""

    def __init__(self, rendered: str) -> None:
        self.rendered = rendered

    def token_urlsafe(self, nbytes: int) -> str:
        return self.rendered


class _FrozenWallClock:
    """Stands in for ``time.time`` and always reports the same instant."""

    def __init__(self, instant: float) -> None:
        self.instant = instant

    def __call__(self) -> float:
        return self.instant


def _standard_base64_render_of_16_bytes() -> str:
    """A 16-byte payload rendered with the standard, URL-unsafe base64 alphabet.

    The payload starts with ``0xFB``, whose top six bits are base64 index 62,
    i.e. ``+``: the render cannot be mistaken for a URL-safe one.
    """
    return base64.b64encode(bytes([0xFB]) * DOCUMENTED_ID_BYTES).decode("ascii")


def test_tc101_all_one_thousand_back_to_back_ids_have_the_documented_format() -> None:
    """The task's own scale: 1000 ids, each 22 URL-safe characters, all distinct."""
    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]

    assert len(drawn) == BACK_TO_BACK_IDS == 1000
    for identifier in drawn:
        assert len(identifier) == DOCUMENTED_ID_LENGTH_CHARS
        assert set(identifier) <= URL_SAFE_ALPHABET
        assert not set(identifier) & URL_UNSAFE_BASE64_CHARS
    assert len(set(drawn)) == BACK_TO_BACK_IDS


def test_tc102_one_thousand_sequential_ids_are_not_in_ascending_order() -> None:
    """A counter or a sequence arrives sorted; 1000 random draws do not."""
    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]

    descending_steps = [
        (earlier, later)
        for earlier, later in zip(drawn, drawn[1:])
        if earlier > later
    ]

    assert descending_steps, "1000 ids came back in ascending order"
    assert drawn != sorted(drawn)


def test_tc103_two_consecutive_calls_return_two_different_fresh_ids() -> None:
    """Nothing is cached or reused: each call is a new id, and a new string."""
    first = ids.new_id()
    second = ids.new_id()

    assert first != second
    assert first is not second
    for identifier in (first, second):
        assert len(identifier) == DOCUMENTED_ID_LENGTH_CHARS
        assert set(identifier) <= URL_SAFE_ALPHABET


def test_tc104_one_thousand_ids_cost_one_thousand_draws_of_sixteen_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned to ``secrets.token_urlsafe(16)`` once per id, the render returned as-is."""
    stub = _DistinctRenderingSecrets()
    monkeypatch.setattr(ids, "secrets", stub)

    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]

    assert stub.calls == [DOCUMENTED_ID_BYTES] * BACK_TO_BACK_IDS
    assert drawn == stub.renders
    assert len(set(drawn)) == BACK_TO_BACK_IDS


def test_tc105_one_thousand_ids_survive_a_url_path_with_no_escaping() -> None:
    """The link can be pasted into a browser as-is (PRD.md Users: paste reader)."""
    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]

    for identifier in drawn:
        assert identifier.isascii()
        assert identifier.encode("ascii").decode("ascii") == identifier

        parts = urlsplit(LINK_PREFIX + identifier)
        assert parts.path == "/pastes/" + identifier
        assert parts.query == ""
        assert parts.fragment == ""


def test_tc106_one_thousand_ids_drawn_with_the_wall_clock_frozen_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id is not derived from time: a frozen clock still yields 1000 unique ids."""
    frozen = _FrozenWallClock(instant=FROZEN_INSTANT)
    monkeypatch.setattr(time, "time", frozen)

    assert time.time is frozen
    assert time.time() == FROZEN_INSTANT

    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]

    assert len(set(drawn)) == BACK_TO_BACK_IDS


def test_tc107_a_standard_base64_render_is_outside_the_url_safe_alphabet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps tc101 honest: a wrong generator's output is detectable, not invisible."""
    standard_render = _standard_base64_render_of_16_bytes()
    unpadded = standard_render.rstrip("=")

    assert standard_render.endswith("==")
    assert unpadded[0] == "+"
    assert len(unpadded) == DOCUMENTED_ID_LENGTH_CHARS
    assert set(unpadded) & URL_UNSAFE_BASE64_CHARS
    assert not set(unpadded) <= URL_SAFE_ALPHABET

    monkeypatch.setattr(ids, "secrets", _ConstantRenderingSecrets(unpadded))

    returned = ids.new_id()

    assert returned == unpadded
    assert set(returned) & URL_UNSAFE_BASE64_CHARS
    assert not set(returned) <= URL_SAFE_ALPHABET
