"""Score Markdown and locate blocks that can be rendered independently."""

import re
from collections import Counter
from dataclasses import dataclass

from markdown_it.token import Token

from .markdown_parser import create_parser, is_mermaid


@dataclass(frozen=True)
class MarkdownAnalysis:
    """Whole/outside formatting scores and independently renderable source ranges."""

    score: int
    outside_score: int
    block_spans: tuple[tuple[int, int], ...]
    has_long_list: bool


def _nonempty_code_lines(token: Token) -> int:
    return sum(bool(line.strip()) for line in token.content.splitlines())


def _score_tokens(tokens: list[Token]) -> int:
    """Apply category caps independently to the supplied portion of the reply."""
    blocks = Counter()
    inline = Counter()
    code_score = 0
    diagram_score = 0
    math_score = 0
    for token in tokens:
        blocks[token.type] += 1
        if is_mermaid(token):
            diagram_score += 6 if token.content.strip() else 0
        elif token.type == "math_block":
            math_score += 6 if token.content.strip() else 0
        elif token.type in {"fence", "code_block"}:
            lines = _nonempty_code_lines(token)
            code_score += 6 if lines >= 2 else 2 if lines else 0
        elif token.type == "inline":
            inline.update(child.type for child in token.children or [])
            math_score += 6 * sum(
                child.type in {"math_inline", "math_inline_double"}
                and bool(child.content.strip())
                for child in token.children or []
            )

    # Per-category caps keep repeated inline markup from dominating the decision.
    return (
        min(blocks["table_open"] * 6, 6)
        + min(code_score, 6)
        + min(diagram_score, 6)
        + min(math_score, 6)
        + min(blocks["heading_open"] * 2, 6)
        + min(blocks["list_item_open"], 6)
        + min(blocks["blockquote_open"] * 2, 4)
        + min(inline["strong_open"] + inline["em_open"] + inline["s_open"], 2)
        + min(inline["code_inline"] + inline["link_open"], 2)
        + min(blocks["hr"], 1)
    )


def analyze_markdown(text: str, threshold: int) -> MarkdownAnalysis:
    """Locate independent blocks and score the reply without qualifying lists.

    Args:
        text: Complete Markdown reply, bounded by the caller before parsing.
        threshold: Minimum direct item count for an independent top-level list.

    Returns:
        Whole/outside scores, character ranges and a score-independent list trigger.
        Reference definitions prevent partial rendering to retain link context.
    """
    parser = create_parser()
    environment = {}
    tokens = parser.parse(text, environment)
    line_spans = []
    scoring_tokens = []
    outside_tokens = []
    has_long_list = False
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token.level == 0 and token.type in {
            "table_open",
            "bullet_list_open",
            "ordered_list_open",
        }:
            block_end = token_index + 1
            while tokens[block_end].level != 0:
                block_end += 1
            block_tokens = tokens[token_index : block_end + 1]
            is_long_list = (
                token.type != "table_open"
                and sum(
                    child.type == "list_item_open" and child.level == 1
                    for child in block_tokens
                )
                >= threshold
            )
            if is_long_list:
                has_long_list = True
            else:
                scoring_tokens.extend(block_tokens)
            if token.type == "table_open" or is_long_list:
                line_spans.append(token.map)
            else:
                outside_tokens.extend(block_tokens)
            token_index = block_end + 1
            continue

        scoring_tokens.append(token)
        if (
            token.type == "math_block"
            and bool(token.content.strip())
            or is_mermaid(token)
            and bool(token.content.strip())
            or token.type in {"fence", "code_block"}
            and not is_mermaid(token)
            and _nonempty_code_lines(token) >= 2
        ) and token.level == 0:
            line_spans.append(token.map)
        else:
            outside_tokens.append(token)
        token_index += 1

    score = _score_tokens(scoring_tokens)
    if environment.get("references"):
        return MarkdownAnalysis(score, score, (), has_long_list)
    outside_score = _score_tokens(outside_tokens)
    if not line_spans:
        return MarkdownAnalysis(score, outside_score, (), has_long_list)

    # Match MarkdownIt's newline normalization while retaining original characters.
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n?|\n", text))]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return MarkdownAnalysis(
        score,
        outside_score,
        tuple((offsets[start], offsets[end]) for start, end in line_spans),
        has_long_list,
    )
