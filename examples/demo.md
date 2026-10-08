# Markdown 图片回复

模型把 **Markdown 正文** 交给工具，插件在本地排版为 PNG，并发送到当前会话。

> 无需在线渲染服务。适合清晰展示步骤、对比表格、代码片段和数学公式。

## 功能一览

| 内容 | 支持情况 | 示例 |
| --- | :---: | --- |
| 中文排版 | 支持 | 标题、段落与**重点** |
| 列表与引用 | 支持 | 操作步骤、注意事项 |
| 代码高亮 | 支持 | Python、JavaScript 等 |
| LaTeX 公式 | 支持 | 分式、根号、积分与矩阵 |
| 任务列表 | 支持 | 已完成与待完成的复选框 |
| 外部图片 | 不加载 | 显示图片说明文字 |

## 调用流程

1. 模型组织好要展示的 Markdown。
2. 调用 `render_markdown_image`。
3. 插件生成图片并发送，模型收到发送结果。

## Python 代码

```python
async def greet(name: str) -> str:
    return f"你好，{name}！"

message = await greet("AstrBot")
```

- 支持 **粗体**、*斜体*、~~删除线~~ 和 `行内代码`。
- 支持嵌套列表：
  - 每次调用只发送到当前会话。
  - 长文可以按章节分段调用。

## LaTeX 公式

### 行内公式

勾股定理：$a^2 + b^2 = c^2$。欧拉恒等式：\(e^{i\pi} + 1 = 0\)。

### 求根公式

$$
x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}
$$

### 积分与矩阵

\[
\int_0^{\infty} e^{-x^2}\,dx = \frac{\sqrt{\pi}}{2}
\]

$$
A = \begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix},
\qquad \det(A) = 1 \times 4 - 2 \times 3 = -2
$$

## 任务列表

- [x] 行内公式与中文段落
- [x] 分式、根号、积分和矩阵
- [x] 本地字体，不请求在线资源
- [ ] 继续探索更多数学表达式

---

参考链接：[AstrBot 文档](https://docs.astrbot.app/)

![示意图片](https://example.com/demo.png)
