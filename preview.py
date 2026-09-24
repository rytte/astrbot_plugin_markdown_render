"""Render a local sample without starting AstrBot or sending a message."""

import argparse
import asyncio
from pathlib import Path

from .renderer import MarkdownRenderer


async def main() -> None:
    """Read Markdown and write a PNG for local visual verification."""
    parser = argparse.ArgumentParser(description="将本地 Markdown 文件渲染为 PNG")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    markdown = await asyncio.to_thread(args.source.read_text, encoding="utf-8")
    renderer = MarkdownRenderer()
    try:
        await renderer.initialize()
        png = await renderer.render(markdown)
        await asyncio.to_thread(args.output.write_bytes, png)
    finally:
        await renderer.close()
    print(args.output.resolve())


if __name__ == "__main__":
    asyncio.run(main())
