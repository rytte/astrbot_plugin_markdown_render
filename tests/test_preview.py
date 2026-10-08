from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_markdown_render import preview


async def test_preview_owns_a_browser_service_without_sending_messages(
    tmp_path, monkeypatch
):
    source = tmp_path / "example.md"
    source.write_text("$x^2$", encoding="utf-8")
    output = tmp_path / "example.png"
    service = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    renderer = SimpleNamespace(
        initialize=AsyncMock(),
        close=AsyncMock(),
        render=AsyncMock(return_value=b"png-bytes"),
    )
    resolvers = []

    def create_renderer(**options):
        resolvers.append(options["browser_service_resolver"])
        return renderer

    monkeypatch.setattr(preview, "BrowserService", lambda: service)
    monkeypatch.setattr(preview, "MarkdownRenderer", create_renderer)
    monkeypatch.setattr(
        preview.argparse.ArgumentParser,
        "parse_args",
        lambda _: SimpleNamespace(source=source, output=output),
    )
    await preview.main()
    assert output.read_bytes() == b"png-bytes"
    assert resolvers[0]() is service
    service.initialize.assert_awaited_once()
    service.close.assert_awaited_once()
    renderer.render.assert_awaited_once_with("$x^2$")
    renderer.close.assert_awaited_once()
