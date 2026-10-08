async ({math, diagrams, fontSize}) => {
  if (math) {
    const elements = document.querySelectorAll(".math-inline, .math-block");
    for (const [index, element] of Array.from(elements).entries()) {
      try {
        katex.render(element.textContent, element, {
          displayMode: element.classList.contains("math-block"),
          throwOnError: true,
          trust: false,
          strict: "error",
          maxExpand: 1000,
          maxSize: 20
        });
      } catch (error) {
        return {error: `第 ${index + 1} 个 LaTeX 公式渲染失败：${error.message}`};
      }
    }
  }
  if (diagrams) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      suppressErrorRendering: true,
      htmlLabels: false,
      maxTextSize: 20000,
      maxEdges: 500,
      theme: "default",
      themeVariables: {fontSize: `${fontSize}px`},
      flowchart: {htmlLabels: false}
    });
    const elements = document.querySelectorAll(".mermaid-diagram");
    for (const [index, element] of Array.from(elements).entries()) {
      try {
        const {svg} = await mermaid.render(`markdown-diagram-${index}`, element.textContent);
        element.innerHTML = svg;
      } catch (error) {
        return {error: `第 ${index + 1} 个 Mermaid 图表渲染失败：${error.message}`};
      }
    }
  }
  await document.fonts.ready;
  return {error: null};
}
