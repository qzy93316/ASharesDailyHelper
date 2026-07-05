---
name: render-html
description: 把日报/复盘等 Markdown 转成富文本 HTML（表格、卡片、涨跌红绿着色、信号徽标），可读性更强、可直接双击打开。Use when 用户说"把日报转成HTML/生成网页版/出个好看的报告/渲染成HTML/转成富文本/发我一份能看的报告"，或每次生成日报后想要更友好的可视化版本时。
---

# 渲染 HTML（Render HTML）

把项目产出的 Markdown（每日报告、复盘随笔、持仓诊断等）转成**自带样式、响应式、单文件**的 HTML —— 富文本排版 + 结构化表格，比纯文本 Markdown 对用户友好得多，可直接双击用浏览器打开、也便于存档或转发。

## 何时触发

- 用户说："把今天的日报转成 HTML""生成网页版报告""出个好看点的""渲染成富文本"
- 每天生成 `daily_report.py` 的 Markdown 后，顺手产一份 HTML 版
- 复盘随笔、持仓诊断结果等任何 Markdown 想要更好读时

## 一条命令

脚本：`skills/render-html/scripts/md_to_html.py`（**纯标准库，无需 pip install**）

```bash
# 默认输出同名 .html
python skills/render-html/scripts/md_to_html.py "reports/日报-2026-07-04.md"

# 指定输出与标题
python skills/render-html/scripts/md_to_html.py "reports/日报-2026-07-04.md" \
  -o "reports/日报-2026-07-04.html" --title "A股盘前报告 2026-07-04"
```

生成后告诉用户 HTML 路径，让其双击打开即可。

## 渲染增强（专为 A股 报告优化）

| Markdown 结构 | HTML 呈现 |
|---|---|
| `# / ## / ###` | 分级标题；`##` 带左侧色条，`###`(荐股)渲染成**卡片** |
| `\| 表格 \|` | 圆角带阴影表格、表头底色、悬停高亮 |
| `+1.23%` / `-1.23%` | **红涨绿跌**着色(A股习惯) |
| 信号 emoji 🔵🟡⚪🔴 | 转成彩色徽标(蓝=关注/黄=持有/灰=观望/红=回避) |
| `> 引用` | 黄色提示框(适合免责声明) |
| `<!-- 注释 -->` | 自动剥离(如消息面占位符) |

## 支持的 Markdown 子集

针对本项目报告结构定制，覆盖：标题、引用块、表格、无序列表、`**加粗**`、`` `代码` ``、水平线、HTML 注释剥离、段落。**不支持**图片、有序列表嵌套、代码块高亮等（本项目报告用不到；如需扩展改脚本即可）。

## 能力边界

- 只做「Markdown → HTML」的**呈现层**转换，不改内容、不做分析
- 输出为单文件 HTML（样式内联），离线可开、无外部依赖
- 与 [trading-memory](../trading-memory/SKILL.md) 配合：复盘随笔也可一键转 HTML 归档
