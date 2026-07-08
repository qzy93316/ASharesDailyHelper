# -*- coding: utf-8 -*-
"""信号有效性回测 —— 量化 signals.py 逐日买卖信号的预测力,反推优化研判权重。

思路(呼应「AI零计算」):signals.compute 已返回**全历史**信号点,一次 K线即得该股所有
信号 + 其前向数据。对每个信号量化各周期前向收益与胜率,按信号类型/多源共振聚合,
**只产出效力报告 + 权重调整建议(人工采纳),绝不自动写回 judge/scoring**。

命中口径:buy 信号 primary 周期前向收益 >0 为胜;sell 信号 <0 为胜(正确预警下跌)。
仅统计有足够前向K线的信号(避免序列末端幸存者偏差)。MVP 为**样本内**回测。

用法:
  python signal_backtest.py                         # 默认:我的持仓 + 近日两池 picks
  python signal_backtest.py --codes 603005,000100   # 指定股
  python signal_backtest.py --universe 200          # 全市场抽样(慢)
  python signal_backtest.py --days 250 --hold-days 5
输出:reviews/信号回测-YYYY-MM-DD.(md/json)
"""
import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "trading-memory" / "scripts"))
import signals  # noqa: E402

REVIEWS = ROOT / "reviews"
HORIZONS = (1, 3, 5, 10)


# ── 纯核心(离线可测):单只 bars → 逐信号前向收益 ──────────────────────
def backtest_bars(bars: list[dict], code: str = "", horizons=HORIZONS, primary: int = 5) -> list[dict]:
    """对单只标的的全历史信号做前向收益回测。返回逐信号明细(末端不足 horizon 的剔除)。"""
    if len(bars) < max(horizons) + 6:
        return []
    sig = signals.compute(signals.series_from_bars(bars))
    closes = [b["c"] for b in bars]
    idx = {b["d"]: i for i, b in enumerate(bars)}
    maxh = max(horizons)
    out = []
    for kind_key in ("macd", "kdj", "rsi", "vol"):
        for g in sig.get(kind_key, []):
            i = idx.get(g["d"])
            if i is None or i + maxh >= len(bars):   # 末端不足前向窗口 → 剔除
                continue
            c0 = closes[i]
            if not c0:
                continue
            fwd = {f"r{h}": round((closes[i + h] / c0 - 1) * 100, 2) for h in horizons}
            win = [b["h"] for b in bars[i + 1:i + 1 + primary]]
            los = [b["l"] for b in bars[i + 1:i + 1 + primary]]
            max_fav = round((max(win) / c0 - 1) * 100, 2) if win else 0.0
            max_adv = round((min(los) / c0 - 1) * 100, 2) if los else 0.0
            rp = fwd[f"r{primary}"]
            hit = (rp > 0) if g["dir"] == "buy" else (rp < 0)   # sell 正确预警下跌为胜
            out.append({"code": code, "d": g["d"], "kind": g.get("kind", kind_key),
                        "dir": g["dir"], "fwd": fwd, "max_fav": max_fav, "max_adv": max_adv,
                        "win": hit})
    return out


def _mark_resonance(rows: list[dict]) -> None:
    """同一 (code, d, dir) 若 ≥2 类信号同现 → 标为共振,用于验证'共振加分'前提。"""
    cnt = defaultdict(int)
    for r in rows:
        cnt[(r["code"], r["d"], r["dir"])] += 1
    for r in rows:
        r["resonant"] = cnt[(r["code"], r["d"], r["dir"])] >= 2


# ── 聚合与统计 ────────────────────────────────────────────────────────
def _stat(rows: list[dict], primary: int) -> dict:
    if not rows:
        return {"n": 0}
    n = len(rows)
    wins = sum(1 for r in rows if r["win"])
    avg = {f"r{h}": round(sum(r["fwd"][f"r{h}"] for r in rows) / n, 2) for h in HORIZONS}
    return {"n": n, "win_rate": round(wins / n * 100, 1),
            "avg_fwd": avg, "avg_max_fav": round(sum(r["max_fav"] for r in rows) / n, 2),
            "avg_max_adv": round(sum(r["max_adv"] for r in rows) / n, 2)}


def aggregate(rows: list[dict], primary: int = 5) -> dict:
    _mark_resonance(rows)
    by_kind = {}
    for k in sorted({r["kind"] for r in rows}):
        by_kind[k] = _stat([r for r in rows if r["kind"] == k], primary)
    reson = {"共振(≥2同向)": _stat([r for r in rows if r["resonant"]], primary),
             "单信号": _stat([r for r in rows if not r["resonant"]], primary)}
    return {"overall": _stat(rows, primary), "by_kind": by_kind, "resonance": reson}


def suggestions(agg: dict, primary: int) -> list[str]:
    """据 edge(胜率−50%、primary 周期平均前向收益方向)给权重建议(仅文字,不写回)。"""
    tips = []
    for k, s in agg["by_kind"].items():
        if not s.get("n"):
            continue
        wr, rp = s["win_rate"], s["avg_fwd"][f"r{primary}"]
        edge = round(wr - 50, 1)
        if s["n"] < 20:
            verdict = f"样本仅{s['n']}只,统计不足,暂存疑"
        elif wr >= 58:
            verdict = f"有效(胜率{wr}%/edge{edge:+}、{primary}日均{rp:+}%),建议维持或加权"
        elif wr <= 45:
            verdict = f"疑似反指/噪声(胜率{wr}%/edge{edge:+}),建议降权或仅作辅助"
        else:
            verdict = f"边际(胜率{wr}%/edge{edge:+}),辅助确认为宜、勿单独依赖"
        tips.append(f"**{k}**(n={s['n']}):{verdict}")
    r2, r1 = agg["resonance"]["共振(≥2同向)"], agg["resonance"]["单信号"]
    if r2.get("n") and r1.get("n"):
        diff = round(r2["win_rate"] - r1["win_rate"], 1)
        stance = ("支持现有共振加分(judge quality/scoring 信号确认)" if diff >= 5 else
                  "共振优势不明显,现有共振加分偏乐观,建议调低" if diff <= 0 else
                  "共振略优,现有加分幅度大体合理")
        tips.append(f"**多源共振 vs 单信号**:共振 {r2['win_rate']}%({r2['n']}) vs 单 "
                    f"{r1['win_rate']}%({r1['n']}),差 {diff:+}% → {stance}")
    return tips


# ── 回测范围解析(用户机可联网) ──────────────────────────────────────
def _bars_from_df(df) -> list[dict]:
    return [{"d": str(r["日期"]), "o": float(r["开盘"]), "c": float(r["收盘"]),
             "l": float(r["最低"]), "h": float(r["最高"]), "v": float(r["成交量"])}
            for _, r in df.iterrows()]


def resolve_universe(args) -> list[tuple[str, str]]:
    """返回 [(code,name)] 去重。默认:持仓 + 近 N 日两池 picks。"""
    seen, out = set(), []

    def add(code, name=""):
        code = "".join(ch for ch in str(code) if ch.isdigit()).zfill(6)
        if len(code) == 6 and code not in seen:
            seen.add(code)
            out.append((code, name or code))
    if args.codes:
        for c in args.codes.split(","):
            add(c.strip())
        return out
    if args.universe:
        import cache  # noqa: E402
        df, _ = cache.load_aged("universe_snapshot", 30)
        if df is not None and "代码" in df.columns:
            df = df.head(args.universe)
            for _, r in df.iterrows():
                add(r["代码"], str(r.get("名称", "")))
        return out
    # 默认:持仓(portfolio 最新文件)+ 未平仓台账 + 近 N 日两池 picks
    try:
        import diagnose_portfolio as dp
        pf = sorted((ROOT / "portfolio").glob("*.*"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
        pf = [f for f in pf if f.suffix.lower() in (".xls", ".xlsx", ".csv")]
        if pf:
            for h in dp.read_holdings(pf[0]):
                add(h["code"], h.get("name", ""))
    except Exception as e:  # noqa: BLE001
        print(f"  (读持仓文件跳过:{e})")
    try:
        import ledger
        _, pos = ledger._fifo_match(ledger._load())
        for code, p in pos.items():
            add(code, p.get("name", ""))
    except Exception as e:  # noqa: BLE001
        print(f"  (读台账跳过:{e})")
    # 近 N 日两池 picks
    days_back = args.pool_days
    today = dt.date.today()
    for i in range(days_back):
        d = (today - dt.timedelta(days=i)).isoformat()
        dd = d.replace("-", "")
        for fname in (f"日报-{d}.json", f"全局池-{d}.json"):
            f = ROOT / "reports" / dd / fname
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    for p in data.get("picks", []):
                        add(p.get("code", ""), p.get("name", ""))
                except Exception:  # noqa: BLE001
                    pass
    return out


def _render_md(date, agg, tips, codes_n, sig_n, primary, days) -> str:
    o = agg["overall"]
    L = [f"# 信号有效性回测 — {date}", "",
         f"> 回测 **{codes_n}** 只标的、**{sig_n}** 个信号点(每只近 {days} 根日K,样本内)。"
         f"命中口径:买入信号 {primary} 日前向收益>0 为胜、卖出信号<0 为胜。**仅供研究,权重建议需人工采纳。**", "",
         "## 总览", "",
         f"- 信号样本 **{o.get('n',0)}** · 总体胜率 **{o.get('win_rate','—')}%**", ""]
    if o.get("n"):
        L += ["## 分信号类型", "",
              f"| 信号 | 样本 | {primary}日胜率 | 1日 | 3日 | 5日 | 10日 | 均最大顺行 | 均最大逆行 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for k, s in agg["by_kind"].items():
            a = s["avg_fwd"]
            L.append(f"| {k} | {s['n']} | {s['win_rate']}% | {a['r1']:+}% | {a['r3']:+}% | "
                     f"{a['r5']:+}% | {a['r10']:+}% | {s['avg_max_fav']:+}% | {s['avg_max_adv']:+}% |")
        r2, r1 = agg["resonance"]["共振(≥2同向)"], agg["resonance"]["单信号"]
        L += ["", "## 多源共振 vs 单信号", "",
              f"- 共振(≥2同向):样本 {r2.get('n',0)} · 胜率 {r2.get('win_rate','—')}%",
              f"- 单信号:样本 {r1.get('n',0)} · 胜率 {r1.get('win_rate','—')}%", ""]
    L += ["## 权重调整建议(人工采纳,未自动写回)", ""]
    L += [f"- {t}" for t in tips] or ["- (样本不足,暂无建议)"]
    L += ["", "---",
          "*样本内回测:信号与前向收益取自同一段历史,存在过拟合可能;真正的样本外验证需逐日落盘信号后跨时间累积。"
          "由代码计算,AI 零计算,仅供研究复盘。*"]
    return "\n".join(L)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="信号有效性回测(反推优化研判权重)")
    ap.add_argument("--codes", help="指定股,逗号分隔,如 603005,000100")
    ap.add_argument("--universe", type=int, help="从全市场清单抽样前 N 只")
    ap.add_argument("--days", type=int, default=250, help="每只回看K线根数(默认250)")
    ap.add_argument("--hold-days", dest="hold", type=int, default=5, help="primary 前向周期(默认5)")
    ap.add_argument("--pool-days", dest="pool_days", type=int, default=10, help="默认范围回看几日两池(默认10)")
    args = ap.parse_args()

    import fetcher  # noqa: E402
    uni = resolve_universe(args)
    if not uni:
        print("回测范围为空:请放持仓文件、先跑过盘前两池,或用 --codes/--universe 指定。"); return
    print(f"回测 {len(uni)} 只标的(primary={args.hold}日,回看{args.days}根)...")
    all_rows = []
    for code, name in uni:
        try:
            k = fetcher.get_kline(code, args.days)
            if k is None or len(k) < max(HORIZONS) + 20:
                continue
            all_rows += backtest_bars(_bars_from_df(k), code, primary=args.hold)
        except Exception as e:  # noqa: BLE001
            print(f"  {code} 跳过:{e}")
    if not all_rows:
        print("未采集到有效信号样本(标的太少或K线不足)。"); return
    agg = aggregate(all_rows, primary=args.hold)
    tips = suggestions(agg, args.hold)
    today = dt.date.today()
    REVIEWS.mkdir(parents=True, exist_ok=True)
    md = _render_md(today, agg, tips, len(uni), len(all_rows), args.hold, args.days)
    (REVIEWS / f"信号回测-{today}.md").write_text(md, encoding="utf-8")
    (REVIEWS / f"信号回测-{today}.json").write_text(
        json.dumps({"date": str(today), "codes": len(uni), "signals": len(all_rows),
                    "primary": args.hold, "agg": agg, "suggestions": tips},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成 → {REVIEWS / f'信号回测-{today}.md'}")
    o = agg["overall"]
    print(f"信号样本 {o['n']} · 总体胜率 {o['win_rate']}%")
    for k, s in agg["by_kind"].items():
        print(f"  {k}: n={s['n']} 胜率{s['win_rate']}% {args.hold}日均{s['avg_fwd'][f'r{args.hold}']:+}%")
    for t in tips:
        print("  · " + t.replace("**", ""))


if __name__ == "__main__":
    main()
