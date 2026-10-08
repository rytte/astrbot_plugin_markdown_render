import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot_plugin_markdown_render.main import (
    DEFAULT_TOOL_PROMPT,
    MarkdownImagePlugin,
    MarkdownImageTool,
)
from astrbot_plugin_markdown_render.renderer import RenderError


@pytest.fixture
def plugin():
    context = object.__new__(Context)
    context.provider_manager = SimpleNamespace(llm_tools=FunctionToolManager())
    instance = MarkdownImagePlugin(
        context,
        {
            "enable_tool": True,
            "enable_tool_prompt": True,
            "tool_prompt": "自定义工具使用偏好",
            "enable_auto_render": False,
        },
    )
    instance.renderer = SimpleNamespace(
        render=AsyncMock(), initialize=AsyncMock(), close=AsyncMock()
    )
    return instance


def test_tool_prompt_defaults_match_schema_and_are_disabled(plugin):
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    instance = MarkdownImagePlugin(plugin.context, {})
    assert (
        instance.enable_tool_prompt is schema["enable_tool_prompt"]["default"] is False
    )
    assert (
        instance.tool_prompt == schema["tool_prompt"]["default"] == DEFAULT_TOOL_PROMPT
    )
    assert instance.tool_prompt.strip()


async def test_default_prompt_is_used_when_injection_is_enabled(plugin):
    instance = MarkdownImagePlugin(plugin.context, {"enable_tool_prompt": True})
    request = ProviderRequest(
        system_prompt="原系统提示词",
    )
    await instance.inject_tool_prompt(SimpleNamespace(), request)
    assert request.system_prompt == "原系统提示词\n\n" + DEFAULT_TOOL_PROMPT


@pytest.mark.parametrize("enable_auto_render", [False, True])
@pytest.mark.parametrize(
    ("enable_tool", "enable_tool_prompt", "tool_prompt", "tool_state", "inject"),
    [
        (True, True, "使用偏好", "available", True),
        (False, True, "使用偏好", "available", True),
        (False, True, "使用偏好", "none", True),
        (True, False, "使用偏好", "available", False),
        (True, True, "", "available", False),
        (True, True, " \n\t", "available", False),
        (True, True, "使用偏好", "none", True),
        (True, True, "使用偏好", "empty", True),
        (True, True, "使用偏好", "other", True),
        (True, True, "使用偏好", "inactive", True),
    ],
    ids=[
        "enabled",
        "tool-disabled",
        "tool-disabled-and-unavailable",
        "injection-disabled",
        "empty-prompt",
        "whitespace-prompt",
        "no-tool-set",
        "empty-tool-set",
        "other-tool-only",
        "inactive-tool",
    ],
)
async def test_injection_depends_only_on_prompt_switch_and_nonempty_content(
    plugin,
    enable_auto_render,
    enable_tool,
    enable_tool_prompt,
    tool_prompt,
    tool_state,
    inject,
):
    instance = MarkdownImagePlugin(
        plugin.context,
        {
            "enable_tool": enable_tool,
            "enable_tool_prompt": enable_tool_prompt,
            "tool_prompt": tool_prompt,
            "enable_auto_render": enable_auto_render,
            "auto_threshold": 30,
        },
    )
    tools = None
    if tool_state != "none":
        tools = ToolSet()
        if tool_state != "empty":
            tool = MarkdownImageTool(instance.render_markdown_image)
            if tool_state == "other":
                tool.name = "another_tool"
            tool.active = tool_state != "inactive"
            tools.add_tool(tool)
    request = ProviderRequest(system_prompt="原系统提示词", func_tool=tools)
    await instance.inject_tool_prompt(SimpleNamespace(), request)
    expected = "原系统提示词" + ("\n\n" + tool_prompt if inject else "")
    assert request.system_prompt == expected
    assert instance.enable_auto_render is enable_auto_render
    assert instance.auto_threshold == 30


@pytest.mark.parametrize("system_prompt", ["", "原系统提示词", "原系统提示词\n"])
async def test_custom_prompt_appends_without_changing_other_request_fields(
    plugin, system_prompt
):
    tool = MarkdownImageTool(plugin.render_markdown_image)
    description = tool.description
    parameters = json.loads(json.dumps(tool.parameters))
    tools = ToolSet([tool])
    contexts = [{"role": "user", "content": "历史消息"}]
    request = ProviderRequest(
        prompt="当前用户消息",
        contexts=contexts.copy(),
        system_prompt=system_prompt,
        func_tool=tools,
    )
    await plugin.inject_tool_prompt(SimpleNamespace(), request)
    separator = "\n\n" if system_prompt else ""
    assert request.system_prompt == system_prompt + separator + plugin.tool_prompt
    assert request.prompt == "当前用户消息"
    assert request.contexts == contexts
    assert request.func_tool is tools
    assert tool.description == description
    assert tool.parameters == parameters
    plugin.renderer.render.assert_not_awaited()


async def test_prompt_injection_works_without_registering_model_tool(plugin):
    instance = MarkdownImagePlugin(
        plugin.context,
        {
            "enable_tool": False,
            "enable_tool_prompt": True,
            "tool_prompt": "独立提示词",
            "enable_auto_render": False,
        },
    )
    instance.renderer = plugin.renderer
    await instance.initialize()
    assert not instance.context.get_llm_tool_manager().get_func("render_markdown_image")
    request = ProviderRequest(system_prompt="原系统提示词")
    await instance.inject_tool_prompt(SimpleNamespace(), request)
    assert request.system_prompt == "原系统提示词\n\n独立提示词"
    assert request.func_tool is None
    instance.renderer.render.assert_not_awaited()


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({"enable_tool_prompt": None}, "enable_tool_prompt 必须是布尔值"),
        ({"enable_tool_prompt": 1}, "enable_tool_prompt 必须是布尔值"),
        ({"enable_tool_prompt": "true"}, "enable_tool_prompt 必须是布尔值"),
        ({"tool_prompt": None}, "tool_prompt 必须是字符串"),
        ({"tool_prompt": True}, "tool_prompt 必须是字符串"),
        ({"tool_prompt": 123}, "tool_prompt 必须是字符串"),
        ({"tool_prompt": ["文本"]}, "tool_prompt 必须是字符串"),
        ({"tool_prompt": {}}, "tool_prompt 必须是字符串"),
    ],
)
def test_invalid_tool_prompt_config_fails_fast_even_when_disabled(
    plugin, config, error
):
    with pytest.raises(RenderError, match=error):
        MarkdownImagePlugin(plugin.context, config)
