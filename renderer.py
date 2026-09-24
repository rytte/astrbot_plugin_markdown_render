"""Render Markdown in an isolated, offline Chromium context."""

from __future__ import annotations

import asyncio
import logging
import math
from html import escape
from pathlib import Path

from markdown_it import MarkdownIt
from playwright.async_api import Browser, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

MAX_CHARACTERS = 20_000
MAX_HEIGHT = 12_000
MAX_PIXELS = 12_000_000
MAX_PENDING = 4
RENDER_TIMEOUT = 45
logger = logging.getLogger(__name__)


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


class MarkdownRenderer:
    """Keep one browser, serialize screenshots, and bound queued work."""

    def __init__(
        self, width: int = 900, font_size: int = 22, browser_executable: str = ""
    ):
        if type(width) is not int or not 480 <= width <= 1600:
            raise RenderError("width 必须是 480～1600 的整数。")
        if type(font_size) is not int or not 14 <= font_size <= 32:
            raise RenderError("font_size 必须是 14～32 的整数。")
        if not isinstance(browser_executable, str):
            raise RenderError("browser_executable 必须是字符串。")
        if browser_executable:
            if (
                browser_executable[0] in "\"'“”‘’"
                or browser_executable[-1] in "\"'“”‘’"
            ):
                raise RenderError(
                    "browser_executable 请填写浏览器可执行文件的绝对路径，不要加引号。"
                )
            if browser_executable != browser_executable.strip() or any(
                char in browser_executable for char in "\r\n\0"
            ):
                raise RenderError(
                    "browser_executable 不得含首尾空白、换行或空字符；使用默认 Chromium 请留空。"
                )
            path = Path(browser_executable)
            if not path.is_absolute():
                raise RenderError(
                    "browser_executable 必须是 Chromium 或 Edge 可执行文件的绝对路径。"
                )
            try:
                exists = path.is_file()
            except OSError as exc:
                raise RenderError(
                    f"无法访问 browser_executable 指定的文件：{path}。请检查路径和访问权限。"
                ) from exc
            if not exists:
                raise RenderError(
                    f"browser_executable 指定的文件不存在或不是文件：{path}。不会自动切换浏览器。"
                )
        self.width = width
        self.font_size = font_size
        self.browser_executable = browser_executable
        self.browser: Browser | None = None
        self.playwright: Playwright | None = None
        self.lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self.styles = (Path(__file__).parent / "style.css").read_text(encoding="utf-8")
        self.styles += HtmlFormatter(style="friendly").get_style_defs("pre code")
        self.parser = MarkdownIt(
            "commonmark", {"html": False, "highlight": highlight_code}
        ).enable(["table", "strikethrough"])
        self.parser.add_render_rule("image", render_image_label)

    async def initialize(self) -> None:
        """Launch the configured Chromium or Edge without browser fallback.

        Raises:
            RenderError: Browser startup fails or the renderer was closed.
        """
        async with self.lock:
            if self.closed:
                raise RenderError("插件已停止，请重新加载插件。")
            if self.browser is not None:
                return
            try:
                async with asyncio.timeout(20):
                    self.playwright = await async_playwright().start()
                    launch = {"headless": True}
                    if self.browser_executable:
                        launch["executable_path"] = self.browser_executable
                    self.browser = await self.playwright.chromium.launch(**launch)
            except BaseException as exc:
                if self.playwright is not None:
                    await self.playwright.stop()
                    self.playwright = None
                if isinstance(exc, (PlaywrightError, TimeoutError, OSError)):
                    if self.browser_executable:
                        raise RenderError(
                            "配置的本地浏览器启动失败："
                            f"{self.browser_executable}。"
                            "请确认它是 Chromium 或 Edge 的可执行文件，并检查运行权限与系统依赖；"
                            "未自动切换到其他浏览器。"
                        ) from exc
                    raise RenderError(
                        "Playwright 默认 Chromium 启动失败。请在运行 AstrBot 的 Python 环境中执行 "
                        "python -m playwright install chromium；Linux 还需安装浏览器系统依赖。"
                        "未自动切换到其他浏览器。"
                    ) from exc
                raise

    def html(self, markdown: str) -> str:
        """Convert bounded Markdown into a self-contained, script-free page.

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
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" '
            "content=\"default-src 'none'; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'none'\">"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<style>"
            + self.styles
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
        if self.closed or self.browser is None or not self.browser.is_connected():
            raise RenderError("本地浏览器未运行，请重新加载插件。")
        if len(self.tasks) >= MAX_PENDING:
            raise RenderError("渲染队列已满，请稍后重试。")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with asyncio.timeout(RENDER_TIMEOUT):
                async with self.lock:
                    document = await asyncio.to_thread(self.html, markdown)
                    context = await self.browser.new_context(
                        viewport={"width": self.width, "height": 720},
                        device_scale_factor=1,
                        java_script_enabled=False,
                        service_workers="block",
                    )
                    try:
                        await context.route("**/*", lambda route: route.abort())
                        page = await context.new_page()
                        await page.set_content(document, wait_until="load")
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
                    finally:
                        try:
                            async with asyncio.timeout(5):
                                await context.close()
                        except Exception:
                            logger.exception("Failed to close Markdown browser context")
        except TimeoutError as exc:
            raise RenderError("本地渲染超时，未发送图片；请缩短内容后重试。") from exc
        finally:
            self.tasks.discard(task)

    async def close(self) -> None:
        """Cancel pending renders and release the browser process."""
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self.lock:
            try:
                if self.browser is not None:
                    await self.browser.close()
            finally:
                self.browser = None
                if self.playwright is not None:
                    await self.playwright.stop()
                    self.playwright = None
