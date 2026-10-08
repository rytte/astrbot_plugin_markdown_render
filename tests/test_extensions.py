import os
from contextlib import asynccontextmanager
from io import BytesIO

import pytest
from astrbot_plugin_markdown_render.renderer import MarkdownRenderer, RenderError
from PIL import Image

pytestmark = pytest.mark.skipif(
    not os.environ.get("ASTRBOT_BROWSER_EXECUTABLE"),
    reason="Set ASTRBOT_BROWSER_EXECUTABLE to run real extension integration tests",
)


class ObservedService:
    def __init__(self, service):
        self.service = service
        self.requests = []
        self.snapshots = []
        self.attempt_script = False

    @asynccontextmanager
    async def session(self, **options):
        async with self.service.session(**options) as page:
            page.on("request", lambda request: self.requests.append(request.url))
            yield page
            if self.attempt_script:
                await page.evaluate("""() => {
                    const script = document.createElement('script');
                    script.textContent = 'globalThis.evil = true';
                    document.head.appendChild(script);
                    script.remove();
                }""")
            self.snapshots.append(
                await page.evaluate("""() => ({
                    math: document.querySelectorAll('.katex').length,
                    diagrams: document.querySelectorAll('.mermaid-diagram > svg').length,
                    checked: document.querySelectorAll('input:checked:disabled').length,
                    fonts: Array.from(document.fonts).filter(font => font.status === 'loaded').length,
                    evil: Boolean(globalThis.evil),
                    images: document.querySelectorAll('img, image').length,
                    scripts: document.scripts.length,
                    links: Array.from(document.querySelectorAll('svg a')).map(link => link.getAttribute('href') || link.getAttribute('xlink:href'))
                })""")
            )


@pytest.fixture
async def browser_renderer():
    from astrbot_plugin_browser.service import BrowserService

    service = BrowserService(
        browser_executable=os.environ["ASTRBOT_BROWSER_EXECUTABLE"]
    )
    observed = ObservedService(service)
    renderer = MarkdownRenderer(browser_service_resolver=lambda: observed)
    await service.initialize()
    await renderer.initialize()
    try:
        yield renderer, observed, service
    finally:
        await renderer.close()
        await service.close()


async def test_browser_renders_all_math_delimiters_tasks_and_multiple_diagrams(
    browser_renderer,
):
    renderer, observed, _ = browser_renderer
    png = await renderer.render(
        r"中文公式 $x^2$ 和 \(y^2\)。" + "\n\n"
        r"$$\frac{1}{2}$$" + "\n\n"
        "\\[\n\\sqrt{2}\n\\]\n\n"
        "> $$\n> a^2+b^2=c^2\n> $$\n\n"
        "- [ ] 未完成\n- [x] 已完成\n\n"
        "```mermaid\nflowchart LR\nA[开始] --> B[结束]\n```\n\n"
        "```mermaid\nsequenceDiagram\n用户->>插件: 渲染正文\n插件-->>用户: 图片\n```"
    )
    image = Image.open(BytesIO(png))
    assert image.format == "PNG" and image.width == 900
    snapshot = observed.snapshots[-1]
    assert snapshot["math"] == 5
    assert snapshot["diagrams"] == 2
    assert snapshot["checked"] == 1
    assert snapshot["fonts"] > 0
    assert snapshot["scripts"] == 0
    assert observed.requests == []


async def test_browser_rejects_invalid_syntax_and_remains_available(browser_renderer):
    renderer, observed, service = browser_renderer
    for source, name in [
        (r"$\frac{$", "LaTeX"),
        ("```mermaid\nnot a diagram\n```", "Mermaid"),
    ]:
        with pytest.raises(RenderError, match=name):
            await renderer.render(source)
        assert not renderer.tasks
        assert service.ready
    assert (await renderer.render("$x^2$")).startswith(b"\x89PNG\r\n\x1a\n")
    assert observed.requests == []


async def test_browser_does_not_execute_model_scripts_or_load_external_resources(
    browser_renderer,
):
    renderer, observed, _ = browser_renderer
    observed.attempt_script = True
    await renderer.render(
        "<script>globalThis.evil = true</script>\n\n"
        '<img src="https://example.com/private.png" onerror="globalThis.evil=true">\n\n'
        r"$\href{javascript:alert(1)}{click}$" + "\n\n"
        "```mermaid\nflowchart LR\nA[开始] --> B[结束]\n"
        'click A "javascript:alert(1)"\n```'
    )
    snapshot = observed.snapshots[-1]
    assert not snapshot["evil"]
    assert snapshot["images"] == 0
    assert not any(snapshot["links"])
    assert snapshot["scripts"] == 0
    assert observed.requests == []


async def test_browser_renders_other_builtin_mermaid_diagrams(browser_renderer):
    renderer, observed, _ = browser_renderer
    sources = [
        "classDiagram\nAnimal <|-- Duck\nAnimal : +int age",
        "stateDiagram-v2\n[*] --> Running\nRunning --> [*]",
        "erDiagram\nUSER ||--o{ NOTE : writes",
        'pie title Composition\n"A" : 60\n"B" : 40',
        "gantt\ndateFormat YYYY-MM-DD\nsection Plan\nTask :2026-10-08, 3d",
        "mindmap\n  root((Markdown))\n    Math\n    Diagrams",
    ]
    for source in sources:
        assert (await renderer.render(f"```mermaid\n{source}\n```")).startswith(
            b"\x89PNG\r\n\x1a\n"
        )
        assert observed.snapshots[-1]["diagrams"] == 1
    assert observed.requests == []
