"""Score Markdown and locate blocks that can be rendered independently."""

import re
from collections import Counter
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token


@dataclass(frozen=True)
class MarkdownAnalysis:
    """Whole/outside formatting scores and independently renderable source ranges."""

    score: int
    outside_score: int
    block_spans: tuple[tuple[int, int], ...]


def _nonempty_code_lines(token: Token) -> int:
    return sum(bool(line.strip()) for line in token.content.splitlines())


def _score_tokens(tokens: list[Token]) -> int:
    """Apply category caps independently to the supplied portion of the reply."""
    blocks = Counter()
    inline = Counter()
    code_score = 0
    for token in tokens:
        blocks[token.type] += 1
        if token.type in {"fence", "code_block"}:
            lines = _nonempty_code_lines(token)
            code_score += 6 if lines >= 2 else 2 if lines else 0
        elif token.type == "inline":
            inline.update(child.type for child in token.children or [])

    # Per-category caps keep repeated inline markup from dominating the decision.
    return (
        min(blocks["table_open"] * 6, 6)
        + min(code_score, 6)
        + min(blocks["heading_open"] * 2, 6)
        + min(blocks["list_item_open"], 6)
        + min(blocks["blockquote_open"] * 2, 4)
        + min(inline["strong_open"] + inline["em_open"] + inline["s_open"], 2)
        + min(inline["code_inline"] + inline["link_open"], 2)
        + min(blocks["hr"], 1)
    )


def analyze_markdown(text: str) -> MarkdownAnalysis:
    """Score the reply and separately score formatting outside tables/multiline code.

    Args:
        text: Complete Markdown reply, bounded by the caller before parsing.

    Returns:
        Whole/outside scores and character ranges for partial rendering. The caller
        compares both scores with its threshold. Empty ranges mean the reply cannot
        be rendered in independent blocks.
    """
    parser = MarkdownIt("commonmark", {"html": False}).enable(
        ["table", "strikethrough"]
    )
    environment = {}
    tokens = parser.parse(text, environment)
    score = _score_tokens(tokens)
    # Reference definitions outside a block must remain with their consumers.
    if environment.get("references"):
        return MarkdownAnalysis(score, score, ())

    line_spans = []
    outside_tokens = []
    in_table = False
    for token in tokens:
        if token.type == "table_open" and token.level == 0:
            line_spans.append(token.map)
            in_table = True
        elif in_table:
            if token.type == "table_close":
                in_table = False
        elif (
            token.type in {"fence", "code_block"}
            and token.level == 0
            and _nonempty_code_lines(token) >= 2
        ):
            line_spans.append(token.map)
        else:
            outside_tokens.append(token)

    outside_score = _score_tokens(outside_tokens)
    if not line_spans:
        return MarkdownAnalysis(score, outside_score, ())

    # Match MarkdownIt's newline normalization while retaining original characters.
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n?|\n", text))]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return MarkdownAnalysis(
        score,
        outside_score,
        tuple((offsets[start], offsets[end]) for start, end in line_spans),
    )
