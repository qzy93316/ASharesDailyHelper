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


# 三池检验:热点池(日报)/ 全局池 / 盘中机会池。三池都要纳入决策正确性检验。
POOL_FILES = [
    ("热点池", "日报-{d}.json"),
    ("全局池", "全局池-{d}.json"),
    ("盘中池", "盘中机会池-{d}.json"),
]


def _read_json(date: str, fname: str) -> dict | None:
    # 优先日期文件夹 reports/YYYYMMDD/,兼容旧的扁平路径
    f = REPORTS / date.replace("-", "") / fname.format(d=date)
    if not f.exists():
        f = REPORTS / fname.format(d=date)
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def _normalize_intraday(p: dict, sidecar: dict) -> dict:
    """盘中机会池 pick 归一到 evaluate_pick 所需口径 —— 它只有 close/soft_stop,
    没有 entry_close/plan_target;目标价按 params.target_gain_pct(超短线止盈)反推。"""
    q = dict(p)
    close = float(p.get("close") or (p.get("indicators") or {}).get("close") or 0)
    tgt_pct = float((sidecar.get("params") or {}).get("target_gain_pct", 5))
    q.setdefault("entry_close", close)
    q.setdefault("plan_stop", p.get("soft_stop"))
    q.setdefault("plan_target", round(close * (1 + tgt_pct / 100), 2) if close else None)
    q.setdefault("entry_date", str(sidecar.get("date", "")))
    q.setdefault("strategy", "超短线")
    return q


def _collect_picks(date: str) -> tuple[list[dict], str | None]:
    """汇总三池荐股,各自打上 pool 标签(荐股维度检验的输入)。返回 (picks, 情绪阶段)。"""
    picks, phase = [], None
    for pool, fname in POOL_FILES:
        data = _read_json(date, fname)
        if not data:
            continue
        if pool == "热点池":
            phase = (data.get("emotion") or {}).get("phase")
        for p in data.get("picks", []):
            q = _normalize_intraday(p, data) if pool == "盘中池" else dict(p)
            q["pool"] = pool
            q.setdefault("entry_date", date)
            picks.append(q)
    return picks, phase


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
    entry = float(pick.get("entry_close") or 0)
    _s, _t = pick.get("plan_stop"), pick.get("plan_target")
    stop = float(_s) if _s is not None else None
    target = float(_t) if _t is not None else None
    # 记录因子标签(影子验证:哪些因子命中,用于事后按因子分组统计胜率)
    factor_keys = list((pick.get("factors") or {}).keys())
    if pick.get("vcp"):
        factor_keys.append("vcp_hit")
    if pick.get("accumulating"):
        factor_keys.append("accumulating")
    base = {"code": code, "name": name, "signal": pick.get("signal", ""),
            "score": pick.get("score"), "entry": entry, "stop": stop, "target": target,
            "pool": pick.get("pool", "?"),
            "strategy": pick.get("strategy", ""), "shadow": pick.get("shadow", False),
            "factor_keys": factor_keys}
    fut = _bars_after(code, pick["entry_date"], hold)
    if fut is None or len(fut) == 0 or not entry:
        return {**base, "outcome": "待验证", "success": None, "pending": True}

    highs = [float(x) for x in fut["最高"]]
    lows = [float(x) for x in fut["最低"]]
    closes = [float(x) for x in fut["收盘"]]
    # 逐日判定首次触及目标/止损(同日都触及,保守判为先触止损);
    # 无计划价位(如"观察"类)则跳过触线判定,只按收益定小胜/小负。
    first = None
    if stop is not None and target is not None:
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
                existing[(r["entry_date"], r["code"], r.get("pool", "?"))] = r
    for r in rows:
        if not r.get("pending"):
            existing[(r["entry_date"], r["code"], r.get("pool", "?"))] = r
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


def _by_pool(rows: list[dict]) -> dict:
    """按池分组命中率(热点池/全局池/盘中池),看哪个池的决策更靠谱。"""
    g = defaultdict(list)
    for r in rows:
        g[r.get("pool", "?")].append(r)
    out = {}
    for k, rs in g.items():
        d = [x for x in rs if not x.get("pending")]
        out[k] = {"n": len(d),
                  "win_rate_pct": round(sum(1 for x in d if x["success"]) / len(d) * 100, 1) if d else None,
                  "pending": sum(1 for x in rs if x.get("pending"))}
    return out


def _write_md(title: str, rows: list[dict], summ: dict, hold: int) -> Path:
    REVIEWS.mkdir(parents=True, exist_ok=True)
    span = "次日" if hold == 1 else f"{hold} 个交易日"
    L = [f"# {title}", "",
         f"> **荐股维度**复盘(热点池/全局池/盘中池三池合验)· 持有期 {span} · "
         f"判定:先触目标=胜/先触止损=败/无计划价或未触线则看{span}收盘正负 · 仅供研究复盘",
         "> 本复盘只检验**荐股当初的计划**(建仓价/目标/止损)对不对,与你的实际持仓成本无关;"
         "**持仓健康度是另一维度**,由 diagnose_portfolio 按你的成本单独诊断。二者分开看。", ""]
    if summ.get("evaluated"):
        L += ["## 汇总", "",
              f"- 已验证 **{summ['evaluated']}** 只(待验证 {summ.get('pending',0)} 只)",
              f"- **命中率 {summ['win_rate_pct']}%**(达标 {summ['hit_target']} · 止损 {summ['hit_stop']})",
              f"- {span}平均收益 {summ['avg_ret_hold']}% · 平均最大浮盈 {summ['avg_max_gain']}% · 平均最大浮亏 {summ['avg_max_dd']}%",
              ""]
        bp = _by_pool(rows)
        if len([k for k in bp if k != "?"]) > 1:
            L += ["**分池命中率**:" + " · ".join(
                f"{k} {v['win_rate_pct']}%({v['n']}只)" for k, v in bp.items() if v['n']), ""]
    else:
        L += ["## 汇总", "", "本期暂无可验证标的(荐股日之后尚无足够交易日)。", ""]
    L += ["## 逐只明细", "",
          "| 日期 | 池 | 名称 | 代码 | 信号 | 评分 | 建仓价 | 次日% | 持有期% | 最大浮盈% | 最大浮亏% | 结果 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        pool = r.get("pool", "?")
        if r.get("pending"):
            L.append(f"| {r.get('entry_date','')} | {pool} | {r['name']} | {r['code']} | {r['signal']} | {r.get('score','')} | {r['entry']} | — | — | — | — | 待验证 |")
        else:
            L.append(f"| {r['entry_date']} | {pool} | {r['name']} | {r['code']} | {r['signal']} | {r['score']} | {r['entry']} | "
                     f"{r['ret_1']:+} | {r['ret_hold']:+} | {r['max_gain']:+} | {r['max_dd']:+} | {r['outcome']} |")
    out = REVIEWS / f"{title}.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


def cmd_daily(args) -> None:
    picks, phase = _collect_picks(args.date)
    if not picks:
        print(f"找不到 {args.date} 的任一池侧车(日报/全局池/盘中机会池),先跑对应脚本。"); return
    rows = []
    for p in picks:
        r = evaluate_pick(p, args.hold)
        r.setdefault("entry_date", p.get("entry_date", args.date))
        r["emotion_phase"] = phase
        r["pool"] = p.get("pool", "?")
        rows.append(r)
    summ = _summary(rows)
    _append_ledger(rows)
    out = _write_md(f"复盘-{args.date}", rows, summ, args.hold)
    print(json.dumps({"summary": summ, "by_pool": _by_pool(rows), "md": str(out)},
                     ensure_ascii=False, indent=2))


def cmd_range(args) -> None:
    d0 = datetime.strptime(args.since, "%Y-%m-%d").date()
    d1 = datetime.strptime(args.until, "%Y-%m-%d").date()
    rows = []
    d = d0
    while d <= d1:
        picks, phase = _collect_picks(d.isoformat())
        for p in picks:
            r = evaluate_pick(p, args.hold)
            r.setdefault("entry_date", p.get("entry_date", d.isoformat()))
            r["emotion_phase"] = phase
            r["pool"] = p.get("pool", "?")
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
    by_strategy, by_factor, by_phase, by_pool = (defaultdict(list), defaultdict(list),
                                                 defaultdict(list), defaultdict(list))
    for r in rows:
        by_strategy[r.get("strategy") or "?"].append(r)
        by_phase[r.get("emotion_phase") or "?"].append(r)  # 情绪周期分组(影子验证)
        by_pool[r.get("pool") or "?"].append(r)            # 分池命中率(热点/全局/盘中)
        for fk in (r.get("factor_keys") or []):
            by_factor[fk].append(r)
    print(json.dumps({
        "overall": overall,
        "by_pool": {k: wr(v) for k, v in by_pool.items()},
        "by_signal": {k: wr(v) for k, v in by_signal.items()},
        "by_score": {k: wr(v) for k, v in by_score.items()},
        "by_strategy": {k: wr(v) for k, v in by_strategy.items()},
        "by_factor": {k: wr(v) for k, v in by_factor.items()},
        "by_emotion_phase": {k: wr(v) for k, v in by_phase.items()},
    }, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="盘后/周度复盘验证")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("daily"); a.add_argument("--date", required=True); a.add_argument("--hold", type=int, default=1); a.set_defaults(func=cmd_daily)
    b = sub.add_parser("range"); b.add_argument("--since", required=True); b.add_argument("--until", required=True); b.add_argument("--hold", type=int, default=1); b.set_defaults(func=cmd_range)
    c = sub.add_parser("stats"); c.set_defaults(func=cmd_stats)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
