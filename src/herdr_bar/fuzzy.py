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

from typing import Dict, List, NamedTuple, Optional, Tuple

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


# Case folding and word boundaries depend on the field alone, but the matcher
# wants both once per query term per field -- hundreds of times over a single
# keystroke, for strings that have not changed since the bar opened. Entries
# are keyed by whole fields, so the cache is bounded by the size of the
# session rather than by how long the bar stays open; the cap is there for the
# pathological case, and dropping the lot costs one keystroke of recompute.
_PREPARED: Dict[str, Tuple[str, List[int]]] = {}
_PREPARED_LIMIT = 4096


def _prepare(text: str) -> Tuple[str, List[int]]:
    """The folded text and its per-character bonuses. Never mutated by callers."""
    found = _PREPARED.get(text)
    if found is not None:
        return found
    folded = text.lower()
    if len(folded) != len(text):
        # A handful of characters grow when they fold -- "İ" becomes an i and
        # a combining dot -- which slides every later position out of step
        # with the text the bonuses and the highlights describe. Fold those
        # one character at a time and keep the leading one, so a search still
        # finds them and lands its highlight on the right cell.
        folded = "".join(char.lower()[0] for char in text)
    found = (folded, _bonuses(text))
    if len(_PREPARED) >= _PREPARED_LIMIT:
        _PREPARED.clear()
    _PREPARED[text] = found
    return found


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
    if fold:
        haystack, bonus = _prepare(text)
        needle = query.lower()
    else:
        haystack, bonus = text, _prepare(text)[1]
        needle = query

    # Cheap rejection before the DP: every query character must appear in order.
    locate = haystack.find
    cursor = 0
    for char in needle:
        cursor = locate(char, cursor)
        if cursor < 0:
            return None
        cursor += 1

    n = len(haystack)
    m = len(needle)

    # The scoring loop is the hottest thing in the bar -- every field of every
    # item, on every keystroke -- so the constants it reads are bound to locals
    # and the sparse first row is found with str.find rather than a scan.
    neg = _NEG
    gap_open = PENALTY_GAP_START
    gap_more = PENALTY_GAP_EXTENSION
    step = SCORE_MATCH
    adjacent_bonus = BONUS_CONSECUTIVE

    # Nothing outside the first place the query could start and the last place
    # it could end can take part in a match, and for a term that lands early in
    # a long field that is most of the string. The window is in the text's own
    # coordinates, so the boundary bonuses and the reported positions are
    # untouched -- it only stops the loop walking cells that cannot score.
    low = locate(needle[0])
    high = haystack.rfind(needle[m - 1])

    row = [neg] * n
    ceiling = high - m + 1
    at = low
    while 0 <= at <= ceiling:
        row[at] = step + bonus[at] * BONUS_FIRST_CHAR_MULTIPLIER
        at = locate(needle[0], at + 1)
    scores: List[List[int]] = [row]
    parents: List[List[int]] = [[-1] * n]

    for j in range(1, m):
        qchar = needle[j]
        previous = scores[j - 1]
        row = [neg] * n
        back = [-1] * n
        ceiling = high - m + 1 + j
        # Running best over every earlier match of the previous query
        # character, decayed by the gap penalty as the cursor moves right.
        carry = neg
        carry_from = -1
        carry_gap = 0
        landed = False
        for i in range(low + 1, ceiling + 1):
            if carry > neg:
                carry += gap_open if carry_gap == 0 else gap_more
                carry_gap += 1
            adjacent = previous[i - 1]
            if adjacent > neg and adjacent >= carry:
                carry = adjacent
                carry_from = i - 1
                carry_gap = 0
            if carry == neg or haystack[i] != qchar:
                continue
            gained = bonus[i]
            if carry_gap == 0 and gained < adjacent_bonus:
                gained = adjacent_bonus
            row[i] = carry + step + gained
            back[i] = carry_from
            landed = True
        if not landed:
            return None
        scores.append(row)
        parents.append(back)

    final = scores[m - 1]
    best = max(final)
    if best == _NEG:
        return None
    best_index = final.index(best)

    positions: List[int] = []
    index = best_index
    for j in range(m - 1, -1, -1):
        positions.append(index)
        index = parents[j][index]
        if index < 0:
            break
    positions.reverse()
    return Match(final[best_index], tuple(positions))
