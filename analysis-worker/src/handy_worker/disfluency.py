"""Find and highlight fillers, false starts and repetitions in a transcript.

The rules mirror Handy's speech analysis (``src-tauri/src/personal/
analysis.rs``) so the spike shows what the Insights page will count.
CrisperWhisper writes fillers as ``[UH]``/``[UM]``; brackets and case are
ignored.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

PURE_FILLERS = frozenset(
    ["um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm"]
)
#: Counted only when followed by a comma ("like," / "you know," / "I mean,").
DISCOURSE_FILLERS: tuple[tuple[str, ...], ...] = (("like",), ("you", "know"), ("i", "mean"))
ALLOWED_REPEATS = frozenset({"that", "had", "is", "do", "very", "no", "bye"})

FILLER = "filler"
FALSE_START = "false_start"
REPETITION = "repetition"

_TRAILING = ",.?!;:\"')]…"
_DASHES = ("-", "—", "–")


@dataclass
class Token:
    raw: str
    word: str
    comma_after: bool
    cut_off: bool


@dataclass
class Analysis:
    tokens: list[Token]
    #: Token index -> kind.
    marks: dict[int, str] = field(default_factory=dict)
    #: Occurrences per kind ("you know," is one filler over two tokens).
    counts: dict[str, int] = field(default_factory=dict)

    def count(self, kind: str) -> int:
        return self.counts.get(kind, 0)

    def _add(self, kind: str, indices: range) -> None:
        for k in indices:
            self.marks[k] = kind
        self.counts[kind] = self.counts.get(kind, 0) + 1

    @property
    def words(self) -> int:
        return sum(1 for i, _ in enumerate(self.tokens) if self.marks.get(i) != FILLER)


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    for piece in text.split():
        stripped_end = piece.rstrip(_TRAILING)
        trailing = piece[len(stripped_end) :]
        comma_after = "," in trailing or "…" in trailing or ".." in trailing
        cut_off = stripped_end.endswith(_DASHES)
        word = re.sub(r"^[^\w']+|[^\w']+$", "", stripped_end).lower().replace("’", "'")
        if not word:
            if cut_off and tokens:
                tokens[-1].cut_off = True
            continue
        tokens.append(Token(piece, word, comma_after, cut_off))
    return tokens


def analyze(text: str) -> Analysis:
    tokens = tokenize(text)
    a = Analysis(tokens)
    i = 0
    while i < len(tokens):
        if tokens[i].word in PURE_FILLERS:
            a._add(FILLER, range(i, i + 1))
            i += 1
            continue
        matched = False
        for phrase in DISCOURSE_FILLERS:
            end = i + len(phrase)
            if (
                end <= len(tokens)
                and tuple(t.word for t in tokens[i:end]) == phrase
                and tokens[end - 1].comma_after
            ):
                a._add(FILLER, range(i, end))
                i = end
                matched = True
                break
        if not matched:
            i += 1
    for idx, t in enumerate(tokens):
        if t.cut_off and idx not in a.marks:
            a._add(FALSE_START, range(idx, idx + 1))
    content = [idx for idx in range(len(tokens)) if a.marks.get(idx) != FILLER]
    for prev, cur in itertools.pairwise(content):
        p, c = tokens[prev], tokens[cur]
        if (
            p.word == c.word
            and not p.cut_off
            and p.word not in ALLOWED_REPEATS
            and cur not in a.marks
        ):
            a._add(REPETITION, range(cur, cur + 1))
    return a


_COLORS = {FILLER: "\033[1;33m", FALSE_START: "\033[1;35m", REPETITION: "\033[1;36m"}
_BRACKETS = {FILLER: ("[", "]"), FALSE_START: ("<", ">"), REPETITION: ("{", "}")}
_RESET = "\033[0m"


def highlight(text: str, color: bool = True) -> str:
    """The text with disfluencies marked: colours on a terminal, otherwise
    [filler] <false start> {repetition}."""
    a = analyze(text)
    out: list[str] = []
    for idx, t in enumerate(a.tokens):
        kind = a.marks.get(idx)
        if kind is None:
            out.append(t.raw)
        elif color:
            out.append(f"{_COLORS[kind]}{t.raw}{_RESET}")
        else:
            left, right = _BRACKETS[kind]
            out.append(f"{left}{t.raw}{right}")
    return " ".join(out)


def summary(text: str) -> dict[str, int]:
    a = analyze(text)
    return {
        "words": a.words,
        "fillers": a.count(FILLER),
        "false_starts": a.count(FALSE_START),
        "repetitions": a.count(REPETITION),
    }
