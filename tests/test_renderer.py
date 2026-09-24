import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from astrbot_plugin_markdown_render.renderer import (
    MAX_CHARACTERS,
    MAX_PENDING,
    MarkdownRenderer,
    RenderError,
)
from PIL import Image


@pytest.fixture
async def renderer():
    instance = MarkdownRenderer()
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
        {"browser_executable": 123},
        {"browser_executable": "missing-browser"},
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


async def test_real_png_dimensions_and_no_network(renderer):
    requests = []
    new_context = renderer.browser.new_context

    async def observe_context(**kwargs):
        context = await new_context(**kwargs)
        context.on("request", lambda request: requests.append(request.url))
        return context

    with patch.object(renderer.browser, "new_context", side_effect=observe_context):
        png = await renderer.render(
            "# 本地图片\n\n| 中文 | value |\n| --- | --- |\n| 表格 | 正常 |\n\n"
            '```python\nprint("hello")\n```\n\n'
            "![图片](http://127.0.0.1:9/private)\n\n"
            '<img src="file:///etc/passwd">\n\n'
            '<script src="https://example.com/script.js"></script>'
        )
    image = Image.open(BytesIO(png))
    assert image.format == "PNG"
    assert image.width == 900
    assert 100 < image.height < 12_000
    assert image.convert("L").getextrema()[0] < 100
    assert requests == []
    assert renderer.browser.contexts == []
    assert not renderer.tasks


async def test_long_code_and_table_do_not_overflow(renderer):
    document = renderer.html(
        "| very long cell | code |\n| --- | --- |\n| "
        + "x" * 1000
        + " | `"
        + "y" * 1000
        + "` |\n\n```text\n"
        + "z" * 1000
        + "\n```"
    )
    page = await renderer.browser.new_page(viewport={"width": 900, "height": 720})
    try:
        await page.set_content(document)
        assert await page.evaluate("document.documentElement.scrollWidth") == 900
    finally:
        await page.close()


async def test_oversized_layout_rejected_and_context_closed(renderer):
    with pytest.raises(RenderError, match="图片过长"):
        await renderer.render("段落\n\n" * 500)
    assert renderer.browser.contexts == []
    assert not renderer.tasks


async def test_timeout_cleans_context(renderer, monkeypatch):
    import astrbot_plugin_markdown_render.renderer as module

    monkeypatch.setattr(module, "RENDER_TIMEOUT", 0.2)
    new_context = renderer.browser.new_context

    async def wait_forever():
        await asyncio.sleep(60)

    async def blocked_context(**kwargs):
        context = await new_context(**kwargs)
        context.new_page = wait_forever
        return context

    with patch.object(renderer.browser, "new_context", side_effect=blocked_context):
        with pytest.raises(RenderError, match="超时"):
            await renderer.render("正文")
    assert renderer.browser.contexts == []
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
    assert renderer.browser is None
    assert renderer.playwright is None
    with pytest.raises(RenderError, match="未运行"):
        await renderer.render("正文")


async def test_browser_startup_failure_is_actionable():
    from astrbot_plugin_markdown_render import renderer as module
    from playwright.async_api import Error as PlaywrightError

    playwright = AsyncMock()
    playwright.chromium.launch.side_effect = PlaywrightError("executable missing")
    instance = MarkdownRenderer()
    with patch.object(module, "async_playwright") as manager:
        manager.return_value.start = AsyncMock(return_value=playwright)
        with pytest.raises(RenderError, match="playwright install chromium"):
            await instance.initialize()
    playwright.stop.assert_awaited_once()
    playwright.chromium.launch.assert_awaited_once_with(headless=True)
    assert instance.playwright is None
    assert instance.browser is None


@pytest.mark.parametrize("quote", ['"', "'", "“", "”", "‘", "’"])
def test_quoted_browser_paths_are_rejected(tmp_path, quote):
    executable = tmp_path / "msedge.exe"
    executable.touch()
    with pytest.raises(RenderError, match="不要加引号"):
        MarkdownRenderer(browser_executable=f"{quote}{executable}{quote}")


@pytest.mark.parametrize("value", [" ", "\t", "relative/msedge.exe"])
def test_blank_space_and_relative_paths_do_not_select_default_browser(value):
    with pytest.raises(RenderError):
        MarkdownRenderer(browser_executable=value)


def test_nonexistent_browser_and_directory_fail_before_startup(tmp_path):
    for path in (tmp_path / "missing.exe", tmp_path):
        with pytest.raises(RenderError, match="不存在或不是文件"):
            MarkdownRenderer(browser_executable=str(path))


@pytest.mark.parametrize("executable_name", [None, "chrome.exe", "msedge.exe"])
async def test_browser_selection_is_passed_exactly_to_playwright(
    tmp_path, executable_name
):
    from astrbot_plugin_markdown_render import renderer as module

    executable = ""
    if executable_name:
        path = tmp_path / "Program Files" / executable_name
        path.parent.mkdir()
        path.touch()
        executable = str(path)
    browser = SimpleNamespace(close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        stop=AsyncMock(),
    )
    instance = MarkdownRenderer(browser_executable=executable)
    with patch.object(module, "async_playwright") as manager:
        manager.return_value.start = AsyncMock(return_value=playwright)
        try:
            await instance.initialize()
            expected = {"headless": True}
            if executable:
                expected["executable_path"] = executable
            playwright.chromium.launch.assert_awaited_once_with(**expected)
            assert instance.browser is browser
        finally:
            await instance.close()
    browser.close.assert_awaited_once()


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError, PermissionError])
async def test_custom_browser_failure_never_launches_default(tmp_path, error_type):
    from astrbot_plugin_markdown_render import renderer as module
    from playwright.async_api import Error as PlaywrightError

    executable = tmp_path / "msedge.exe"
    executable.touch()
    error = (
        PlaywrightError("failed to launch")
        if error_type is RuntimeError
        else error_type("failed to launch")
    )
    playwright = AsyncMock()
    playwright.chromium.launch.side_effect = error
    instance = MarkdownRenderer(browser_executable=str(executable))
    with patch.object(module, "async_playwright") as manager:
        manager.return_value.start = AsyncMock(return_value=playwright)
        with pytest.raises(RenderError, match="配置的本地浏览器启动失败") as caught:
            await instance.initialize()
    assert str(executable) in str(caught.value)
    assert "playwright install" not in str(caught.value)
    playwright.chromium.launch.assert_awaited_once_with(
        headless=True, executable_path=str(executable)
    )
    playwright.stop.assert_awaited_once()
    assert instance.browser is None
    assert instance.playwright is None


async def test_real_edge_can_render_with_an_absolute_executable_path():
    executable = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not executable.is_file():
        pytest.skip("This machine does not have Edge installed at the test path")
    instance = MarkdownRenderer(browser_executable=str(executable))
    try:
        await instance.initialize()
        png = await instance.render("# Edge 渲染\n\n**本地浏览器路径**")
        image = Image.open(BytesIO(png))
        assert image.format == "PNG" and image.width == 900
    finally:
        await instance.close()
