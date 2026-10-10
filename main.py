"""Render Markdown through independently enabled tools and reply detection."""

from __future__ import annotations

import asyncio
import json

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .detector import analyze_markdown
from .renderer import MAX_CHARACTERS, RENDER_TIMEOUT, MarkdownRenderer, RenderError

DEFAULT_TOOL_PROMPT = (
    "用户明确要求图片回复，或回答包含复杂表格、多行代码、较多排版结构时，"
    "优先使用 render_markdown_image。"
    "简短回答和普通列表直接回复文字；用户明确要求纯文本时不要调用。"
)


class MarkdownImageTool(FunctionTool):
    """Declare the required Markdown argument through an explicit schema."""

    def __init__(self, handler):
        super().__init__(
            name="render_markdown_image",
            description=(
                "把 Markdown 正文在本地渲染成 PNG，并直接发送到当前会话。"
                "适合用户要求图片回复或需要展示表格、公式、图表、代码和排版时使用。"
                "支持标题、任务列表、引用、表格、删除线、代码高亮、LaTeX 公式和 Mermaid 图表；"
                "不执行用户提供的 HTML、脚本，不下载外部图片。"
                "只传正文，不要用一层代码围栏包住整篇文章。"
                "成功后图片已经发送，不要重复调用或再次输出全文；"
                "失败时根据错误说明处理。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "markdown": {
                        "type": "string",
                        "description": "完整 Markdown 正文；过长时按章节分段调用。",
                        "minLength": 1,
                        "maxLength": MAX_CHARACTERS,
                    }
                },
                "required": ["markdown"],
                "additionalProperties": False,
            },
            handler=handler,
        )


class MarkdownImagePlugin(Star):
    """Render model-written Markdown and deliver it to the invoking chat."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        settings = dict(config)
        self.enable_tool = settings.pop("enable_tool", False)
        self.enable_tool_prompt = settings.pop("enable_tool_prompt", False)
        self.tool_prompt = settings.pop("tool_prompt", DEFAULT_TOOL_PROMPT)
        self.enable_auto_render = settings.pop("enable_auto_render", True)
        if type(self.enable_tool) is not bool:
            raise RenderError("enable_tool 必须是布尔值（true 或 false）。")
        if type(self.enable_tool_prompt) is not bool:
            raise RenderError("enable_tool_prompt 必须是布尔值（true 或 false）。")
        if not isinstance(self.tool_prompt, str):
            raise RenderError("tool_prompt 必须是字符串。")
        self.tool_prompt = self.tool_prompt.strip()
        if type(self.enable_auto_render) is not bool:
            raise RenderError("enable_auto_render 必须是布尔值（true 或 false）。")
        self.auto_threshold = settings.pop("auto_threshold", 6)
        if type(self.auto_threshold) is not int or not 1 <= self.auto_threshold <= 30:
            raise RenderError("auto_threshold 必须是 1～30 的整数。")
        if settings.pop("browser_executable", ""):
            logger.warning(
                "Markdown renderer browser_executable is now configured by astrbot_plugin_browser."
            )
        unknown = set(settings) - {"width", "font_size"}
        if unknown:
            raise RenderError("未知插件配置：" + ", ".join(sorted(unknown)))
        self.renderer = MarkdownRenderer(
            **settings, browser_service_resolver=self.get_browser_service
        )

    def get_browser_service(self):
        metadata = self.context.get_registered_star("astrbot_plugin_browser")
        plugin = metadata.star_cls if metadata and metadata.activated else None
        service = getattr(plugin, "service", None)
        if service is None:
            raise RenderError("浏览器服务不可用，请启用 astrbot_plugin_browser 插件。")
        return service

    async def initialize(self) -> None:
        """Initialize the shared renderer and register the tool when enabled."""
        await self.renderer.initialize()
        if self.enable_tool:
            self.context.add_llm_tools(MarkdownImageTool(self.render_markdown_image))

    @filter.on_llm_request()
    async def inject_tool_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Append the configured prompt independently of rendering features.

        Args:
            event: Event associated with the model request.
            req: Model request whose existing system prompt must be preserved.
        """
        if not self.enable_tool_prompt or not self.tool_prompt:
            return
        req.system_prompt += ("\n\n" if req.system_prompt else "") + self.tool_prompt

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def prepare_auto_reply(self, event: AstrMessageEvent) -> None:
        """Wait for complete standard model replies before making a rendering decision.

        Args:
            event: Incoming event before AstrBot chooses its streaming mode.
        """
        if self.enable_auto_render:
            event.set_extra("enable_streaming", False)

    @filter.on_decorating_result()
    async def auto_render_reply(self, event: AstrMessageEvent) -> None:
        """Replace a formatted model reply in place, leaving delivery to AstrBot.

        Args:
            event: Event containing the complete reply about to be sent.
        """
        if not self.enable_auto_render:
            return
        result = event.get_result()
        if result is None or not result.is_llm_result() or not result.chain:
            return
        # Auto mode owns this reply's image decision, including text on failure.
        result.use_t2i_ = False
        if event.get_extra("markdown_render_tool_sent", False):
            return
        if not all(isinstance(part, Plain | At | Reply) for part in result.chain):
            return
        positions = [
            i for i, part in enumerate(result.chain) if isinstance(part, Plain)
        ]
        if not positions:
            return
        first, last = positions[0], positions[-1]
        # Preserve mentions and quotes exactly; do not move text across them.
        if any(not isinstance(part, Plain) for part in result.chain[first : last + 1]):
            return
        markdown = "".join(part.text for part in result.chain[first : last + 1])
        if not markdown.strip():
            return
        if len(markdown) > MAX_CHARACTERS:
            logger.warning(
                "Automatic Markdown rendering skipped: reply exceeds character limit"
            )
            return
        try:
            async with asyncio.timeout(RENDER_TIMEOUT):
                analysis = await asyncio.to_thread(
                    analyze_markdown, markdown, self.auto_threshold
                )
                if analysis.score < self.auto_threshold and not analysis.has_long_list:
                    return
                replacement = []
                cursor = 0
                spans = (
                    analysis.block_spans
                    if analysis.block_spans
                    and analysis.outside_score < self.auto_threshold
                    else ((0, len(markdown)),)
                )
                for start, end in spans:
                    text = markdown[cursor:start]
                    if text.strip():
                        replacement.append(Plain(text))
                    png = await self.renderer.render(markdown[start:end])
                    replacement.append(Image.fromBytes(png))
                    cursor = end
                if markdown[cursor:].strip():
                    replacement.append(Plain(markdown[cursor:]))
        except Exception:
            logger.exception(
                "Automatic Markdown rendering failed; retaining original text"
            )
            return
        # The provider may still own the original list for history or tracing.
        result.chain = [*result.chain[:first], *replacement, *result.chain[last + 1 :]]

    async def render_markdown_image(
        self, event: AstrMessageEvent, markdown: str
    ) -> str:
        """Render Markdown and send exactly one image to the invoking event.

        Args:
            event: The current event supplied by AstrBot.
            markdown: Complete Markdown body to display.

        Returns:
            A JSON delivery receipt or actionable failure description.
        """
        if not self.enable_tool:
            return json.dumps(
                {
                    "ok": False,
                    "stage": "disabled",
                    "error": "模型转图工具未启用，请开启 enable_tool。",
                },
                ensure_ascii=False,
            )
        try:
            png = await self.renderer.render(markdown)
        except RenderError as exc:
            return json.dumps(
                {"ok": False, "stage": "render", "error": str(exc)},
                ensure_ascii=False,
            )
        except Exception:
            logger.exception("Markdown image rendering failed")
            return json.dumps(
                {
                    "ok": False,
                    "stage": "render",
                    "error": "本地渲染失败，未发送图片；请检查 AstrBot 插件日志。",
                },
                ensure_ascii=False,
            )

        try:
            await event.send(
                MessageChain(chain=[Image.fromBytes(png)], type="tool_direct_result")
            )
        except Exception:
            logger.exception("Markdown image delivery failed")
            return json.dumps(
                {
                    "ok": False,
                    "stage": "send",
                    "error": "图片发送失败，平台可能已收到图片；请勿自动重试，以免重复发送。",
                },
                ensure_ascii=False,
            )
        event.set_extra("markdown_render_tool_sent", True)
        return json.dumps(
            {
                "ok": True,
                "sent_images": 1,
                "message": "图片已发送到当前会话，无需重复发送或输出 Markdown 全文。",
            },
            ensure_ascii=False,
        )

    async def terminate(self) -> None:
        """Release the browser and cancel pending renders on plugin unload."""
        await self.renderer.close()
