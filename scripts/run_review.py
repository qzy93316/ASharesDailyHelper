# -*- coding: utf-8 -*-
"""盘后复盘一键 —— 荐股维度 + 持仓维度**同时触发**,合成一份暗色图文 HTML。

用户说「复盘 / 盘后复盘」即跑此脚本(不必再单独说「诊断持仓」):
  ① review.py daily --date <date>   荐股维度:热点/全局/盘中三池,按荐股计划判对错(默认次日 hold=1)
  ② diagnose_portfolio.py           持仓维度:读 portfolio/ 最新持仓,按你的成本做健康度诊断(有持仓文件才跑)
  ③ review_to_html.py               两维度合成图文 HTML(复用盘前报告暗色主题,宽表)

两维度分开展示、互不污染:荐股维度=系统荐股决策对错;持仓维度=你手上的钱的健康度。
作战方案不在复盘出(留给次日盘前工作流结合最新消息面)。

用法:
  python scripts/run_review.py                 # 复盘今天
  python scripts/run_review.py 2026-07-06       # 指定复盘日期
  python scripts/run_review.py 2026-07-06 --hold 5   # 波段视角
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PY = sys.executable


def _run(desc, args) -> int:
    print(f"\n{'='*54}\n▶ {desc}\n{'='*54}")
    r = subprocess.run([PY, "-X", "utf8"] + args, cwd=str(ROOT))
    if r.returncode != 0:
        print(f"⚠️ {desc} 返回码 {r.returncode}(继续后续步骤)")
    return r.returncode


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    date = argv[0] if argv else dt.date.today().isoformat()
    hold = "1"
    if "--hold" in sys.argv:
        try:
            hold = sys.argv[sys.argv.index("--hold") + 1]
        except (IndexError, ValueError):
            pass

    # ① 荐股维度(三池)
    _run("[1/3] 荐股维度复盘 · review.py(热点/全局/盘中三池,按计划判对错)",
         ["skills/backtest-review/scripts/review.py", "daily", "--date", date, "--hold", hold])

    # ② 持仓维度(有持仓文件才跑;诊断始终针对"当前持仓",按 today 落盘)
    today = dt.date.today().isoformat()
    port_files = [f for f in (ROOT / "portfolio").glob("*.*")
                  if f.suffix.lower() in (".xls", ".xlsx", ".csv")]
    diag_json = None
    if port_files:
        _run("[2/3] 持仓维度诊断 · diagnose_portfolio(读 portfolio/ 最新持仓,按成本诊断)",
             ["scripts/diagnose_portfolio.py"])
        cand = ROOT / "reports" / today.replace("-", "") / f"持仓诊断-{today}.json"
        diag_json = cand if cand.exists() else None
    else:
        print("\n[2/3] portfolio/ 无持仓文件,跳过持仓维度(仅出荐股维度)。")

    # ③ 合成图文 HTML(两模块)
    rv_json = ROOT / "reviews" / f"复盘-{date}.json"
    out_html = ROOT / "reviews" / f"复盘-{date}.html"
    if not rv_json.exists():
        print(f"\n⚠️ 找不到 {rv_json},无法渲染。请检查上面步骤日志。")
        return
    render_args = ["skills/render-html/scripts/review_to_html.py", str(rv_json), "-o", str(out_html)]
    if diag_json:
        render_args += ["--diag", str(diag_json)]
    _run("[3/3] 合成复盘图文 HTML(荐股维度 + 持仓维度)", render_args)

    dim = "荐股维度 + 持仓维度" if diag_json else "仅荐股维度"
    print(f"\n{'='*54}\n✅ 盘后复盘完成({dim})\n   报告:{out_html}\n{'='*54}")


if __name__ == "__main__":
    main()
