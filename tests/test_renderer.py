import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_markdown_render.renderer import (
    MAX_CHARACTERS,
    MAX_HEIGHT,
    MAX_PENDING,
    MarkdownRenderer,
    RenderError,
)


class FakeService:
    def __init__(self):
        self.options = []
        self.closed_sessions = 0
        self.box = {"width": 900, "height": 600}
        self.set_content_side_effect = None

    @asynccontextmanager
    async def session(self, **options):
        self.options.append(options)
        page = FakePage(self)
        try:
            yield page
        finally:
            self.closed_sessions += 1


class FakePage:
    def __init__(self, service):
        self.service = service
        self.set_content = AsyncMock(side_effect=self._set_content)
        self.locator_instance = FakeLocator(service)
        self.locator = Mock(return_value=self.locator_instance)

    async def _set_content(self, *args, **kwargs):
        if self.service.set_content_side_effect is not None:
            await self.service.set_content_side_effect()

    async def evaluate(self, script):
        return None


class FakeLocator:
    def __init__(self, service):
        self.bounding_box = AsyncMock(return_value=service.box)
        self.screenshot = AsyncMock(return_value=b"png-bytes")


@pytest.fixture
async def renderer():
    service = FakeService()
    instance = MarkdownRenderer(browser_service_resolver=lambda: service)
    instance.service = service
    await instance.initialize()
    yield instance
    await instance.close()


@pytest.mark.parametrize(
    "value",
    ["", " \n\t", None, 123, "字" * (MAX_CHARACTERS + 1)],
    ids=["empty", "whitespace", "null", "number", "oversized"],
)
def test_invalid_markdown(value):
    with pytest.raises(RenderError):
        MarkdownRenderer().html(value)


@pytest.mark.parametrize(
    "config",
    [
        {"width": 0},
        {"width": True},
        {"width": "900"},
        {"width": 1601},
        {"font_size": 33},
        {"font_size": 13},
        {"font_size": False},
        {"browser_executable": "legacy setting"},
        {"legacy_width": 900},
    ],
)
def test_invalid_configuration_fails_fast(config):
    with pytest.raises((RenderError, TypeError)):
        MarkdownRenderer(**config)


def test_markdown_structure_highlighting_and_html_escaping():
    document = MarkdownRenderer().html(
        "# 标题\n\n**重点** 和 ~~删除~~\n\n> 引用\n\n"
        "| 列 | 值 |\n| --- | --- |\n| 中文 | `code` |\n\n"
        '```python\nprint("你好")\n```\n\n'
        "```unknown-language\n<script>never()</script>\n```\n\n"
        "<script>window.evil=true</script>\n\n"
        "![示意图](https://example.com/private.png)"
    )
    assert "<h1>标题</h1>" in document
    assert "<table>" in document
    assert "<strong>重点</strong>" in document
    assert "<s>删除</s>" in document
    assert "<blockquote>" in document
    assert '<span class="nb">print</span>' in document
    assert "&lt;script&gt;" in document
    assert "<script>" not in document
    assert "<img" not in document
    assert "https://example.com/private.png" not in document
    assert "图片未加载：示意图" in document


async def test_render_uses_shared_offline_page_session(renderer):
    png = await renderer.render(
        "# 本地图片\n\n| 中文 | value |\n| --- | --- |\n| 表格 | 正常 |\n\n"
        '```python\nprint("hello")\n```\n\n'
        "![图片](http://127.0.0.1:9/private)\n\n"
        '<img src="file:///etc/passwd">\n\n'
        '<script src="https://example.com/script.js"></script>'
    )

    assert png == b"png-bytes"
    assert renderer.service.options == [
        {
            "viewport": {"width": 900, "height": 720},
            "javascript_enabled": False,
            "timeout": 45,
        }
    ]
    assert renderer.service.closed_sessions == 1
    assert not renderer.tasks


async def test_oversized_layout_rejected_and_session_closed(renderer):
    renderer.service.box = {"width": 900, "height": MAX_HEIGHT + 1}
    with pytest.raises(RenderError, match="图片过长"):
        await renderer.render("正文")
    assert renderer.service.closed_sessions == 1
    assert not renderer.tasks


async def test_timeout_cleans_session(renderer, monkeypatch):
    import astrbot_plugin_markdown_render.renderer as module

    monkeypatch.setattr(module, "RENDER_TIMEOUT", 0.2)

    async def wait_forever():
        await asyncio.sleep(60)

    renderer.service.set_content_side_effect = wait_forever
    with pytest.raises(RenderError, match="超时"):
        await renderer.render("正文")
    assert renderer.service.closed_sessions == 1
    assert not renderer.tasks


async def test_queue_limit_and_unload_cancels_pending_work(renderer):
    await renderer.lock.acquire()
    pending = [asyncio.create_task(renderer.render("正文")) for _ in range(MAX_PENDING)]
    await asyncio.sleep(0)
    try:
        with pytest.raises(RenderError, match="队列已满"):
            await renderer.render("新请求")
    finally:
        renderer.lock.release()
    await renderer.close()
    assert all(task.cancelled() for task in pending)
    assert not renderer.tasks
    with pytest.raises(RenderError, match="未运行"):
        await renderer.render("正文")


@pytest.mark.skipif(
    not os.environ.get("ASTRBOT_BROWSER_EXECUTABLE"),
    reason="Set ASTRBOT_BROWSER_EXECUTABLE to run a real browser integration test",
)
async def test_real_browser_renders_markdown_offline():
    from astrbot_plugin_browser.service import BrowserService

    service = BrowserService(
        browser_executable=os.environ["ASTRBOT_BROWSER_EXECUTABLE"]
    )
    renderer = MarkdownRenderer(browser_service_resolver=lambda: service)
    await service.initialize()
    await renderer.initialize()
    try:
        png = await renderer.render("# 中文标题\n\n**共享浏览器会话**")
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert service.ready
    finally:
        await renderer.close()
        await service.close()
