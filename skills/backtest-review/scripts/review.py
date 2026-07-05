# -*- coding: utf-8 -*-
"""盘后/周度复盘验证 —— 用荐股池的计划(目标/止损)对照盘后真实走向,判定命中,
累积历史成功率。评估口径借鉴 a-share-paper-trading 的成交判定(见调研):
先触目标=胜、先触止损=败、都没碰则按持有期收盘收益定小胜/小负。

数据复用主项目 scripts/fetcher.py 的韧性数据层(缓存+双源),复盘用的后续K线
即来自 get_kline 返回的近端历史。

子命令:
  daily   --date YYYY-MM-DD [--hold 5]   评估某日荐股池,写复盘MD+累积台账
  range   --since D --until D [--hold 5]  评估区间(周复盘),汇总命中率
  stats                                   读累积台账,输出历史成功率(总/按信号/按评分档)
输出:reviews/复盘-*.md,累积台账 reviews/review_ledger.jsonl
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import fetcher  # noqa: E402

REPORTS = ROOT / "reports"
REVIEWS = ROOT / "reviews"
LEDGER = REVIEWS / "review_ledger.jsonl"


def _load_sidecar(date: str) -> dict | None:
    # 优先日期文件夹 reports/YYYYMMDD/,兼容旧的扁平路径
    f = REPORTS / date.replace("-", "") / f"日报-{date}.json"
    if not f.exists():
        f = REPORTS / f"日报-{date}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def _bars_after(code: str, entry_date: str, hold: int):
    """取入场日之后最多 hold 根K线(用主项目数据层,含缓存/双源)。"""
    df = fetcher.get_kline(code, days=max(hold + 10, 40))
    dates = [str(d) for d in df["日期"].tolist()]
    if entry_date not in dates:
        return None
    i = dates.index(entry_date)
    future = df.iloc[i + 1:i + 1 + hold]
    return future


def evaluate_pick(pick: dict, hold: int) -> dict:
    """对单只荐股做盘后判定。返回含命中结果与收益指标的 dict(future 不足则 pending)。"""
    code, name = pick["code"], pick["name"]
    entry = float(pick["entry_close"])
    stop, target = float(pick["plan_stop"]), float(pick["plan_target"])
    # 记录因子标签(影子验证:哪些因子命中,用于事后按因子分组统计胜率)
    factor_keys = list((pick.get("factors") or {}).keys())
    if pick.get("vcp"):
        factor_keys.append("vcp_hit")
    if pick.get("accumulating"):
        factor_keys.append("accumulating")
    base = {"code": code, "name": name, "signal": pick.get("signal", ""),
            "score": pick.get("score"), "entry": entry, "stop": stop, "target": target,
            "strategy": pick.get("strategy", ""), "shadow": pick.get("shadow", False),
            "factor_keys": factor_keys}
    fut = _bars_after(code, pick["entry_date"], hold)
    if fut is None or len(fut) == 0:
        return {**base, "outcome": "待验证", "success": None, "pending": True}

    highs = [float(x) for x in fut["最高"]]
    lows = [float(x) for x in fut["最低"]]
    closes = [float(x) for x in fut["收盘"]]
    # 逐日判定首次触及目标/止损(同日都触及,保守判为先触止损)
    first = None
    for h, lo in zip(highs, lows):
        if lo <= stop:
            first = "止损"; break
        if h >= target:
            first = "达标"; break
    ret_1 = round((closes[0] / entry - 1) * 100, 2)
    ret_hold = round((closes[-1] / entry - 1) * 100, 2)
    max_gain = round((max(highs) / entry - 1) * 100, 2)
    max_dd = round((min(lows) / entry - 1) * 100, 2)

    if first == "达标":
        outcome, success = "达标(先触目标)", True
    elif first == "止损":
        outcome, success = "止损(先触止损)", False
    else:
        outcome, success = ("持有小胜", True) if ret_hold >= 0 else ("持有小负", False)
    return {**base, "entry_date": pick["entry_date"], "hold_used": len(fut),
            "ret_1": ret_1, "ret_hold": ret_hold, "max_gain": max_gain, "max_dd": max_dd,
            "outcome": outcome, "success": success, "pending": False}


def _append_ledger(rows: list[dict]) -> None:
    """写入累积台账(按 日期+代码 去重覆盖)。"""
    REVIEWS.mkdir(parents=True, exist_ok=True)
    existing = {}
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                existing[(r["entry_date"], r["code"])] = r
    for r in rows:
        if not r.get("pending"):
            existing[(r["entry_date"], r["code"])] = r
    with LEDGER.open("w", encoding="utf-8") as f:
        for r in existing.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _summary(rows: list[dict]) -> dict:
    done = [r for r in rows if not r.get("pending")]
    if not done:
        return {"evaluated": 0}
    wins = [r for r in done if r["success"]]
    return {
        "evaluated": len(done),
        "pending": sum(1 for r in rows if r.get("pending")),
        "win_rate_pct": round(len(wins) / len(done) * 100, 1),
        "avg_ret_hold": round(sum(r["ret_hold"] for r in done) / len(done), 2),
        "avg_max_gain": round(sum(r["max_gain"] for r in done) / len(done), 2),
        "avg_max_dd": round(sum(r["max_dd"] for r in done) / len(done), 2),
        "hit_target": sum(1 for r in done if "达标" in r["outcome"]),
        "hit_stop": sum(1 for r in done if "止损" in r["outcome"]),
    }


def _write_md(title: str, rows: list[dict], summ: dict, hold: int) -> Path:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    L = [f"# {title}", "",
         f"> 持有期 {hold} 个交易日 · 判定:先触目标=胜/先触止损=败/否则看持有期收盘 · 仅供研究复盘", ""]
    if summ.get("evaluated"):
        L += ["## 汇总", "",
              f"- 已验证 **{summ['evaluated']}** 只(待验证 {summ.get('pending',0)} 只)",
              f"- **命中率 {summ['win_rate_pct']}%**(达标 {summ['hit_target']} · 止损 {summ['hit_stop']})",
              f"- 持有期平均收益 {summ['avg_ret_hold']}% · 平均最大浮盈 {summ['avg_max_gain']}% · 平均最大浮亏 {summ['avg_max_dd']}%", ""]
    else:
        L += ["## 汇总", "", "本期暂无可验证标的(荐股日之后尚无足够交易日)。", ""]
    L += ["## 逐只明细", "",
          "| 日期 | 名称 | 代码 | 信号 | 评分 | 现价 | 次日% | 持有期% | 最大浮盈% | 最大浮亏% | 结果 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("pending"):
            L.append(f"| {r.get('entry_date','')} | {r['name']} | {r['code']} | {r['signal']} | {r.get('score','')} | {r['entry']} | — | — | — | — | 待验证 |")
        else:
            L.append(f"| {r['entry_date']} | {r['name']} | {r['code']} | {r['signal']} | {r['score']} | {r['entry']} | "
                     f"{r['ret_1']:+} | {r['ret_hold']:+} | {r['max_gain']:+} | {r['max_dd']:+} | {r['outcome']} |")
    out = REVIEWS / f"{title}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def cmd_daily(args) -> None:
    data = _load_sidecar(args.date)
    if not data:
        print(f"找不到 {args.date} 的侧车 JSON(reports/日报-{args.date}.json),先跑 daily_report。"); return
    rows = [evaluate_pick(p, args.hold) for p in data.get("picks", [])]
    for r in rows:
        r.setdefault("entry_date", args.date)
    summ = _summary(rows)
    _append_ledger(rows)
    out = _write_md(f"复盘-{args.date}", rows, summ, args.hold)
    print(json.dumps({"summary": summ, "md": str(out)}, ensure_ascii=False, indent=2))


def cmd_range(args) -> None:
    d0 = datetime.strptime(args.since, "%Y-%m-%d").date()
    d1 = datetime.strptime(args.until, "%Y-%m-%d").date()
    rows = []
    d = d0
    while d <= d1:
        data = _load_sidecar(d.isoformat())
        if data:
            for p in data.get("picks", []):
                r = evaluate_pick(p, args.hold)
                r.setdefault("entry_date", d.isoformat())
                rows.append(r)
        d += timedelta(days=1)
    summ = _summary(rows)
    _append_ledger(rows)
    out = _write_md(f"周复盘-{args.since}_{args.until}", rows, summ, args.hold)
    print(json.dumps({"summary": summ, "md": str(out)}, ensure_ascii=False, indent=2))


def cmd_stats(args) -> None:
    if not LEDGER.exists():
        print(json.dumps({"note": "累积台账为空,先跑 daily/range 复盘"}, ensure_ascii=False)); return
    rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    overall = _summary(rows)
    by_signal, by_score = defaultdict(list), defaultdict(list)
    for r in rows:
        by_signal[r.get("signal", "?")].append(r)
        s = r.get("score") or 0
        bucket = "≥75" if s >= 75 else "60-74" if s >= 60 else "<60"
        by_score[bucket].append(r)
    def wr(rs):
        d = [x for x in rs if not x.get("pending")]
        return {"n": len(d), "win_rate_pct": round(sum(1 for x in d if x["success"]) / len(d) * 100, 1) if d else None}
    # 影子验证:按 策略路 / 单个因子 分组统计命中率,判断哪个因子真有效
    by_strategy, by_factor = defaultdict(list), defaultdict(list)
    for r in rows:
        by_strategy[r.get("strategy") or "?"].append(r)
        for fk in (r.get("factor_keys") or []):
            by_factor[fk].append(r)
    print(json.dumps({
        "overall": overall,
        "by_signal": {k: wr(v) for k, v in by_signal.items()},
        "by_score": {k: wr(v) for k, v in by_score.items()},
        "by_strategy": {k: wr(v) for k, v in by_strategy.items()},
        "by_factor": {k: wr(v) for k, v in by_factor.items()},
    }, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="盘后/周度复盘验证")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("daily"); a.add_argument("--date", required=True); a.add_argument("--hold", type=int, default=5); a.set_defaults(func=cmd_daily)
    b = sub.add_parser("range"); b.add_argument("--since", required=True); b.add_argument("--until", required=True); b.add_argument("--hold", type=int, default=5); b.set_defaults(func=cmd_range)
    c = sub.add_parser("stats"); c.set_defaults(func=cmd_stats)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
