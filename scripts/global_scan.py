# -*- coding: utf-8 -*-
"""全局上升趋势选股 —— 不局限于热点板块,做全市场维度扫描,
补 daily_report(热点板块×N只)可能漏掉的"冷门板块强势股"。

流程:全市场快照(东财→新浪降级)→ 流动性+当日涨幅预筛 → 对候选拉K线算真实趋势
→ 多头排列且评分达标者入"全局池" → 与当日热点荐股去重。

用法:python global_scan.py [--cap 60] [--date YYYY-MM-DD]
输出:reports/全局池-YYYY-MM-DD.json(+ 文本摘要);可交给 render-html 出图表版。
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml
import akshare as ak

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402
import cache  # noqa: E402
import chips  # noqa: E402
import chan  # noqa: E402
import judge  # noqa: E402
import candle_patterns  # noqa: E402
import factors  # noqa: E402
import signals  # noqa: E402  逐日买卖信号(反哺研判)
from indicators import compute_indicators  # noqa: E402
from scoring import score_stock  # noqa: E402

ROOT = Path(__file__).parent.parent


def _fetch_universe():
    """实时拉全市场快照。东财 spot(字段全,含60日涨跌幅)→ 新浪 spot 降级。"""
    try:
        df = fetcher._call(ak.stock_zh_a_spot_em)
        df["_全字段"] = True
        return df
    except Exception as e:  # noqa: BLE001
        print(f"  [快照降级] 东财 spot 失败,切新浪 —— {e}")
        df = fetcher._call(ak.stock_zh_a_spot)
        ren = {"symbol": "代码", "code": "代码", "name": "名称", "trade": "最新价",
               "changepercent": "涨跌幅", "turnoverratio": "换手率", "amount": "成交额"}
        df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
        df["_全字段"] = False
        return df


def _universe(ttl_days: int):
    """全市场快照(带 TTL 缓存):清单落库,ttl_days 内直接用缓存,避免每次重拉 5000 只。"""
    df, fdate = cache.load_aged("universe_snapshot", ttl_days)
    if df is not None:
        print(f"  [清单缓存] 复用 {fdate} 的全市场快照({len(df)} 只),未重拉")
        return df, bool(df["_全字段"].iloc[0]) if "_全字段" in df else False
    df = _fetch_universe()
    cache.save("universe_snapshot", df)
    return df, bool(df["_全字段"].iloc[0]) if "_全字段" in df else False


def _is_excluded(name: str, code: str, fl: dict) -> bool:
    if fl.get("exclude_st") and "ST" in str(name).upper():
        return True
    if fl.get("exclude_bse") and str(code)[:2] in ("92", "83", "87", "88", "43", "82"):
        return True
    if fl.get("exclude_chinext") and str(code)[:3] in ("300", "301"):  # 创业板
        return True
    if fl.get("exclude_star") and str(code)[:3] in ("688", "689"):  # 科创板
        return True
    return False


def _prefilter(df, has_60d: bool, fl: dict, cap: int, min_60d: float):
    """双入口预筛,避免"成交额top榜天然只有强势股"把超跌股挡在门外:
    - 趋势候选:按成交额取前 cap(流动性好的活跃股);
    - 超跌候选:在"有足够流动性"的股里,按当日跌幅最深取前 cap(超跌股专属入口)。
    合并去重返回,后续再由分类逻辑分到两路。"""
    import pandas as pd
    df = df.copy()
    df["代码"] = df["代码"].astype(str).str.extract(r"(\d{6})")[0]
    df = df.dropna(subset=["代码"])
    for col in ("涨跌幅", "成交额", "换手率"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[~df.apply(lambda r: _is_excluded(r.get("名称", ""), r["代码"], fl), axis=1)]
    if "涨跌幅" in df:
        df = df[df["涨跌幅"].abs() < 9.8]  # 剔涨跌停
    if "成交额" not in df:
        return df.head(cap)
    df = df.sort_values("成交额", ascending=False)
    trend_cand = df.head(cap)
    # 超跌入口:成交额前 3×cap(保证流动性)里,取当日跌得最多的 cap 只
    liquid = df.head(cap * 3)
    os_cand = liquid.sort_values("涨跌幅").head(cap) if "涨跌幅" in liquid else liquid.head(0)
    merged = pd.concat([trend_cand, os_cand]).drop_duplicates(subset="代码")
    return merged


def main() -> None:
    ap = argparse.ArgumentParser(description="全局上升趋势选股")
    ap.add_argument("--cap", type=int, help="预筛后拉K线的候选上限(覆盖 config)")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rp, fl, hr = cfg["report"], cfg["filters"], cfg["hard_rules"]
    gs = cfg.get("global_scan", {})
    cap = args.cap or int(gs.get("scan_cap", 60))
    min_60d = float(gs.get("min_60d_return", 10))
    min_score = int(gs.get("min_score", rp["min_score"]))
    dd = cfg["style"]["max_drawdown_pct"]

    day_dir = ROOT / "reports" / args.date.replace("-", "")
    day_dir.mkdir(parents=True, exist_ok=True)
    # 当日热点池代码(去重用):优先日期文件夹,兼容旧的扁平路径
    hot_codes = set()
    side = day_dir / f"日报-{args.date}.json"
    if not side.exists():
        side = ROOT / "reports" / f"日报-{args.date}.json"
    if side.exists():
        hot_codes = {p["code"] for p in json.loads(side.read_text(encoding="utf-8")).get("picks", [])}

    # 大盘环境总开关:动态收紧
    regime = fetcher.get_index_regime()
    eff_min_score = min_score + regime.get("score_delta", 0)
    cap_each = max(1, round(int(gs.get("max_picks", 8)) * regime.get("picks_factor", 1.0)))
    oversold_ok = regime.get("oversold_ok", True)
    oversold_strict = regime.get("oversold_strict", False)  # 防守档:超跌路仅收最强企稳信号
    rps_min = float(gs.get("rps_min", 85))
    print(f"[环境] 大盘 {regime.get('level')}(上证{regime.get('close')} MA20={regime.get('ma20')} "
          f"MA60={regime.get('ma60')}) → 趋势门槛{eff_min_score}分、每路上限{cap_each}、"
          f"超跌路{'严格影子(仅底背离)' if oversold_strict else '开'}")

    print("[1/3] 拉全市场快照(带TTL缓存)...")
    df, has_60d = _universe(int(gs.get("universe_ttl_days", 5)))
    cand = _prefilter(df, has_60d, fl, cap, min_60d)
    os_rsi = float(gs.get("oversold_rsi", 35))
    os_bias = float(gs.get("oversold_bias", -8))
    # RPS 相对强度:全市场 60 日涨幅百分位(仅东财 spot 有 60日涨跌幅字段)
    rps_map = {}
    if has_60d and "60日涨跌幅" in df.columns:
        import pandas as pd
        allret = pd.to_numeric(df["60日涨跌幅"], errors="coerce")
        pct = allret.rank(pct=True) * 100
        for c, r in zip(df["代码"].astype(str).str.extract(r"(\d{6})")[0], pct):
            if isinstance(c, str):
                rps_map[c] = None if pd.isna(r) else round(float(r), 1)
    # 第三梯队:事件黑名单(盘前拉一次全市场,解禁/业绩雷剔除)
    blacklist = factors.event_blacklist(int(gs.get("event_days_ahead", 14)),
                                        float(gs.get("unlock_ratio_pct", 3.0))) if gs.get("event_filter", True) else {}
    print(f"[事件] 黑名单 {len(blacklist)} 只(解禁/业绩雷),命中将剔除")
    print(f"[2/3] 预筛得 {len(cand)} 只候选(全字段={has_60d},RPS={'启用' if rps_map else '不可用→用60日涨幅代理'}),"
          f"双路:趋势追涨 / 超跌回调(影子)...")

    def _zt_gene(bars) -> int:
        """近60日涨停次数(涨跌幅≥9.7%),涨停基因:衡量短线爆发力与资金关注。"""
        n = 0
        for i in range(1, len(bars)):
            if bars[i - 1]["c"] and bars[i]["c"] / bars[i - 1]["c"] - 1 >= 0.097:
                n += 1
        return n

    def _mk(code, name, ind, sc, bars, kind, stop, target, shadow=False, extra=None):
        chip = chips.compute_chips(bars)
        flow = fetcher.get_fund_flow(code)
        chan_res = chan.analyze(bars)
        turn_pct = round((bars[-1].get("换手", 0) or 0) * 100, 2)
        sig = signals.compute(signals.series_from_bars(bars))       # 逐日信号(图上箭头 + 反哺研判)
        sig_sum = signals.latest_summary(sig, [b["d"] for b in bars])
        jd = judge.synthesize(ind, chip, judge.flow_sum(flow), chan_res,
                              round(target, 2), dd, turn_pct, strategy=kind, sig=sig_sum)
        import emotion as _emo
        import fundamental as _fnd
        p = {"code": code, "name": name, "sector": f"全局·{kind}", "sector_pct": 0.0,
             "stock_emotion": _emo.stock_heat(code, bars),
             "fundamental": _fnd.summarize(code),
             "signal": sc["signal"], "score": sc["total"], "breakdown": sc["breakdown"],
             "entry_date": ind["date"], "entry_close": ind["close"],
             "plan_stop": jd["structural_stop"]["stop"], "plan_target": round(target, 2),
             "indicators": ind, "bars": bars, "chips": chip, "signals": sig, "signal_summary": sig_sum,
             "chip_comment": chips.control_comment(chip),
             "fund_flow": flow, "judge": jd, "strategy": kind, "shadow": shadow}
        p.update(extra or {})
        return p

    trend, oversold, dropped = [], [], 0
    for _, row in cand.iterrows():
        code, name = str(row["代码"]).zfill(6), row.get("名称", row["代码"])
        if code in hot_codes:
            continue
        if code in blacklist:  # 第三梯队:事件黑名单直接剔除
            dropped += 1
            continue
        try:
            k = fetcher.get_kline(code, rp["kline_days"])
            if fl.get("exclude_sub_new") and len(k) < 60:
                continue
            fresh, _ = fetcher.check_freshness(k, cfg["data"]["freshness_max_age_days"])
            if not fresh:
                continue
            ind = compute_indicators(k)
            sc = score_stock(ind, hr)
            kk = k.tail(int(rp.get("chart_bars", 120)))
            bars = [{"d": str(r["日期"]), "o": float(r["开盘"]), "c": float(r["收盘"]),
                     "l": float(r["最低"]), "h": float(r["最高"]), "v": float(r["成交量"]),
                     "换手": round(float(r.get("换手", 0) or 0), 5)} for _, r in kk.iterrows()]
            close, ma20 = ind["close"], ind["ma20"]
            rps = rps_map.get(code)
            zt = _zt_gene(bars[-60:])
            flow = fetcher.get_fund_flow(code)
            chip = chips.compute_chips(bars)
            turn_pct = round(bars[-1].get("换手", 0) * 100, 2)
            # ① 趋势追涨:多头/弱多 + 站上MA20 + 评分达标 + RPS≥85 + 涨停基因
            if ind["alignment"] in ("多头排列", "弱多") and close >= ma20 and sc["total"] >= eff_min_score and not sc["vetoes"]:
                if rps is not None and rps < rps_min:
                    continue
                # 第二梯队因子:筹码/资金/VCP/换手 → 分数调整 + 否决 + 记录
                is_bo = close >= max(b["h"] for b in bars[-20:-1])  # 突破近20日高
                cf = factors.chip_factor(chip, ind, is_bo)
                ff = factors.fund_continuity_factor(flow, ind)
                vf = factors.vcp_factor(bars)
                tf = factors.turnover_factor(turn_pct)
                fac = {"chip": cf, "fund": ff, "vcp": vf, "turnover": tf}
                if cf["veto"] or ff["reject"]:
                    dropped += 1
                    continue
                adj = cf["delta"] + ff["delta"] + vf["delta"] + tf["delta"]
                stop = max(ma20, round(close * (1 - dd / 100), 2))
                trend.append(_mk(code, name, ind, sc, bars, "趋势追涨", stop, ind["pressure"],
                                 extra={"rps": rps, "zt60": zt, "factor_adj": adj,
                                        "factors": {kk2: vv for kk2, vv in fac.items() if vv.get("note")},
                                        "vcp": vf["hit"], "accumulating": ff["accumulating"]}))
                continue
            # ② 超跌回调潜伏(影子运行):价在MA20下 + 超卖/深跌/近支撑 + 缩量 + 企稳信号
            #    企稳分强弱——强:底背离1B/见底K线组合;弱:仅今日收阳。
            #    防守档只收「强企稳」(严格影子),非防守档强弱皆可,不再一刀切关闭。
            if oversold_ok and close < ma20:
                near_sup = ind.get("support") and close <= ind["support"] * 1.05
                weak = (ind["rsi6"] <= os_rsi) or (ind["bias5"] <= os_bias) or near_sup
                shrink = (ind["vol_ratio"] < 0.9) or (ind["vol_pattern"] == "缩量回调")
                if weak and shrink:
                    res = chan.analyze(bars)
                    cndl = candle_patterns.detect(bars, 4)
                    bottom_candle = any(c["bias"] == "bull" for c in cndl)
                    has_1b = bool(res.get("divergence") and res["divergence"]["bs"] == "1B")
                    strong_stabil = has_1b or bottom_candle       # 强企稳信号
                    weak_stabil = bars[-1]["c"] >= bars[-1]["o"]   # 弱:今日收阳
                    ok = strong_stabil if oversold_strict else (strong_stabil or weak_stabil)
                    if ok:
                        ff = factors.fund_continuity_factor(flow, ind)  # 温和吸筹佐证
                        sig = "底背离1B" if has_1b else ("见底K线" if bottom_candle else "收阳企稳")
                        sup = ind.get("support") or close * 0.95
                        stop = round(min(sup * 0.98, close * (1 - dd / 100)), 2)
                        oversold.append(_mk(code, name, ind, sc, bars, "超跌回调", stop, ma20,
                                            shadow=True, extra={"rps": rps, "zt60": zt,
                                            "accumulating": ff["accumulating"], "stabil_signal": sig,
                                            "factors": {"fund": ff} if ff.get("note") else {}}))
        except Exception as e:  # noqa: BLE001
            print(f"  跳过 {name}({code}): {e}")
    print(f"  (因子/黑名单剔除 {dropped} 只)")
    # 趋势路排序:因子调整分优先(第二梯队权重),再涨停基因,再评分
    trend.sort(key=lambda p: (p.get("factor_adj", 0), p.get("zt60", 0), p["score"]), reverse=True)
    # 超跌路排序:强企稳信号优先(底背离>见底K线>收阳),同级越超卖越靠前
    _sig_rank = {"底背离1B": 0, "见底K线": 1, "收阳企稳": 2}
    oversold.sort(key=lambda p: (_sig_rank.get(p.get("stabil_signal"), 3), p["indicators"]["rsi6"]))
    picks = trend[:cap_each] + oversold[:cap_each]

    print(f"[3/3] 输出(趋势 {len(trend[:cap_each])} + 超跌影子 {len(oversold[:cap_each])})...")
    import emotion
    emo = emotion.latest_snapshot()  # 情绪周期(影子:只写侧车展示,不 gate)
    out = {"date": args.date, "source": "全局扫描", "scanned": len(cand),
           "indexes": fetcher.get_index_snapshot(), "sectors": [], "regime": regime,
           "emotion": emo, "picks": picks}
    f = day_dir / f"全局池-{args.date}.json"
    f.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 → {f}(入池 {len(picks)} 只)")
    for p in picks:
        i = p["indicators"]
        tag = "影子" if p.get("shadow") else "正式"
        print(f"  [{tag}] {p['strategy']} {p['name']}({p['code']}) {p['score']}分 现价{i['close']} "
              f"RPS={p.get('rps')} 涨停×{p.get('zt60')} RSI6={i['rsi6']} 乖离{i['bias5']}%")


if __name__ == "__main__":
    main()
