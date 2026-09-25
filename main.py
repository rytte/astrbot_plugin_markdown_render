"""Render Markdown through a model tool or automatic reply detection."""

from __future__ import annotations

import asyncio
import json

from astrbot.api import AstrBotConfig, FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star

from .detector import analyze_markdown
from .renderer import MAX_CHARACTERS, RENDER_TIMEOUT, MarkdownRenderer, RenderError


class MarkdownImageTool(FunctionTool):
    """Declare the required Markdown argument through an explicit schema."""

    def __init__(self, handler):
        super().__init__(
            name="render_markdown_image",
            description=(
                "把 Markdown 正文在本地渲染成 PNG，并直接发送到当前会话。"
                "适合用户要求图片回复或需要展示表格、代码和排版时使用。"
                "支持标题、列表、引用、表格、删除线与代码高亮；"
                "不执行 HTML、脚本，不下载外部图片。"
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
        self.mode = settings.pop("mode", "tool")
        if self.mode not in ("tool", "auto"):
            raise RenderError("mode 必须是 tool（模型工具）或 auto（自动检测）。")
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
        """Initialize renderer state and register the selected mode's tool."""
        await self.renderer.initialize()
        if self.mode == "tool":
            self.context.add_llm_tools(MarkdownImageTool(self.render_markdown_image))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def prepare_auto_reply(self, event: AstrMessageEvent) -> None:
        """Wait for complete standard model replies before making a rendering decision.

        Args:
            event: Incoming event before AstrBot chooses its streaming mode.
        """
        if self.mode == "auto":
            event.set_extra("enable_streaming", False)

    @filter.on_decorating_result()
    async def auto_render_reply(self, event: AstrMessageEvent) -> None:
        """Replace a formatted model reply in place, leaving delivery to AstrBot.

        Args:
            event: Event containing the complete reply about to be sent.
        """
        if self.mode != "auto":
            return
        result = event.get_result()
        if result is None or not result.is_llm_result() or not result.chain:
            return
        # Auto mode owns this reply's image decision, including text on failure.
        result.use_t2i_ = False
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
                analysis = await asyncio.to_thread(analyze_markdown, markdown)
                if analysis.score < self.auto_threshold:
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
        if self.mode != "tool":
            return json.dumps(
                {
                    "ok": False,
                    "stage": "mode",
                    "error": "当前为自动检测模式，未启用模型转图工具。",
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
