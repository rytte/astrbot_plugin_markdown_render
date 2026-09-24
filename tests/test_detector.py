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
    assert analyze_markdown(text).score < 6


@pytest.mark.parametrize(
    "text",
    [
        "| 时间 | 任务 |\n| --- | --- |\n| 上午 | 阅读 |",
        "```python\nx = 1\nprint(x)\n```",
        "    x = 1\n    print(x)\n",
        "# 操作步骤\n\n- **准备**环境\n- **安装**依赖\n- 运行\n",
        "# 一\n\n内容\n\n## 二\n\n内容\n\n## 三\n\n内容",
        "\n".join(f"{i}. 步骤{i}" for i in range(1, 7)),
    ],
    ids=["table", "fenced-code", "indented-code", "mixed", "headings", "long-list"],
)
def test_structured_replies_reach_default_threshold(text):
    assert analyze_markdown(text).score >= 6


TABLE = "| 项目 | 内容 |\n| --- | --- |\n| **加粗** | [链接](https://example.com) |\n"
CODE = "```python\nx = 1\nprint(x)\n```\n"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_partial_ranges_preserve_original_source_and_block_order(newline):
    introduction = "普通介绍\u2028还是同一源代码行\n\n"
    middle = "\n这是中间说明。\n\n"
    suffix = "\n结束说明。"
    text = (introduction + TABLE + middle + CODE + suffix).replace("\n", newline)
    analysis = analyze_markdown(text)
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
    analysis = analyze_markdown(text)
    assert analysis.score >= 6
    assert analysis.outside_score == expected_score
    assert [text[start:end] for start, end in analysis.block_spans] == [TABLE]


def test_reference_links_keep_their_definitions_with_the_whole_reply():
    text = "[ref]: https://example.com\n\n" + TABLE.replace(
        "[链接](https://example.com)", "[链接][ref]"
    )
    assert analyze_markdown(text).block_spans == ()


def test_outside_category_caps_are_independent_of_formatting_inside_tables():
    table = "| 项目 | 内容 |\n| --- | --- |\n| **甲** *乙* | `丙` [丁](https://example.com) |\n"
    text = "# 标题\n\n**甲** *乙* `丙` [丁](https://example.com)\n\n" + table
    analysis = analyze_markdown(text)
    assert analysis.score == 12
    assert analysis.outside_score == 6
    assert [text[start:end] for start, end in analysis.block_spans] == [table]


def test_two_separators_around_multiline_code_count_as_one_outside_point():
    text = "普通介绍\n\n---\n\n" + CODE + "\n---\n\n普通结尾"
    analysis = analyze_markdown(text)
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
    assert analyze_markdown(block).score == expected_score
    assert analyze_markdown((block + "\n") * 4).score == min(expected_score * 4, 6)


def test_code_contents_are_not_treated_as_outside_markdown():
    block = "```markdown\n# 标题\n- **加粗**\n```"
    text = "说明：\n\n" + block + "\n\n结束。"
    analysis = analyze_markdown(text)
    assert analysis.outside_score == 0
    assert [text[start:end] for start, end in analysis.block_spans] == [block + "\n"]


def test_indented_multiline_code_is_isolated_without_removing_indentation():
    block = "    x = 1\n    print(x)\n"
    text = "说明：\n\n" + block + "\n结束。"
    analysis = analyze_markdown(text)
    assert [text[start:end] for start, end in analysis.block_spans] == [block]


def test_code_with_only_one_nonempty_line_is_not_a_partial_target():
    text = "```python\n\nx = 1\n\n```"
    assert analyze_markdown(text).block_spans == ()


def test_nested_code_in_a_quote_keeps_its_surrounding_structure():
    analysis = analyze_markdown("> 说明\n>\n> ```python\n> x = 1\n> print(x)\n> ```")
    assert analysis.score >= 6
    assert analysis.block_spans == ()
