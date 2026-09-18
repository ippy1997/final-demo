"""Task 4: ``app/ids.py``, the paste id generator (TASKS.md item 4).

The module ships ``new_id()`` over ``secrets.token_urlsafe(16)``, so every
paste id carries 128 random bits rendered as 22 characters of unpadded
URL-safe base64 (PRD.md item 3; ARCHITECTURE.md Parts, table row "Ids", and its
decision row for the generator). Ids are unguessable and pastes are not
enumerable, which is why they come from the stdlib ``secrets`` module and
never from a counter, a timestamp or a hash of the text (AGENTS.md
Conventions).

The three checks TASKS.md item 4 names appear below: the length, the alphabet
and 1000 ids created back to back all distinct. The store and the create route
arrive with TASKS.md items 5 and 6, so "1000 pastes created back to back" is
1000 calls to ``new_id()`` in a row — the same call the create route will make
once per paste, with nothing in between that could distinguish them.
"""

from __future__ import annotations

import base64
import inspect
import string

import pytest

from app import ids

# The byte count behind an id: TASKS.md item 4 and ARCHITECTURE.md's decision
# row both name ``secrets.token_urlsafe(16)``, and 16 bytes is the 128 bits of
# randomness PRD.md item 3 requires of a link that is the only credential
# (ARCHITECTURE.md Sign-in).
ID_BYTES = 16

# 128 bits over base64's 6 bits per character, with the padding dropped:
# ceil(128 / 6) = 22 characters, the length ARCHITECTURE.md's verification gate
# probes and PRD.md's defaults quote.
ID_LENGTH_CHARS = 22

# The alphabet an unpadded URL-safe base64 render can use, and the characters
# that would betray the standard base64 alphabet or padding instead.
URL_SAFE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
URL_UNSAFE_BASE64_CHARS = frozenset("+/=")

# How many ids the task asks for, drawn in one run with nothing in between
# (PRD.md item 3: "1000 pastes created back to back all have distinct ids").
BACK_TO_BACK_IDS = 1000

# A sample size for the per-id properties, where 1000 draws would only repeat
# the same assertion 1000 times.
SAMPLE_IDS = 200


class _RecordingSecrets:
    """Stands in for the ``secrets`` module and records what it was asked for.

    Installed in place of ``ids.secrets`` to pin the delegation the task names,
    the way ``tests/test_clock.py`` pins ``clock.now`` to ``time.time()``: the
    answer is fixed, so the test reads the argument and the call count instead.
    """

    def __init__(self, rendered: str) -> None:
        self.rendered = rendered
        self.calls: list[int] = []

    def token_urlsafe(self, nbytes: int) -> str:
        self.calls.append(nbytes)
        return self.rendered


def test_new_id_returns_a_string() -> None:
    """The type a route returns as JSON, and a store row keys on."""
    identifier = ids.new_id()

    assert isinstance(identifier, str)
    assert identifier != ""


def test_new_id_is_twenty_two_characters() -> None:
    """Every id, not just the first: one length, whatever the drawn bytes are."""
    lengths = {len(ids.new_id()) for _ in range(SAMPLE_IDS)}

    assert lengths == {ID_LENGTH_CHARS}


def test_new_id_uses_only_the_url_safe_base64_alphabet() -> None:
    """``A-Za-z0-9-_``: no ``+``, no ``/`` and no ``=`` padding to escape."""
    drawn = [ids.new_id() for _ in range(SAMPLE_IDS)]
    used = set().union(*(set(identifier) for identifier in drawn))

    # The sample is non-empty, so an all-blank render cannot pass on an
    # empty character set that happens to be a subset of the alphabet.
    assert used
    assert used <= URL_SAFE_ALPHABET
    assert not used & URL_UNSAFE_BASE64_CHARS


def test_standard_base64_output_would_fail_the_alphabet_case() -> None:
    """Keeps the case above honest: a ``+/=`` render is detectable, not invisible."""
    standard_render = base64.b64encode(b"\xff\xef\xbe").decode("ascii")

    assert set(standard_render) & URL_UNSAFE_BASE64_CHARS
    assert not set(standard_render) <= URL_SAFE_ALPHABET


def test_new_id_carries_one_hundred_and_twenty_eight_random_bits() -> None:
    """PRD.md item 3: at least 128 bits — the 22 characters decode to 16 bytes."""
    identifier = ids.new_id()
    padding = "=" * (-len(identifier) % 4)

    decoded = base64.urlsafe_b64decode(identifier + padding)

    assert len(decoded) == ID_BYTES
    assert len(decoded) * 8 >= 128


def test_a_thousand_ids_created_back_to_back_are_all_distinct() -> None:
    """PRD.md item 3's check: no collision across 1000 pastes made in a row."""
    drawn = [ids.new_id() for _ in range(BACK_TO_BACK_IDS)]

    assert len(drawn) == BACK_TO_BACK_IDS == 1000
    assert len(set(drawn)) == BACK_TO_BACK_IDS


def test_new_id_takes_no_arguments() -> None:
    """No caller input steers an id: not the text, not a chosen length."""
    assert list(inspect.signature(ids.new_id).parameters) == []


def test_new_id_asks_the_stdlib_generator_for_sixteen_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned to ``secrets.token_urlsafe(16)``: 128 bits per call, once per id."""
    stub = _RecordingSecrets(rendered="A" * ID_LENGTH_CHARS)
    monkeypatch.setattr(ids, "secrets", stub)

    first = ids.new_id()
    second = ids.new_id()

    assert first == second == "A" * ID_LENGTH_CHARS
    assert stub.calls == [ID_BYTES, ID_BYTES]
