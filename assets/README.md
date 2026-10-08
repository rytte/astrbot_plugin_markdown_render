# 内置浏览器资源

- KaTeX **0.19.0**：生产版 JavaScript、CSS 和 WOFF2 字体，许可证见 `katex/LICENSE`。
- Mermaid **12.1.0**：生产版浏览器 JavaScript，许可证见 `mermaid/LICENSE`。

这些文件随插件分发，运行时不请求 CDN，不需要 Node.js 或 npm。
仅第三方生产资源约 6 MB，不包含源码、source map 或完整 npm 包。

维护者可在仓库根目录运行 `python scripts/vendor_assets.py` 重新获取固定版本的资源。
下载脚本使用 npm 注册表提供的 SHA-512 integrity 校验完整发布包。
