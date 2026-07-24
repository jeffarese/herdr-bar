"""Fuzzy matching.

A compact take on the fzf scoring model: subsequence matching with bonuses for
word boundaries, camelCase transitions and runs of adjacent characters, and
penalties for the gaps between them. Query terms are space separated and
combined with AND, so "tri work" matches an item whose text contains both.

Sizes here are small (a few hundred items, queries under ~20 characters), so
the matcher runs a plain O(len(text) * len(query)) dynamic program and keeps
the exact matched positions for highlighting.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, Tuple

SCORE_MATCH = 16
BONUS_BOUNDARY = 16
BONUS_CAMEL = 12
BONUS_CONSECUTIVE = 16
BONUS_FIRST_CHAR_MULTIPLIER = 2
PENALTY_GAP_START = -3
PENALTY_GAP_EXTENSION = -1

_DELIMITERS = frozenset(" \t/\\-_.:,;|()[]{}<>@#'\"~+*=&%!?")
_NEG = -(10**9)


class Match(NamedTuple):
    score: int
    positions: Tuple[int, ...]


def split_query(query: str) -> List[str]:
    """Split a raw query into AND-ed terms, keeping quoted phrases together."""
    terms: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for char in query:
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char.isspace():
            if current:
                terms.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        terms.append("".join(current))
    return terms


def _bonuses(text: str) -> List[int]:
    out: List[int] = []
    for index, char in enumerate(text):
        if index == 0:
            out.append(BONUS_BOUNDARY)
            continue
        previous = text[index - 1]
        if previous in _DELIMITERS:
            out.append(BONUS_BOUNDARY)
        elif previous.islower() and char.isupper():
            out.append(BONUS_CAMEL)
        elif previous.isdigit() != char.isdigit():
            out.append(BONUS_CAMEL)
        else:
            out.append(0)
    return out


def match(query: str, text: str) -> Optional[Match]:
    """Score one term against one string, or return None when it does not match.

    Matching is smart-case: an all-lowercase query is case insensitive, a query
    with any uppercase character is case sensitive.
    """
    if not query:
        return Match(0, ())
    if not text:
        return None

    fold = query.islower()
    haystack = text.lower() if fold else text
    needle = query.lower() if fold else query

    # Cheap rejection before the DP: every query character must appear in order.
    cursor = 0
    for char in needle:
        cursor = haystack.find(char, cursor)
        if cursor < 0:
            return None
        cursor += 1

    n = len(haystack)
    m = len(needle)
    bonus = _bonuses(text)

    scores: List[List[int]] = []
    parents: List[List[int]] = []

    row = [_NEG] * n
    back = [-1] * n
    first = needle[0]
    for i in range(n):
        if haystack[i] == first:
            row[i] = SCORE_MATCH + bonus[i] * BONUS_FIRST_CHAR_MULTIPLIER
    scores.append(row)
    parents.append(back)

    for j in range(1, m):
        qchar = needle[j]
        previous = scores[j - 1]
        row = [_NEG] * n
        back = [-1] * n
        # Running best over every earlier match of the previous query
        # character, decayed by the gap penalty as the cursor moves right.
        carry = _NEG
        carry_from = -1
        carry_gap = 0
        for i in range(1, n):
            if carry > _NEG:
                carry += PENALTY_GAP_START if carry_gap == 0 else PENALTY_GAP_EXTENSION
                carry_gap += 1
            adjacent = previous[i - 1]
            if adjacent > _NEG and adjacent >= carry:
                carry = adjacent
                carry_from = i - 1
                carry_gap = 0
            if carry == _NEG or haystack[i] != qchar:
                continue
            gained = bonus[i]
            if carry_gap == 0:
                gained = max(gained, BONUS_CONSECUTIVE)
            row[i] = carry + SCORE_MATCH + gained
            back[i] = carry_from
        if all(value == _NEG for value in row):
            return None
        scores.append(row)
        parents.append(back)

    final = scores[m - 1]
    best_index = max(range(n), key=lambda i: final[i])
    if final[best_index] == _NEG:
        return None

    positions: List[int] = []
    index = best_index
    for j in range(m - 1, -1, -1):
        positions.append(index)
        index = parents[j][index]
        if index < 0:
            break
    positions.reverse()
    return Match(final[best_index], tuple(positions))


def match_terms(terms: Sequence[str], text: str) -> Optional[Match]:
    """Match every term against the same text and merge scores and positions."""
    if not terms:
        return Match(0, ())
    total = 0
    positions: List[int] = []
    for term in terms:
        found = match(term, text)
        if found is None:
            return None
        total += found.score
        positions.extend(found.positions)
    return Match(total, tuple(sorted(set(positions))))
