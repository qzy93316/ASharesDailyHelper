# -*- coding: utf-8 -*-
"""盘前工作流一键执行 —— 依次跑:热点池(daily_report)→ 全局池(global_scan)→ 合并渲染富HTML。
一条命令替代分步操作。

消息面前置(推荐,由 Claude 编排):对 Claude 说「跑盘前工作流」,它会:
  ① python scripts/run_workflow.py --pools-only   # 只跑两池,不渲染
  ② 联网搜当日政策/热点/公告 → 写 reports/YYYYMMDD/news.md(news-analysis skill)
  ③ python scripts/run_workflow.py                 # 检测到 news.md,渲染时自动并入
独立运行(无 Claude)也成立:没有 news.md 就渲染纯技术面版,事后补消息面再跑一次即可
(池数据当日已缓存,重跑只重渲染,秒级)。

用法:
  python scripts/run_workflow.py               # 用今天日期
  python scripts/run_workflow.py 2026-07-06    # 指定日期
  python scripts/run_workflow.py --pools-only  # 只跑热点池+全局池,跳过渲染(等消息面)
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PY = sys.executable


def _run(desc, args):
    print(f"\n{'='*54}\n▶ {desc}\n{'='*54}")
    r = subprocess.run([PY, "-X", "utf8"] + args, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"⚠️ {desc} 返回码 {r.returncode}(继续后续步骤)")
    return r.returncode


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pools_only = "--pools-only" in sys.argv
    date = args[0] if args else dt.date.today().isoformat()
    day = date.replace("-", "")
    day_dir = ROOT / "reports" / day

    _run("[1/3] 热点池 · daily_report(板块强弱 + 板块内相对强度)", ["scripts/daily_report.py"])
    _run("[2/3] 全局池 · global_scan(趋势追涨 + 超跌回调影子)", ["scripts/global_scan.py", "--date", date])

    daily_json = day_dir / f"日报-{date}.json"
    global_json = day_dir / f"全局池-{date}.json"
    out_html = day_dir / f"盘前报告-{date}.html"
    news_md = day_dir / "news.md"
    if pools_only:
        print(f"\n{'='*54}\n✅ 两池完成(--pools-only,未渲染)\n"
              f"   下一步:把消息面写入 {news_md},再跑本脚本渲染完整版。\n{'='*54}")
        return
    render_args = ["skills/render-html/scripts/report_to_html.py", str(daily_json), "-o", str(out_html)]
    if global_json.exists():
        render_args += ["--global", str(global_json)]
    if news_md.exists():
        render_args += ["--news", str(news_md)]
    if not daily_json.exists():
        print(f"\n⚠️ 找不到 {daily_json},无法渲染。请检查上面步骤日志。")
        return
    _run("[3/3] 合并渲染 · 盘前报告(大盘+板块强弱榜+今日之选+个股详析)", render_args)

    news_note = ("消息面已并入。" if news_md.exists()
                 else "本次为纯技术面版;对我说「今日热点」补消息面后会自动重渲染并入。")
    print(f"\n{'='*54}\n✅ 盘前工作流完成\n   报告:{out_html}\n   {news_note}\n{'='*54}")


if __name__ == "__main__":
    main()
