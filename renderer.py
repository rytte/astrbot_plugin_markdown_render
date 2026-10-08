"""Render Markdown in an isolated, offline Chromium context."""

from __future__ import annotations

import asyncio
import base64
import math
import re
import secrets
from collections.abc import Callable
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from .markdown_parser import create_parser, is_mermaid

MAX_CHARACTERS = 20_000
MAX_HEIGHT = 12_000
MAX_PIXELS = 12_000_000
MAX_PENDING = 4
RENDER_TIMEOUT = 45
ROOT = Path(__file__).parent


@lru_cache(maxsize=1)
def math_styles() -> str:
    """Embed the bundled WOFF2 fonts without network or local file requests."""
    directory = ROOT / "assets" / "katex"
    styles = (directory / "katex.min.css").read_text(encoding="utf-8")

    def embed_font(match: re.Match) -> str:
        data = base64.b64encode((directory / "fonts" / match[1]).read_bytes()).decode()
        return f'src:url("data:font/woff2;base64,{data}") format("woff2")'

    return re.sub(r"src:url\(fonts/([\w-]+\.woff2)\)[^;}]*", embed_font, styles)


@lru_cache(maxsize=2)
def extension_library(name: str) -> str:
    """Read a pinned library distributed with the plugin."""
    if name not in {"katex", "mermaid"}:
        raise RenderError("未知的渲染扩展。")
    return (ROOT / "assets" / name / f"{name}.min.js").read_text(encoding="utf-8")


class RenderError(ValueError):
    """An actionable rendering error suitable for the tool response."""


def highlight_code(code: str, language: str, attributes: str) -> str:
    """Highlight a fenced code block, escaping text for unknown languages.

    Args:
        code: Code block contents.
        language: Markdown fence language identifier.
        attributes: Additional fence attributes, deliberately unused.

    Returns:
        Escaped HTML suitable for the parser's code block wrapper.
    """
    if not language:
        return escape(code)
    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        return escape(code)
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def render_image_label(renderer, tokens, idx, options, env) -> str:
    """Display image alt text without loading model-supplied resources.

    Args:
        renderer: Markdown renderer bound by add_render_rule.
        tokens: Inline Markdown tokens.
        idx: Current image token index.
        options: Parser options.
        env: Parser environment.

    Returns:
        A visible placeholder that makes omitted image loading explicit.
    """
    alt = renderer.renderInlineAsText(tokens[idx].children or [], options, env)
    return (
        '<span class="image-label">[图片未加载：' + escape(alt or "图片") + "]</span>"
    )


def render_math(renderer, tokens, idx, options, env) -> str:
    """Keep formula text escaped until the trusted KaTeX renderer consumes it."""
    if not tokens[idx].content.strip():
        raise RenderError("LaTeX 公式内容不能为空。")
    block = tokens[idx].type != "math_inline"
    tag = "div" if tokens[idx].block else "span"
    kind = "block" if block else "inline"
    return f'<{tag} class="math-{kind}">{escape(tokens[idx].content)}</{tag}>'


def render_fence(renderer, tokens, idx, options, env) -> str:
    """Preserve ordinary code fences and isolate escaped Mermaid source."""
    token = tokens[idx]
    if not is_mermaid(token):
        return renderer.fence(tokens, idx, options, env)
    if not token.content.strip():
        raise RenderError("Mermaid 图表内容不能为空。")
    if token.content.lstrip().startswith("---") or re.search(r"%%\s*\{", token.content):
        raise RenderError("Mermaid 不支持 YAML 配置头或 %%{...}%% 配置指令。")
    return (
        '<div class="mermaid-diagram"><pre class="mermaid-source">'
        + escape(token.content)
        + "</pre></div>\n"
    )


class MarkdownRenderer:
    """Prepare Markdown and use the shared browser service for screenshots."""

    def __init__(
        self,
        width: int = 900,
        font_size: int = 22,
        browser_service_resolver: Callable[[], Any] | None = None,
    ):
        if type(width) is not int or not 480 <= width <= 1600:
            raise RenderError("width 必须是 480～1600 的整数。")
        if type(font_size) is not int or not 14 <= font_size <= 32:
            raise RenderError("font_size 必须是 14～32 的整数。")
        self.width = width
        self.font_size = font_size
        self.browser_service_resolver = browser_service_resolver
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self.initialized = False
        self.styles = (ROOT / "style.css").read_text(encoding="utf-8")
        self.styles += HtmlFormatter(style="friendly").get_style_defs("pre code")
        self.extensions = (ROOT / "extensions.js").read_text(encoding="utf-8")
        self.parser = create_parser(highlight_code)
        self.parser.add_render_rule("image", render_image_label)
        self.parser.add_render_rule("fence", render_fence)
        for kind in ("math_inline", "math_inline_double", "math_block"):
            self.parser.add_render_rule(kind, render_math)

    async def initialize(self) -> None:
        """Mark the renderer available without owning the shared browser.

        Raises:
            RenderError: The renderer was closed.
        """
        if self.closed:
            raise RenderError("插件已停止，请重新加载插件。")
        self.initialized = True

    def html(self, markdown: str) -> str:
        """Convert bounded Markdown into an escaped page with local font data.

        Args:
            markdown: The original model-written Markdown body.

        Returns:
            A complete HTML document with inline styles.

        Raises:
            RenderError: The input is empty, oversized, or not a string.
        """
        if not isinstance(markdown, str) or not markdown.strip():
            raise RenderError("markdown 必须是非空字符串。")
        if len(markdown) > MAX_CHARACTERS:
            raise RenderError("Markdown 超过 20000 个字符，请按章节分段调用。")
        body = self.parser.render(markdown)
        nonce = secrets.token_urlsafe(24)
        styles = self.styles
        if 'class="math-' in body:
            styles = math_styles() + styles
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" '
            f"content=\"default-src 'none'; script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; font-src data:; "
            "base-uri 'none'; form-action 'none'\">"
            f'<meta name="render-nonce" content="{nonce}">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<style>"
            + styles
            + f":root{{--body-size:{self.font_size}px;}}"
            + '</style></head><body><article class="markdown-body">'
            + body
            + "</article></body></html>"
        )

    async def render(self, markdown: str) -> bytes:
        """Render one PNG with limits on queue size, elapsed time, and pixels.

        Args:
            markdown: Markdown body, up to MAX_CHARACTERS characters.

        Returns:
            PNG bytes owned by the caller; no temporary image file is created.

        Raises:
            RenderError: Input, capacity, browser state, or layout is invalid.
        """
        if not isinstance(markdown, str) or not markdown.strip():
            raise RenderError("markdown 必须是非空字符串。")
        if len(markdown) > MAX_CHARACTERS:
            raise RenderError("Markdown 超过 20000 个字符，请按章节分段调用。")
        if self.closed or not self.initialized:
            raise RenderError("渲染器未运行，请重新加载插件。")
        if len(self.tasks) >= MAX_PENDING:
            raise RenderError("渲染队列已满，请稍后重试。")
        service = (
            self.browser_service_resolver()
            if self.browser_service_resolver is not None
            else None
        )
        if service is None:
            raise RenderError("浏览器服务不可用，请启用 astrbot_plugin_browser 插件。")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with asyncio.timeout(RENDER_TIMEOUT):
                async with self.lock:
                    document = await asyncio.to_thread(self.html, markdown)
                    math_enabled = 'class="math-' in document
                    diagrams_enabled = 'class="mermaid-diagram"' in document
                    async with service.session(
                        viewport={"width": self.width, "height": 720},
                        javascript_enabled=math_enabled or diagrams_enabled,
                        timeout=RENDER_TIMEOUT,
                    ) as page:
                        await page.set_content(document, wait_until="load")
                        for name, enabled in (
                            ("katex", math_enabled),
                            ("mermaid", diagrams_enabled),
                        ):
                            if enabled:
                                library = await asyncio.to_thread(
                                    extension_library, name
                                )
                                await page.evaluate(
                                    """source => {
                                        const script = document.createElement('script');
                                        script.nonce = document.querySelector('meta[name="render-nonce"]').content;
                                        script.textContent = source;
                                        document.head.appendChild(script);
                                        script.remove();
                                    }""",
                                    library,
                                )
                        if math_enabled or diagrams_enabled:
                            result = await page.evaluate(
                                self.extensions,
                                {
                                    "math": math_enabled,
                                    "diagrams": diagrams_enabled,
                                    "fontSize": self.font_size,
                                },
                            )
                            if result["error"]:
                                raise RenderError(result["error"])
                        await page.evaluate("document.fonts.ready")
                        article = page.locator(".markdown-body")
                        box = await article.bounding_box()
                        if box is None:
                            raise RenderError(
                                "无法获取正文尺寸，请检查 Markdown 内容。"
                            )
                        height = math.ceil(box["height"])
                        if height > MAX_HEIGHT or self.width * height > MAX_PIXELS:
                            raise RenderError(
                                "排版后的图片过长（上限 12000 像素或 1200 万像素面积），"
                                "请按章节分段调用，不要删减正文。"
                            )
                        return await article.screenshot(
                            type="png", animations="disabled"
                        )
        except TimeoutError as exc:
            raise RenderError("本地渲染超时，未发送图片；请缩短内容后重试。") from exc
        finally:
            self.tasks.discard(task)

    async def close(self) -> None:
        """Cancel pending renders without closing the shared browser service."""
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.initialized = False
