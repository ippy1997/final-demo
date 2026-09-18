"""Task 4 acceptance cases for ``app/ids.py`` (TASKS.md item 4).

``app/ids.py`` ships ``new_id()`` over ``secrets.token_urlsafe(16)``, and the
task's criterion is that a link carries enough randomness to be unguessable
while pastes stay unenumerable (PRD.md item 3, TASKS.md item 4). These cases
pin what a reader of a link can rely on: the 22-character length, the URL-safe
alphabet and the absence of padding, the 16 bytes behind the characters, that
the id goes into a path as-is without escaping, that the generator is the
stdlib ``secrets.token_urlsafe`` asked for 16 bytes, and that 1000 ids drawn
back to back are all distinct — with the distinctness measurement shown to
bite, so it cannot pass by vacuity.

They complement ``tests/test_ids.py`` (same task), which already covers the
returned type, the three checks the task names, the decoding to 128 bits, the
argument-less signature and the recording stub for ``secrets.token_urlsafe``.

Nothing here can observe an id arriving in a create response, resolving on a
link, or answering the uniform 404 for a wrong but well-formed id — those
cases need the store and the routes (TASKS.md items 5-7), and PRD.md item 3's
"no endpoint lists or searches pastes" and item 9's "the same text twice gives
two ids" need the create route, so both are recorded as untestable on this
task.
"""

from __future__ import annotations

import base64
import inspect
import string
from urllib.parse import quote

import pytest

from app import ids

# The documented length and the byte count behind it (ARCHITECTURE.md Parts and
# its decision row; PRD.md's defaults: "22-character URL-safe base64" over 128
# random bits).
DOCUMENTED_ID_LENGTH_CHARS = 22
DOCUMENTED_ID_BITS = 128
DOCUMENTED_ID_BYTES = DOCUMENTED_ID_BITS // 8

# The draws PRD.md item 3 states its check over, and a smaller sample for the
# per-id properties that do not need 1000 repetitions.
DOCUMENTED_BACK_TO_BACK_IDS = 1000
SAMPLE_IDS = 200

# The URL-safe base64 alphabet, and the characters only the standard alphabet
# or a padded render produces.
URL_SAFE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
URL_UNSAFE_BASE64_CHARS = frozenset("+/=")


class _ConstantSecrets:
    """Stands in for ``secrets`` and returns one id every time, never a fresh one."""

    def token_urlsafe(self, nbytes: int) -> str:
        return "A" * DOCUMENTED_ID_LENGTH_CHARS


class _RecordingSecrets:
    """Stands in for ``secrets``, recording the byte count of every call."""

    def __init__(self, rendered: str) -> None:
        self.rendered = rendered
        self.calls: list[int] = []

    def token_urlsafe(self, nbytes: int) -> str:
        self.calls.append(nbytes)
        return self.rendered


def _all_distinct(drawn: list[str]) -> bool:
    """Whether a batch of ids has no repeat, the measurement item 3 makes."""
    return len(set(drawn)) == len(drawn)


def _padding_for(identifier: str) -> str:
    """The base64 padding ``token_urlsafe`` dropped, restored for decoding."""
    return "=" * (-len(identifier) % 4)


def test_tc001_every_id_is_exactly_twenty_two_characters() -> None:
    """The documented length, whatever bytes the draw produced."""
    lengths = {len(ids.new_id()) for _ in range(SAMPLE_IDS)}

    assert lengths == {DOCUMENTED_ID_LENGTH_CHARS}


def test_tc002_ids_use_the_url_safe_base64_alphabet_with_no_padding() -> None:
    """``A-Za-z0-9-_``: a ``+``, a ``/`` or a ``=`` would break a pasted link."""
    drawn = [ids.new_id() for _ in range(SAMPLE_IDS)]

    for identifier in drawn:
        assert set(identifier) <= URL_SAFE_ALPHABET
        assert not set(identifier) & URL_UNSAFE_BASE64_CHARS
        assert identifier == identifier.rstrip("=")


def test_tc003_a_thousand_ids_created_back_to_back_are_all_distinct() -> None:
    """PRD.md item 3: 1000 pastes created in a row all have distinct ids."""
    drawn = [ids.new_id() for _ in range(DOCUMENTED_BACK_TO_BACK_IDS)]

    assert len(drawn) == DOCUMENTED_BACK_TO_BACK_IDS == 1000
    assert _all_distinct(drawn)


def test_tc004_the_distinctness_measurement_bites_on_a_repeating_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps tc003 honest: a generator that repeats one id fails that measurement."""
    monkeypatch.setattr(ids, "secrets", _ConstantSecrets())
    repeated = [ids.new_id() for _ in range(DOCUMENTED_BACK_TO_BACK_IDS)]

    assert not _all_distinct(repeated)
    assert len(set(repeated)) == 1


def test_tc005_an_id_decodes_to_the_documented_one_hundred_and_twenty_eight_bits() -> None:
    """PRD.md item 3's floor: 128 bits, i.e. 16 random bytes behind 22 characters."""
    identifier = ids.new_id()
    decoded = base64.urlsafe_b64decode(identifier + _padding_for(identifier))

    assert len(decoded) == DOCUMENTED_ID_BYTES == 16
    assert len(decoded) * 8 == DOCUMENTED_ID_BITS


def test_tc006_an_id_needs_no_escaping_in_a_url_path() -> None:
    """The link can be pasted into a browser as-is (PRD.md Users: paste reader)."""
    drawn = [ids.new_id() for _ in range(SAMPLE_IDS)]

    for identifier in drawn:
        assert quote(identifier, safe="") == identifier
        assert quote(identifier, safe="/") == identifier


def test_tc007_the_generator_is_the_stdlib_secrets_token_urlsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chosen source, asked for the documented byte count once per id."""
    stub = _RecordingSecrets(rendered="A" * DOCUMENTED_ID_LENGTH_CHARS)
    monkeypatch.setattr(ids, "secrets", stub)

    assert ids.new_id() == "A" * DOCUMENTED_ID_LENGTH_CHARS
    assert ids.new_id() == "A" * DOCUMENTED_ID_LENGTH_CHARS

    assert stub.calls == [DOCUMENTED_ID_BYTES, DOCUMENTED_ID_BYTES]


def test_tc008_new_id_takes_no_arguments() -> None:
    """Nothing a caller supplies steers an id: no text, no length, no seed."""
    assert list(inspect.signature(ids.new_id).parameters) == []


def test_tc009_no_character_position_is_a_counter_or_a_fixed_prefix() -> None:
    """Every position is drawn: a counter or a stamp would pin the first one."""
    drawn = [ids.new_id() for _ in range(DOCUMENTED_BACK_TO_BACK_IDS)]
    leading_characters = {identifier[0] for identifier in drawn}

    # 1000 draws from a 64-character alphabet reach all 64 with overwhelming
    # probability; a sequence, a counter or a prefix reaches a handful.
    assert len(leading_characters) >= 30
    assert leading_characters <= URL_SAFE_ALPHABET
