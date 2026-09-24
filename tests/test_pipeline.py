from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.event import AstrMessageEvent, MessageEventResult, ResultContentType
from astrbot.api.message_components import Image, Plain
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate import stage as decoration
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.star_handler import EventType
from astrbot_plugin_markdown_render.main import MarkdownImagePlugin
from astrbot_plugin_markdown_render.renderer import RenderError


@pytest.mark.parametrize("render_fails", [False, True])
@pytest.mark.parametrize(
    ("before", "block", "after"),
    [
        (
            "",
            "| 标题 | 内容 |\n| --- | --- |\n| 表格 | 数据 |\n",
            "\n" + "完整正文。" * 30,
        ),
        (
            "普通介绍\n\n---\n\n",
            "```cpp\nint main() {\n    return 0;\n}\n```\n",
            "\n---\n\n普通结尾",
        ),
    ],
    ids=["table", "code-with-separators"],
)
async def test_real_pipeline_sends_image_or_original_text_once(
    render_fails, before, block, after, monkeypatch
):
    config = deepcopy(DEFAULT_CONFIG)
    config["t2i"] = True
    config["t2i_word_threshold"] = 50
    config["platform_settings"]["segmented_reply"]["enable"] = False
    config["platform_settings"]["reply_with_quote"] = False
    config["platform_settings"]["reply_with_mention"] = False
    config["platform_settings"]["reply_prefix"] = ""
    config["content_safety"]["also_use_in_response"] = False
    config["provider_tts_settings"]["enable"] = False
    context = SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=None))
    pipeline = PipelineContext(config, SimpleNamespace(context=context), "test")
    plugin = MarkdownImagePlugin(context, {"mode": "auto"})
    plugin.renderer.render = AsyncMock(
        return_value=b"image bytes",
        side_effect=RenderError("browser unavailable") if render_fails else None,
    )
    hook = SimpleNamespace(
        handler=plugin.auto_render_reply,
        handler_module_path=plugin.__module__,
        handler_name="auto_render_reply",
    )
    monkeypatch.setattr(
        decoration.star_handlers_registry,
        "get_handlers_by_event_type",
        lambda event_type, **kwargs: (
            [hook] if event_type == EventType.OnDecoratingResultEvent else []
        ),
    )
    online_render = AsyncMock(side_effect=AssertionError("Online rendering forbidden"))
    monkeypatch.setattr(decoration.html_renderer, "render_t2i", online_render)
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.message = [Plain("测试请求")]
    message.message_str = "测试请求"
    message.sender = MessageMember("test-user", "Test")
    message.self_id = "bot"
    message.message_id = "message-1"
    event = AstrMessageEvent(
        "测试请求", message, PlatformMetadata("webchat", "Test", "test"), "session-1"
    )
    event.send = AsyncMock()
    markdown = before + block + after
    event.set_result(
        MessageEventResult(
            chain=[Plain(markdown)], result_content_type=ResultContentType.LLM_RESULT
        )
    )
    decorator = decoration.ResultDecorateStage()
    await decorator.initialize(pipeline)
    async for _ in decorator.process(event):
        pass
    event.send.assert_not_awaited()
    responder = RespondStage()
    await responder.initialize(pipeline)
    await responder.process(event)
    event.send.assert_awaited_once()
    online_render.assert_not_awaited()
    plugin.renderer.render.assert_awaited_once_with(block)
    delivered = event.send.call_args.args[0].chain
    if render_fails:
        assert len(delivered) == 1
        assert isinstance(delivered[0], Plain)
        assert delivered[0].text == markdown
    else:
        expected_types = [Plain, Image, Plain] if before else [Image, Plain]
        assert [type(part) for part in delivered] == expected_types
        assert [part.text for part in delivered if isinstance(part, Plain)] == (
            [before, after] if before else [after]
        )
