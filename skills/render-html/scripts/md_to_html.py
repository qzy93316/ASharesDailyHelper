# -*- coding: utf-8 -*-
"""Markdown → 富文本 HTML 渲染器(纯标准库,无需 pip install)。

面向本项目日报/复盘的 Markdown 子集,产出自带样式、响应式、可直接双击打开的
单文件 HTML:标题层级、引用块、表格、列表、加粗、水平线、HTML 注释剥离。

额外增强(让 A股 报告更好读):
  - 涨跌幅 +x% 标红、-x% 标绿(A股习惯:红涨绿跌)
  - 信号 emoji(🔵🟡⚪🔴)着色成彩色徽标
  - 「### 名称(代码)」荐股标题渲染成卡片

用法:
  python md_to_html.py <输入.md> [-o 输出.html] [--title 标题]
  python md_to_html.py reports/日报-2026-07-04.md          # 同名 .html
"""
import argparse
import html
import re
from pathlib import Path

CSS = """
:root{--up:#e03131;--down:#2f9e44;--bg:#f7f8fa;--card:#fff;--ink:#1f2933;
--muted:#66707a;--line:#e5e8ec;--accent:#1971c2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:28px;margin:.2em 0 .6em;padding-bottom:.3em;border-bottom:3px solid var(--accent)}
h2{font-size:22px;margin:1.6em 0 .6em;padding-left:12px;border-left:5px solid var(--accent)}
h3{font-size:18px;margin:0 0 .4em}
blockquote{margin:1em 0;padding:12px 16px;background:#fff8e1;border-left:4px solid #f59f00;
border-radius:6px;color:#8a6d00;font-size:14px}
table{border-collapse:collapse;width:100%;margin:1em 0;background:var(--card);
border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);font-size:15px}
th{background:#eef2f6;font-weight:600}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f4f9ff}
.up{color:var(--up);font-weight:600}.down{color:var(--down);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);
border-radius:10px;padding:16px 20px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.card ul{margin:.4em 0 0;padding-left:1.2em}.card li{margin:.25em 0;font-size:14.5px}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:13px;
font-weight:600;margin-left:6px}
.b-blue{background:#e7f1ff;color:#1971c2}.b-yellow{background:#fff3bf;color:#a67c00}
.b-gray{background:#eceff2;color:#66707a}.b-red{background:#ffe3e3;color:#e03131}
ul{padding-left:1.3em}li{margin:.3em 0}
hr{border:none;border-top:1px dashed var(--line);margin:2em 0}
code{background:#eceff2;padding:1px 6px;border-radius:4px;font-size:14px}
.foot{margin-top:2em;color:var(--muted);font-size:13px;text-align:center}
"""

SIGNAL_BADGE = {"🔵": "b-blue", "🟡": "b-yellow", "⚪": "b-gray", "🔴": "b-red"}


def _inline(text: str) -> str:
    """行内元素:先转义,再处理 **加粗**、`代码`、涨跌幅着色。"""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+?)`", r"<code>\1</code>", t)
    # 涨跌幅着色:+1.23% 红 / -1.23% 绿(A股习惯)
    t = re.sub(r"(?<![\d.])(\+\d[\d.]*%)", r'<span class="up">\1</span>', t)
    t = re.sub(r"(?<![\d.])(-\d[\d.]*%)", r'<span class="down">\1</span>', t)
    return t


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _signal_badge(text: str) -> str:
    """把标题里的信号 emoji 转成彩色徽标。"""
    for emo, cls in SIGNAL_BADGE.items():
        if emo in text:
            text = text.replace(emo, "").strip()
            return f'{_inline(text)} <span class="badge {cls}">{emo}</span>'
    return _inline(text)


def convert(md: str) -> str:
    lines = md.splitlines()
    out, i, n = [], 0, len(lines)
    in_card = False

    def close_card():
        nonlocal in_card
        if in_card:
            out.append("</ul></div>")
            in_card = False

    while i < n:
        line = lines[i]
        # 剥离 HTML 注释块(占位符)
        if "<!--" in line:
            while i < n and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        stripped = line.strip()

        # 表格:当前行是 |...| 且下一行是分隔行
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]) and "---" in lines[i + 1]:
            close_card()
            header = _cells(stripped)
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                cells = _cells(lines[i])
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_card()
            level, text = len(m.group(1)), m.group(2)
            if level == 3:  # ### 荐股 → 卡片
                out.append(f'<div class="card"><h3>{_signal_badge(text)}</h3><ul>')
                in_card = True
            else:
                out.append(f"<h{level}>{_inline(text)}</h{level}>")
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            close_card()
            out.append(f"<blockquote>{_inline(stripped.lstrip('> ').rstrip())}</blockquote>")
            i += 1
            continue

        # 水平线
        if re.match(r"^-{3,}$|^\*{3,}$", stripped):
            close_card()
            out.append("<hr>")
            i += 1
            continue

        # 列表项(卡片内外都先产出 <li>,卡片外的由 _wrap_loose_li 事后包进 <ul>)
        if re.match(r"^[-*]\s+", stripped):
            item = _inline(re.sub(r"^[-*]\s+", "", stripped))
            out.append(f"<li>{item}</li>")
            i += 1
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 普通段落
        close_card()
        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    close_card()
    # 把游离的 <li> 包进 <ul>(卡片外的列表)
    body = _wrap_loose_li("\n".join(x for x in out if x is not None))
    return body


def _wrap_loose_li(body: str) -> str:
    """将卡片外连续的 <li> 包成 <ul>(卡片内的已自带 <ul>)。"""
    lines = body.split("\n")
    res, buf = [], []
    for ln in lines:
        if ln.startswith("<li>") and not _inside_card(res):
            buf.append(ln)
        else:
            if buf:
                res.append("<ul>" + "".join(buf) + "</ul>")
                buf = []
            res.append(ln)
    if buf:
        res.append("<ul>" + "".join(buf) + "</ul>")
    return "\n".join(res)


def _inside_card(res: list[str]) -> bool:
    """判断当前是否处于未闭合的 card 内(card 内的 <li> 保持原样)。"""
    depth = 0
    for ln in res:
        depth += ln.count('<div class="card"')
        depth -= ln.count("</div>")
    return depth > 0


def render_page(md: str, title: str) -> str:
    body = convert(md)
    return (f"<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body><div class=wrap>{body}"
            f"<div class=foot>由 A股投资分析助手 · render-html skill 生成 · 仅供个人研究参考</div>"
            f"</div></body></html>")


def main() -> None:
    ap = argparse.ArgumentParser(description="Markdown → 富文本 HTML")
    ap.add_argument("input", help="输入 Markdown 文件")
    ap.add_argument("-o", "--output", help="输出 HTML(默认同名 .html)")
    ap.add_argument("--title", help="页面标题(默认取文件名)")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"找不到输入文件:{src}")
    md = src.read_text(encoding="utf-8")
    title = args.title or src.stem
    out = Path(args.output) if args.output else src.with_suffix(".html")
    out.write_text(render_page(md, title), encoding="utf-8")
    print(f"完成 → {out}")


if __name__ == "__main__":
    main()
