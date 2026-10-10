import pytest
from astrbot_plugin_markdown_render.detector import analyze_markdown


@pytest.mark.parametrize(
    "text",
    [
        "今天有空一起吃饭吗？",
        "C# 开发，2 * 3 = 6，价格 #123，文件 foo_bar_baz.txt。",
        r"\# 这是转义符号，\*不会变成强调\*。",
        "记住 **这一点** 就够了。",
        "查看 [文档](https://example.com) 和 `example.py`。",
        "# 一个标题\n\n普通的说明。",
        "- 吃饭\n- 睡觉",
        "**甲** **乙** **丙** **丁** **戊** **己** **庚**",
        "```text\n# 只有一行，里面的 Markdown 不能再计分\n```",
        "`# 标题 > 引用 **强调**`",
        "| 管道字符 | 只是文字，没有表头分隔行 |",
        "![外部图片](https://example.com/image.png)",
    ],
    ids=[
        "prose",
        "punctuation",
        "escaped",
        "one-bold",
        "link-code",
        "one-heading",
        "short-list",
        "repeated-bold",
        "syntax-inside-fence",
        "syntax-inside-inline-code",
        "pipes",
        "external-image",
    ],
)
def test_plain_or_lightly_formatted_text_stays_below_default_threshold(text):
    assert analyze_markdown(text, 6).score < 6


@pytest.mark.parametrize(
    "text",
    [
        "| 时间 | 任务 |\n| --- | --- |\n| 上午 | 阅读 |",
        "```python\nx = 1\nprint(x)\n```",
        "    x = 1\n    print(x)\n",
        "# 操作步骤\n\n- **准备**环境\n- **安装**依赖\n- 运行\n",
        "# 一\n\n内容\n\n## 二\n\n内容\n\n## 三\n\n内容",
    ],
    ids=["table", "fenced-code", "indented-code", "mixed", "headings"],
)
def test_structured_replies_reach_default_threshold(text):
    assert analyze_markdown(text, 6).score >= 6


TABLE = "| 项目 | 内容 |\n| --- | --- |\n| **加粗** | [链接](https://example.com) |\n"
CODE = "```python\nx = 1\nprint(x)\n```\n"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_partial_ranges_preserve_original_source_and_block_order(newline):
    introduction = "普通介绍\u2028还是同一源代码行\n\n"
    middle = "\n这是中间说明。\n\n"
    suffix = "\n结束说明。"
    text = (introduction + TABLE + middle + CODE + suffix).replace("\n", newline)
    analysis = analyze_markdown(text, 6)
    assert analysis.score >= 6
    assert analysis.outside_score == 0
    assert [text[start:end] for start, end in analysis.block_spans] == [
        TABLE.replace("\n", newline),
        CODE.replace("\n", newline),
    ]
    (first_start, first_end), (second_start, second_end) = analysis.block_spans
    assert text[:first_start] == introduction.replace("\n", newline)
    assert text[first_end:second_start] == middle.replace("\n", newline)
    assert text[second_end:] == suffix.replace("\n", newline)


@pytest.mark.parametrize(
    ("outside", "expected_score"),
    [
        ("# 标题", 2),
        ("- 列表项", 1),
        ("> 引用", 2),
        ("**加粗**", 1),
        ("*斜体*", 1),
        ("~~删除~~", 1),
        ("`行内代码`", 1),
        ("[链接](https://example.com)", 1),
        ("![图片](https://example.com/a.png)", 0),
        ("---", 1),
        ("一行  \n强制换行", 0),
        ("```text\n单行代码\n```", 2),
        ("```text\n```", 0),
    ],
    ids=[
        "heading",
        "list",
        "quote",
        "strong",
        "emphasis",
        "strike",
        "inline-code",
        "link",
        "image",
        "rule",
        "hardbreak",
        "short-code",
        "empty-code",
    ],
)
def test_other_markup_is_scored_without_removing_independent_blocks(
    outside, expected_score
):
    text = outside + "\n\n" + TABLE
    analysis = analyze_markdown(text, 6)
    assert analysis.score >= 6
    assert analysis.outside_score == expected_score
    assert [text[start:end] for start, end in analysis.block_spans] == [TABLE]


def test_reference_links_keep_their_definitions_with_the_whole_reply():
    text = "[ref]: https://example.com\n\n" + TABLE.replace(
        "[链接](https://example.com)", "[链接][ref]"
    )
    assert analyze_markdown(text, 6).block_spans == ()


def test_outside_category_caps_are_independent_of_formatting_inside_tables():
    table = "| 项目 | 内容 |\n| --- | --- |\n| **甲** *乙* | `丙` [丁](https://example.com) |\n"
    text = "# 标题\n\n**甲** *乙* `丙` [丁](https://example.com)\n\n" + table
    analysis = analyze_markdown(text, 6)
    assert analysis.score == 12
    assert analysis.outside_score == 6
    assert [text[start:end] for start, end in analysis.block_spans] == [table]


def test_two_separators_around_multiline_code_count_as_one_outside_point():
    text = "普通介绍\n\n---\n\n" + CODE + "\n---\n\n普通结尾"
    analysis = analyze_markdown(text, 6)
    assert analysis.score == 7
    assert analysis.outside_score == 1
    assert [text[start:end] for start, end in analysis.block_spans] == [CODE]


@pytest.mark.parametrize(
    ("block", "expected_score"),
    [
        ("| 甲 | 乙 |\n| --- | --- |\n| 丙 | 丁 |\n", 6),
        (CODE, 6),
        ("```text\n一行\n```\n", 2),
        ("```text\n```\n", 0),
    ],
)
def test_table_and_code_weights_and_category_caps_are_unchanged(block, expected_score):
    assert analyze_markdown(block, 6).score == expected_score
    assert analyze_markdown((block + "\n") * 4, 6).score == min(expected_score * 4, 6)


def test_code_contents_are_not_treated_as_outside_markdown():
    block = "```markdown\n# 标题\n- **加粗**\n```"
    text = "说明：\n\n" + block + "\n\n结束。"
    analysis = analyze_markdown(text, 6)
    assert analysis.outside_score == 0
    assert [text[start:end] for start, end in analysis.block_spans] == [block + "\n"]


def test_indented_multiline_code_is_isolated_without_removing_indentation():
    block = "    x = 1\n    print(x)\n"
    text = "说明：\n\n" + block + "\n结束。"
    analysis = analyze_markdown(text, 6)
    assert [text[start:end] for start, end in analysis.block_spans] == [block]


def test_code_with_only_one_nonempty_line_is_not_a_partial_target():
    text = "```python\n\nx = 1\n\n```"
    assert analyze_markdown(text, 6).block_spans == ()


def test_nested_code_in_a_quote_keeps_its_surrounding_structure():
    analysis = analyze_markdown("> 说明\n>\n> ```python\n> x = 1\n> print(x)\n> ```", 6)
    assert analysis.score >= 6
    assert analysis.block_spans == ()


@pytest.mark.parametrize(
    "text",
    [
        "$x^2$",
        r"\(x^2\)",
        "$$x^2$$",
        "$$\nx^2\n$$",
        r"\[x^2\]",
        "\\[\nx^2\n\\]",
        "公式 $$x^2$$。",
        "```mermaid\nflowchart LR; A-->B\n```",
        "```mermaid\nflowchart LR\nA-->B\n```",
    ],
)
def test_math_and_mermaid_each_score_six_points(text):
    assert analyze_markdown(text, 6).score == 6


def test_math_mermaid_and_code_have_independent_six_point_caps():
    formula = "$x$\n\n$$x^2$$\n\n"
    diagram = "```mermaid\nflowchart LR; A-->B\n```\n\n"
    text = formula * 4 + diagram * 4 + (CODE + "\n") * 4 + TABLE
    assert analyze_markdown(text, 6).score == 26


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("formula", ["$$\nx^2\n$$\n", "\\[\nx^2\n\\]\n"])
def test_new_partial_blocks_preserve_source_ranges_and_order(newline, formula):
    diagram = "```mermaid\nflowchart LR; A-->B\n```\n"
    blocks = [formula, diagram, TABLE, CODE]
    text = ("介绍\n\n" + "\n说明\n\n".join(blocks) + "\n结尾").replace("\n", newline)
    analysis = analyze_markdown(text, 6)
    assert analysis.score == 26
    assert analysis.outside_score == 0
    assert [text[start:end] for start, end in analysis.block_spans] == [
        block.replace("\n", newline) for block in blocks
    ]


def test_inline_math_counts_outside_without_splitting_the_sentence():
    text = "正文里的 $x^2$ 不单独拆开。\n\n" + TABLE
    analysis = analyze_markdown(text, 6)
    assert analysis.score == 14
    assert analysis.outside_score == 6
    assert [text[start:end] for start, end in analysis.block_spans] == [TABLE]


@pytest.mark.parametrize(
    "text",
    [
        "价格 $5 和 $10。",
        r"\$x\$",
        "`$x$`",
        "```text\n$x$\n```",
        "$$\n$$",
        "```mermaid\n```",
    ],
)
def test_currency_escapes_code_and_empty_blocks_are_not_six_point_math(text):
    assert analyze_markdown(text, 6).score < 6


@pytest.mark.parametrize("markup", ["$$", r"\["])
def test_nested_math_keeps_quote_and_list_structure(markup):
    closing = "$$" if markup == "$$" else r"\]"
    quote = f"> {markup}\n> x^2\n> {closing}"
    analysis = analyze_markdown(quote, 6)
    assert analysis.score == 8
    assert analysis.block_spans == ()


@pytest.mark.parametrize("formula", ["$$\nx^2\n$$", "\\[\nx^2\n\\]"])
def test_block_math_interrupts_a_paragraph_without_requiring_blank_lines(formula):
    text = "公式说明：\n" + formula + "\n结束说明。"
    analysis = analyze_markdown(text, 6)
    assert analysis.score == 6
    assert analysis.outside_score == 0
    assert [text[start:end] for start, end in analysis.block_spans] == [formula + "\n"]


@pytest.mark.parametrize("marker", ["- ", "3. ", "- [ ] ", "- [x] "])
@pytest.mark.parametrize("threshold", [1, 6, 8, 30])
@pytest.mark.parametrize("extra_items", [-1, 0, 5])
def test_lists_use_item_threshold_and_exclude_qualifying_items_from_scores(
    marker, threshold, extra_items
):
    item_count = threshold + extra_items
    text = "\n".join(f"{marker}项目{index}" for index in range(item_count))
    analysis = analyze_markdown(text, threshold)
    qualifies = item_count >= threshold
    assert analysis.has_long_list is qualifies
    assert (
        analysis.score
        == analysis.outside_score
        == (0 if qualifies else min(item_count, 6))
    )
    assert [text[start:end] for start, end in analysis.block_spans] == (
        [text] if qualifies else []
    )


def test_all_formatting_inside_qualifying_lists_is_excluded_from_scores():
    block = (
        "- # 内部标题\n\n"
        "  **加粗** *斜体* ~~删除~~ `代码` [链接](https://example.com) $x$\n\n"
        "  > 引用\n\n"
        "  ```python\n  first = 1\n  print(first)\n  ```\n\n"
        "  $$\n  x^2\n  $$\n\n"
        "  ```mermaid\n  flowchart LR; A-->B\n  ```\n\n"
        "  | 项目 | 内容 |\n  | --- | --- |\n  | 甲 | 乙 |\n\n"
        "  ---\n\n"
        "  - 子列表项\n"
        "- 第二项\n\n"
    )
    text = "# 外部标题\n\n" + block + "**外部强调**。"
    analysis = analyze_markdown(text, 2)
    assert analysis.has_long_list
    assert analysis.score == analysis.outside_score == 3
    assert [text[start:end] for start, end in analysis.block_spans] == [block]


def test_empty_lines_and_wrapped_content_do_not_increase_list_item_count():
    text = "- 第一项\n  续行一\n  续行二\n\n  第二段\n\n- 第二项\n  续行三\n"
    analysis = analyze_markdown(text, 3)
    assert not analysis.has_long_list
    assert analysis.score == analysis.outside_score == 2
    assert analysis.block_spans == ()


@pytest.mark.parametrize(
    ("parent_count", "child_count", "qualifies"),
    [(1, 10, False), (5, 10, False), (6, 10, True)],
)
def test_nested_items_do_not_increase_parent_list_length(
    parent_count, child_count, qualifies
):
    text = "- 父项一\n" + "  - 子项\n" * child_count + "- 父项\n" * (parent_count - 1)
    analysis = analyze_markdown(text, 6)
    assert analysis.has_long_list is qualifies
    assert analysis.score == analysis.outside_score == (0 if qualifies else 6)
    assert [text[start:end] for start, end in analysis.block_spans] == (
        [text] if qualifies else []
    )


def test_long_lists_inside_quotes_are_not_extracted():
    text = "> - 项目\n" * 8
    analysis = analyze_markdown(text, 6)
    assert not analysis.has_long_list
    assert analysis.score == analysis.outside_score == 8
    assert analysis.block_spans == ()


def test_separate_short_lists_do_not_merge_into_one_independent_list():
    text = "- 项目\n" * 3 + "\n中间说明\n\n" + "- 项目\n" * 3
    analysis = analyze_markdown(text, 6)
    assert not analysis.has_long_list
    assert analysis.score == analysis.outside_score == 6
    assert analysis.block_spans == ()


def test_short_list_formatting_still_scores_next_to_qualifying_list():
    block = "- 项目\n" * 6 + "\n"
    text = block + "说明\n\n- **第一项**\n- $x$"
    analysis = analyze_markdown(text, 6)
    assert analysis.has_long_list
    assert analysis.score == analysis.outside_score == 9
    assert [text[start:end] for start, end in analysis.block_spans] == [block]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_list_ranges_preserve_newlines_and_independent_block_order(newline):
    listing = "- [ ] 项目\n" * 6 + "\n"
    formula = "$$\nx^2\n$$\n"
    diagram = "```mermaid\nflowchart LR; A-->B\n```\n"
    blocks = [listing, TABLE, CODE, formula, diagram]
    text = (
        "介绍\n\n" + listing + "说明\n\n" + "\n说明\n\n".join(blocks[1:]) + "\n结尾"
    ).replace("\n", newline)
    analysis = analyze_markdown(text, 6)
    assert analysis.has_long_list
    assert analysis.score == 26
    assert analysis.outside_score == 0
    assert [text[start:end] for start, end in analysis.block_spans] == [
        block.replace("\n", newline) for block in blocks
    ]


def test_reference_definitions_prevent_splitting_but_keep_list_trigger():
    text = "[ref]: https://example.com\n\n" + "- [项目][ref]\n" * 8
    analysis = analyze_markdown(text, 8)
    assert analysis.has_long_list
    assert analysis.score == analysis.outside_score == 0
    assert analysis.block_spans == ()
