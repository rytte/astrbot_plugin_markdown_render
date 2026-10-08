"""Share Markdown syntax between rendering and automatic detection."""

from collections.abc import Callable

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.tasklists import tasklists_plugin


def is_mermaid(token: Token) -> bool:
    """Identify a Mermaid fence without inspecting ordinary code contents."""
    return token.type == "fence" and token.info.strip().split(maxsplit=1)[:1] == [
        "mermaid"
    ]


def _bracket_inline(state: StateInline, silent: bool) -> bool:
    if not state.src.startswith(r"\(", state.pos):
        return False
    end = state.src.find(r"\)", state.pos + 2, state.posMax)
    if end < 0 or end == state.pos + 2:
        return False
    if not silent:
        token = state.push("math_inline", "math", 0)
        token.content = state.src[state.pos + 2 : end]
        token.markup = r"\("
    state.pos = end + 2
    return True


def _delimited_block(
    state: StateBlock,
    start_line: int,
    end_line: int,
    silent: bool,
    opening: str,
    closing: str,
) -> bool:
    if state.sCount[start_line] - state.blkIndent >= 4:
        return False
    start = state.bMarks[start_line] + state.tShift[start_line]
    if not state.src.startswith(opening, start):
        return False
    content = []
    for line in range(start_line, end_line):
        if line > start_line and state.sCount[line] < state.blkIndent:
            break
        if line > start_line and state.isEmpty(line):
            break
        begin = state.bMarks[line] + state.tShift[line]
        text = state.src[begin : state.eMarks[line]]
        if line == start_line:
            text = text[2:]
        end = text.find(closing)
        if end >= 0:
            if text[end + 2 :].strip():
                return False
            content.append(text[:end])
            if not silent:
                token = state.push("math_block", "math", 0)
                token.block = True
                token.content = "\n".join(content).strip()
                token.markup = opening
                token.map = [start_line, line + 1]
                state.line = line + 1
            return True
        content.append(text)
    return False


def _bracket_block(
    state: StateBlock, start_line: int, end_line: int, silent: bool
) -> bool:
    return _delimited_block(state, start_line, end_line, silent, r"\[", r"\]")


def _dollar_block(
    state: StateBlock, start_line: int, end_line: int, silent: bool
) -> bool:
    return _delimited_block(state, start_line, end_line, silent, "$$", "$$")


def create_parser(highlight: Callable | None = None) -> MarkdownIt:
    """Enable tables, tasks, dollar math and bracket math with HTML disabled."""
    parser = MarkdownIt("commonmark", {"html": False, "highlight": highlight}).enable(
        ["table", "strikethrough"]
    )
    parser.use(
        dollarmath_plugin,
        allow_labels=False,
        allow_space=False,
        allow_digits=False,
        allow_blank_lines=False,
        double_inline=True,
    ).use(tasklists_plugin, enabled=False)
    alternatives = {"alt": ["paragraph", "reference", "blockquote", "list"]}
    parser.block.ruler.at("math_block", _dollar_block, alternatives)
    parser.inline.ruler.before("escape", "bracket_math_inline", _bracket_inline)
    parser.block.ruler.before(
        "fence", "bracket_math_block", _bracket_block, alternatives
    )
    return parser
