import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from astrbot.api.message_components import Image
from astrbot.api.star import Context
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.updater import _PluginUpdater
from astrbot_plugin_markdown_render.main import MarkdownImagePlugin, MarkdownImageTool
from astrbot_plugin_markdown_render.renderer import RenderError


@pytest.fixture
def plugin():
    context = object.__new__(Context)
    context.provider_manager = SimpleNamespace(llm_tools=FunctionToolManager())
    instance = MarkdownImagePlugin(context, {})
    instance.renderer = SimpleNamespace(
        render=AsyncMock(return_value=b"image bytes"),
        initialize=AsyncMock(),
        close=AsyncMock(),
    )
    return instance


async def test_real_astrbot_metadata_and_tool_schema(plugin):
    metadata = _PluginUpdater.inspect_plugin_directory(Path(__file__).parents[1])
    assert metadata["metadata"]["name"] == "astrbot_plugin_markdown_render"
    await plugin.initialize()
    tool = plugin.context.get_llm_tool_manager().get_func("render_markdown_image")
    assert tool is not None
    assert tool.parameters["properties"]["markdown"]["type"] == "string"
    assert tool.parameters["required"] == ["markdown"]
    assert list(tool.parameters["properties"]) == ["markdown"]
    assert tool.handler_module_path == MarkdownImagePlugin.__module__
    schema = ToolSet([tool]).openai_schema()[0]["function"]
    assert schema["parameters"]["required"] == ["markdown"]
    event = SimpleNamespace(send=AsyncMock())
    assert json.loads(await tool.handler(event, markdown="# 正文"))["ok"]


async def test_sends_image_to_current_event_once(plugin):
    event = SimpleNamespace(send=AsyncMock())
    result = json.loads(await plugin.render_markdown_image(event, "# 标题"))
    assert result["ok"] and result["sent_images"] == 1
    plugin.renderer.render.assert_awaited_once_with("# 标题")
    event.send.assert_awaited_once()
    message = event.send.call_args.args[0]
    assert message.type == "tool_direct_result"
    assert len(message.chain) == 1
    assert isinstance(message.chain[0], Image)
    assert message.chain[0].file == "base64://aW1hZ2UgYnl0ZXM="


@pytest.mark.parametrize("error", [RenderError("内容过长"), RuntimeError("details")])
async def test_render_failure_sends_nothing(plugin, error):
    plugin.renderer.render.side_effect = error
    event = SimpleNamespace(send=AsyncMock())
    result = json.loads(await plugin.render_markdown_image(event, "正文"))
    assert not result["ok"] and result["stage"] == "render"
    event.send.assert_not_awaited()


async def test_send_failure_is_not_reported_as_success_or_retried(plugin):
    event = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("failed")))
    result = json.loads(await plugin.render_markdown_image(event, "正文"))
    assert not result["ok"] and result["stage"] == "send"
    assert "勿自动重试" in result["error"]
    event.send.assert_awaited_once()


async def test_cancellation_propagates_without_sending(plugin):
    plugin.renderer.render.side_effect = asyncio.CancelledError
    event = SimpleNamespace(send=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await plugin.render_markdown_image(event, "正文")
    event.send.assert_not_awaited()


async def test_plugin_lifecycle(plugin):
    await plugin.initialize()
    plugin.renderer.initialize.assert_awaited_once()
    await plugin.terminate()
    plugin.renderer.close.assert_awaited_once()


def test_required_argument_rejected_by_schema():
    from jsonschema import ValidationError, validate

    tool = MarkdownImageTool(AsyncMock())
    with pytest.raises(ValidationError):
        validate({}, tool.parameters)
    with pytest.raises(ValidationError):
        validate({"markdown": ""}, tool.parameters)
    with pytest.raises(ValidationError):
        validate({"markdown": "正文", "session": "another-chat"}, tool.parameters)
    with pytest.raises(ValidationError):
        validate(
            {"markdown": "正文", "browser_executable": "/model/chosen/browser"},
            tool.parameters,
        )


def test_unknown_plugin_config_fails_with_clear_error():
    with pytest.raises(RenderError, match="未知插件配置"):
        MarkdownImagePlugin(SimpleNamespace(), {"legacy_width": 900})


async def test_real_render_to_astrbot_image():
    instance = MarkdownImagePlugin(SimpleNamespace(add_llm_tools=lambda tool: None), {})
    event = SimpleNamespace(send=AsyncMock())
    try:
        await instance.initialize()
        result = json.loads(await instance.render_markdown_image(event, "# 中文图片"))
        assert result["ok"]
        image = event.send.call_args.args[0].chain[0]
        base64_image = await image.convert_to_base64()
        assert base64_image.startswith("iVBORw0KGgo")
    finally:
        await instance.terminate()


async def test_dashboard_save_reloads_shared_browser_config(
    plugin, tmp_path, monkeypatch
):
    from astrbot.api import AstrBotConfig
    from astrbot.dashboard.services.config_service import ConfigFileService
    from astrbot_plugin_markdown_render.renderer import MarkdownRenderer

    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    config_path = str(tmp_path / "config.json")
    config = AstrBotConfig(config_path, schema=schema)
    metadata = SimpleNamespace(config=config)
    current = MarkdownImagePlugin(plugin.context, config)
    previous = current

    async def reload_plugin(name):
        nonlocal current
        await current.terminate()
        current = MarkdownImagePlugin(
            plugin.context, AstrBotConfig(config_path, schema=schema)
        )
        await current.initialize()
        return True, None

    manager = SimpleNamespace(reload=AsyncMock(side_effect=reload_plugin))
    service = ConfigFileService(SimpleNamespace(plugin_manager=manager))
    monkeypatch.setattr(service, "get_plugin_metadata_by_name", lambda name: metadata)
    edge_path = tmp_path / "Program Files" / "Edge" / "msedge.exe"
    edge_path.parent.mkdir(parents=True)
    edge_path.touch()
    with (
        patch.object(MarkdownRenderer, "initialize", new_callable=AsyncMock),
        patch.object(MarkdownRenderer, "close", new_callable=AsyncMock) as close,
    ):
        await current.initialize()
        await service.save_plugin_configs(
            dict(config, browser_executable=str(edge_path)),
            "astrbot_plugin_markdown_render",
        )
        manager.reload.assert_awaited_once_with("astrbot_plugin_markdown_render")
        close.assert_awaited_once()
        assert current is not previous
        assert current.renderer.browser_executable == str(edge_path)
        tool = plugin.context.get_llm_tool_manager().get_func("render_markdown_image")
        assert tool.handler.__self__ is current
        current.renderer.render = AsyncMock(return_value=b"image bytes")
        for _ in range(2):
            event = SimpleNamespace(send=AsyncMock())
            result = json.loads(await tool.handler(event, markdown="正文"))
            assert result["ok"]
        assert current.renderer.render.await_count == 2
        await current.terminate()
