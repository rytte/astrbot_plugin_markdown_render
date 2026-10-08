import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from astrbot.api.event import MessageEventResult, ResultContentType
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot_plugin_markdown_render.main import MarkdownImagePlugin
from astrbot_plugin_markdown_render.renderer import MAX_CHARACTERS, RenderError

TABLE = "| 时间 | 任务 |\n| --- | --- |\n| 上午 | 阅读 |"


@pytest.fixture
def plugin():
    context = object.__new__(Context)
    context.provider_manager = SimpleNamespace(llm_tools=FunctionToolManager())
    instance = MarkdownImagePlugin(context, {})
    instance.renderer = SimpleNamespace(
        render=AsyncMock(return_value=b"png bytes"),
        initialize=AsyncMock(),
        close=AsyncMock(),
    )
    return instance


def make_event(chain=None, content_type=ResultContentType.LLM_RESULT):
    result = MessageEventResult(
        chain=[Plain(TABLE)] if chain is None else chain,
        result_content_type=content_type,
    )
    extras = {}
    return SimpleNamespace(
        result=result,
        get_result=lambda: result,
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.update({key: value}),
        send=AsyncMock(),
    )


async def test_auto_only_does_not_expose_or_run_model_tool(plugin):
    await plugin.initialize()
    assert not plugin.context.get_llm_tool_manager().func_list
    event = make_event()
    receipt = json.loads(await plugin.render_markdown_image(event, TABLE))
    assert receipt["stage"] == "disabled" and not receipt["ok"]
    plugin.renderer.render.assert_not_awaited()
    event.send.assert_not_awaited()


async def test_only_auto_render_disables_streaming(plugin):
    event = make_event()
    event.set_extra("enable_streaming", True)
    await plugin.prepare_auto_reply(event)
    assert event.get_extra("enable_streaming") is False
    plugin.enable_auto_render = False
    event.set_extra("enable_streaming", True)
    await plugin.prepare_auto_reply(event)
    assert event.get_extra("enable_streaming") is True
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_not_awaited()
    assert event.result.use_t2i_ is None


@pytest.mark.parametrize("enable_tool", [False, True])
@pytest.mark.parametrize("enable_auto_render", [False, True])
async def test_features_are_independent(plugin, enable_tool, enable_auto_render):
    instance = MarkdownImagePlugin(
        plugin.context,
        {"enable_tool": enable_tool, "enable_auto_render": enable_auto_render},
    )
    instance.renderer = plugin.renderer
    await instance.initialize()
    tools = instance.context.get_llm_tool_manager().func_list
    assert bool(tools) is enable_tool
    event = make_event()
    await instance.prepare_auto_reply(event)
    assert event.get_extra("enable_streaming") is (
        False if enable_auto_render else None
    )
    await instance.auto_render_reply(event)
    if enable_auto_render:
        instance.renderer.render.assert_awaited_once_with(TABLE)
        assert isinstance(event.result.chain[0], Image)
    else:
        instance.renderer.render.assert_not_awaited()
        assert event.result.chain[0].text == TABLE
    event.send.assert_not_awaited()

    instance.renderer.render.reset_mock()
    tool_event = make_event()
    receipt = json.loads(await instance.render_markdown_image(tool_event, TABLE))
    assert receipt["ok"] is enable_tool
    assert tool_event.get_extra("markdown_render_tool_sent", False) is enable_tool
    if enable_tool:
        instance.renderer.render.assert_awaited_once_with(TABLE)
        tool_event.send.assert_awaited_once()
    else:
        instance.renderer.render.assert_not_awaited()
        tool_event.send.assert_not_awaited()


@pytest.mark.parametrize("reply", [TABLE, "# 补充说明\n\n" + TABLE])
async def test_successful_tool_skips_auto_only_for_its_own_event(plugin, reply):
    plugin.enable_tool = True
    event = make_event([Plain(reply)])
    original_chain = event.result.chain
    event.result.use_t2i_ = True
    receipt = json.loads(await plugin.render_markdown_image(event, TABLE))
    assert receipt["ok"]
    assert event.get_extra("markdown_render_tool_sent") is True

    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once_with(TABLE)
    event.send.assert_awaited_once()
    assert event.result.chain is original_chain
    assert event.result.chain[0].text == reply
    assert event.result.use_t2i_ is False

    next_event = make_event()
    await plugin.auto_render_reply(next_event)
    assert plugin.renderer.render.await_args_list == [call(TABLE), call(TABLE)]
    assert isinstance(next_event.result.chain[0], Image)
    assert next_event.get_extra("markdown_render_tool_sent", False) is False
    next_event.send.assert_not_awaited()


@pytest.mark.parametrize("stage", ["render", "send"])
async def test_failed_tool_does_not_suppress_auto_render(plugin, stage):
    plugin.enable_tool = True
    event = make_event()
    if stage == "render":
        plugin.renderer.render.side_effect = RenderError("render failed")
    else:
        event.send.side_effect = RuntimeError("send failed")
    receipt = json.loads(await plugin.render_markdown_image(event, TABLE))
    assert not receipt["ok"] and receipt["stage"] == stage
    assert event.get_extra("markdown_render_tool_sent", False) is False

    plugin.renderer.render.side_effect = None
    await plugin.auto_render_reply(event)
    assert isinstance(event.result.chain[0], Image)
    assert plugin.renderer.render.await_count == 2
    assert event.send.await_count == (1 if stage == "send" else 0)


async def test_auto_render_does_not_block_explicit_tool_calls_in_the_same_turn(plugin):
    plugin.enable_tool = True
    event = make_event()
    for markdown in ("# 第一部分", "# 第二部分"):
        receipt = json.loads(await plugin.render_markdown_image(event, markdown))
        assert receipt["ok"]
    await plugin.auto_render_reply(event)
    assert plugin.renderer.render.await_args_list == [
        call("# 第一部分"),
        call("# 第二部分"),
    ]
    assert event.send.await_count == 2
    assert event.result.chain[0].text == TABLE


async def test_auto_replaces_text_once_preserving_mention_and_quote(plugin):
    quote = Reply(id="message-id")
    mention = At(qq="123")
    event = make_event([quote, mention, Plain(TABLE[:12]), Plain(TABLE[12:])])
    provider_chain = event.result.chain
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once_with(TABLE)
    assert event.result.chain[:2] == [quote, mention]
    assert len(event.result.chain) == 3
    assert isinstance(event.result.chain[2], Image)
    assert len(provider_chain) == 4
    assert provider_chain[2].text + provider_chain[3].text == TABLE
    assert event.result.use_t2i_ is False
    event.send.assert_not_awaited()
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once()
    event.send.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [RenderError("图片过长"), RuntimeError("browser failed")]
)
async def test_auto_failure_preserves_original_text_and_blocks_online_t2i(
    plugin, error
):
    event = make_event()
    original_chain = event.result.chain
    original_text = original_chain[0]
    event.result.use_t2i_ = True
    plugin.renderer.render.side_effect = error
    await plugin.auto_render_reply(event)
    assert event.result.chain is original_chain
    assert event.result.chain[0] is original_text
    assert original_text.text == TABLE
    assert event.result.use_t2i_ is False
    event.send.assert_not_awaited()


async def test_auto_cancellation_propagates_without_consuming_reply(plugin):
    event = make_event()
    plugin.renderer.render.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await plugin.auto_render_reply(event)
    assert event.result.chain[0].text == TABLE
    event.send.assert_not_awaited()


@pytest.mark.parametrize(
    "kind",
    [
        ResultContentType.GENERAL_RESULT,
        ResultContentType.STREAMING_RESULT,
        ResultContentType.STREAMING_FINISH,
        ResultContentType.AGENT_RUNNER_ERROR,
    ],
)
async def test_non_model_and_streaming_results_are_untouched(plugin, kind):
    event = make_event(content_type=kind)
    await plugin.auto_render_reply(event)
    assert event.result.chain[0].text == TABLE
    assert event.result.use_t2i_ is None
    plugin.renderer.render.assert_not_awaited()


@pytest.mark.parametrize(
    "chain",
    [
        [],
        [Plain(" ")],
        [Plain("只是 **重点**")],
        [Plain("字" * (MAX_CHARACTERS + 1))],
        [Plain(TABLE), Image.fromBytes(b"existing")],
        [Plain(TABLE), At(qq="123"), Plain("另一段")],
    ],
    ids=[
        "empty",
        "whitespace",
        "low-score",
        "oversized",
        "existing-image",
        "inline-mention",
    ],
)
async def test_unsuitable_replies_are_not_changed(plugin, chain):
    event = make_event(chain)
    await plugin.auto_render_reply(event)
    assert event.result.chain == chain
    plugin.renderer.render.assert_not_awaited()


async def test_threshold_is_configurable(plugin):
    plugin.auto_threshold = 8
    event = make_event()
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_not_awaited()
    plugin.auto_threshold = 6
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once_with(TABLE)


@pytest.mark.parametrize(
    "config",
    [
        {"auto_threshold": 0},
        {"auto_threshold": 31},
        {"auto_threshold": True},
        {"auto_threshold": "6"},
    ],
)
def test_invalid_threshold_fails_fast(config):
    with pytest.raises(RenderError, match="auto_threshold"):
        MarkdownImagePlugin(SimpleNamespace(), config)


@pytest.mark.parametrize("setting", ["enable_tool", "enable_auto_render"])
@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_invalid_feature_flag_fails_fast(setting, value):
    with pytest.raises(RenderError, match=setting):
        MarkdownImagePlugin(SimpleNamespace(), {setting: value})


async def test_partial_render_preserves_prose_and_multiple_blocks_in_order(plugin):
    introduction = "这是说明。\n\n"
    table = TABLE + "\n"
    middle = "\n代码示例：\n\n"
    code = "```python\nx = 1\nprint(x)\n```\n"
    ending = "\n以上是全部内容。"
    markdown = introduction + table + middle + code + ending
    event = make_event([Plain(markdown)])
    original = event.result.chain
    await plugin.auto_render_reply(event)
    assert plugin.renderer.render.await_args_list == [call(table), call(code)]
    assert [type(part) for part in event.result.chain] == [
        Plain,
        Image,
        Plain,
        Image,
        Plain,
    ]
    assert [part.text for part in event.result.chain if isinstance(part, Plain)] == [
        introduction,
        middle,
        ending,
    ]
    assert original[0].text == markdown
    event.send.assert_not_awaited()


async def test_partial_images_without_prose_do_not_emit_whitespace_messages(plugin):
    table = TABLE + "\n"
    code = "```python\nx = 1\nprint(x)\n```\n"
    event = make_event([Plain("\n\n" + table + "\n" + code + "\n\n")])
    await plugin.auto_render_reply(event)
    assert [type(part) for part in event.result.chain] == [Image, Image]


async def test_light_outside_markup_stays_as_original_text(plugin):
    markdown = "# 标题\n\n" + TABLE + "\n\n**说明**。"
    event = make_event([Plain(markdown)])
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once_with(TABLE + "\n")
    assert [type(part) for part in event.result.chain] == [Plain, Image, Plain]
    assert event.result.chain[0].text == "# 标题\n\n"
    assert event.result.chain[2].text == "\n**说明**。"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_separators_around_code_are_preserved_outside_image(plugin, newline):
    prefix = "普通介绍\n\n---\n\n".replace("\n", newline)
    code = "```cpp\nint main() {\n    return 0;\n}\n```\n".replace("\n", newline)
    suffix = "\n---\n\n普通结尾".replace("\n", newline)
    event = make_event([Plain(prefix + code + suffix)])
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once_with(code)
    assert [type(part) for part in event.result.chain] == [Plain, Image, Plain]
    assert event.result.chain[0].text == prefix
    assert event.result.chain[2].text == suffix


@pytest.mark.parametrize(
    ("outside_score", "threshold", "whole_reply"),
    [(5, 6, False), (6, 6, True), (7, 6, True), (6, 8, False), (5, 5, True)],
)
async def test_outside_score_selects_partial_or_whole_at_configured_threshold(
    plugin, outside_score, threshold, whole_reply
):
    # One heading (2 points) plus enough list items to reach the requested score.
    prefix = "# 标题\n\n" + "- 项目\n" * (outside_score - 2) + "\n"
    table = TABLE.replace("阅读", "**阅读**和[链接](https://example.com)")
    markdown = prefix + table
    plugin.auto_threshold = threshold
    event = make_event([Plain(markdown)])
    await plugin.auto_render_reply(event)
    if whole_reply:
        plugin.renderer.render.assert_awaited_once_with(markdown)
        assert [type(part) for part in event.result.chain] == [Image]
    else:
        plugin.renderer.render.assert_awaited_once_with(table)
        assert [type(part) for part in event.result.chain] == [Plain, Image]
        assert event.result.chain[0].text == prefix


async def test_without_independent_blocks_total_score_still_triggers_whole_reply(
    plugin,
):
    markdown = "# 标题一\n\n正文\n\n## 标题二\n\n正文\n\n## 标题三"
    event = make_event([Plain(markdown)])
    await plugin.auto_render_reply(event)
    plugin.renderer.render.assert_awaited_once_with(markdown)
    assert [type(part) for part in event.result.chain] == [Image]


async def test_partial_failure_discards_completed_images_and_retains_entire_reply(
    plugin,
):
    markdown = (
        "**说明**\n\n---\n\n" + TABLE + "\n\n```text\n第一行\n第二行\n```\n\n结束"
    )
    event = make_event([Plain(markdown)])
    original = event.result.chain
    plugin.renderer.render.side_effect = [b"first image", RenderError("second failed")]
    await plugin.auto_render_reply(event)
    assert plugin.renderer.render.await_count == 2
    assert event.result.chain is original
    assert event.result.chain[0].text == markdown
    event.send.assert_not_awaited()


async def test_partial_render_has_one_total_timeout(plugin, monkeypatch):
    from astrbot_plugin_markdown_render import main

    monkeypatch.setattr(main, "RENDER_TIMEOUT", 0.1)
    markdown = TABLE + "\n\n```text\n第一行\n第二行\n```"
    event = make_event([Plain(markdown)])
    original = event.result.chain

    async def render(text):
        if text.startswith("```"):
            await asyncio.sleep(60)
        return b"first image"

    plugin.renderer.render.side_effect = render
    await plugin.auto_render_reply(event)
    assert event.result.chain is original
    assert event.result.chain[0].text == markdown
    event.send.assert_not_awaited()
