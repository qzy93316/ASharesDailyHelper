# -*- coding: utf-8 -*-
"""把写好的「消息面」Markdown 注入当日报告,替换占位符区块。

用法:
  python inject_news.py --date 2026-07-04 --news news.md
  python inject_news.py --report reports/日报-2026-07-04.md --news news.md
替换规则:定位 "## 三、消息面" 标题,用新内容替换其正文,直到下一个 "## " 标题为止。
"""
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def inject(report: Path, news_md: str) -> None:
    text = report.read_text(encoding="utf-8")
    lines = text.splitlines()
    # 找到消息面小节标题
    start = next((i for i, l in enumerate(lines) if l.startswith("##") and "消息面" in l), None)
    if start is None:
        raise SystemExit("报告中未找到「消息面」小节标题(## …消息面…)")
    # 找到下一个二级标题作为结束
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    new_block = ["## 三、消息面", "", news_md.strip(), ""]
    lines = lines[:start] + new_block + lines[end:]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"已注入消息面 → {report}")


def main() -> None:
    ap = argparse.ArgumentParser(description="注入消息面到日报")
    ap.add_argument("--date", help="YYYY-MM-DD,定位 reports/日报-DATE.md")
    ap.add_argument("--report", help="直接指定报告路径(优先于 --date)")
    ap.add_argument("--news", required=True, help="消息面 Markdown 文件")
    args = ap.parse_args()
    if args.report:
        report = Path(args.report)
    else:
        # 优先日期文件夹 reports/YYYYMMDD/,兼容旧扁平路径
        report = ROOT / "reports" / args.date.replace("-", "") / f"日报-{args.date}.md"
        if not report.exists():
            report = ROOT / "reports" / f"日报-{args.date}.md"
    if not report.exists():
        raise SystemExit(f"找不到报告:{report}")
    news = Path(args.news)
    if not news.exists():
        raise SystemExit(f"找不到消息面文件:{news}")
    inject(report, news.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
