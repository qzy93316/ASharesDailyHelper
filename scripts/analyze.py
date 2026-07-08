# -*- coding: utf-8 -*-
"""个股即席分析 —— 对任意代码跑 指标+评分+筹码分布+资金流,产出与日报同构的侧车 JSON,
可直接交给 render-html 出图表版。补 daily_report(板块驱动)之外的"点名分析"。

用法:
  python analyze.py 603005=晶方科技 002756=永兴材料
  python analyze.py 603005            # 无名字则用代码占位
输出:reports/个股分析-YYYY-MM-DD.json(+ 文本摘要打印)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402
import chips  # noqa: E402
import chan  # noqa: E402
import emotion  # noqa: E402
import fundamental  # noqa: E402
import judge  # noqa: E402
import signals  # noqa: E402  逐日买卖信号(反哺研判/评分)
from indicators import compute_indicators  # noqa: E402
from scoring import score_stock  # noqa: E402

ROOT = Path(__file__).parent.parent


def _plan(ind: dict, dd: float):
    stop = max(ind["ma20"], round(ind["close"] * (1 - dd / 100), 2))
    return round(stop, 2), round(ind["pressure"], 2)


def analyze_one(code: str, name: str, cfg: dict) -> dict:
    rp, hr = cfg["report"], cfg["hard_rules"]
    dd = cfg["style"]["max_drawdown_pct"]
    chart_bars = int(rp.get("chart_bars", 120))
    k = fetcher.get_kline(code, rp["kline_days"])
    fresh, fresh_msg = fetcher.check_freshness(k, cfg["data"]["freshness_max_age_days"])
    ind = compute_indicators(k)
    stop, target = _plan(ind, dd)
    kk = k.tail(chart_bars)
    bars = [{"d": str(r["日期"]), "o": float(r["开盘"]), "c": float(r["收盘"]),
             "l": float(r["最低"]), "h": float(r["最高"]), "v": float(r["成交量"]),
             "换手": round(float(r.get("换手", 0) or 0), 5)}
            for _, r in kk.iterrows()]
    # 逐日买卖信号(纯本地):供图上画箭头 + 反哺研判/评分(与图同源一致)
    sig = signals.compute(signals.series_from_bars(bars))
    sig_sum = signals.latest_summary(sig, [b["d"] for b in bars])
    sc = score_stock(ind, hr, sig_sum)
    chip = chips.compute_chips(bars)
    flow = fetcher.get_fund_flow(code)
    chan_res = chan.analyze(bars)
    turn_pct = round((bars[-1].get("换手", 0) or 0) * 100, 2)
    jd = judge.synthesize(ind, chip, judge.flow_sum(flow), chan_res, target, dd, turn_pct, sig=sig_sum)
    return {
        "code": code, "name": name, "sector": "点名分析", "sector_pct": 0.0,
        "stock_emotion": emotion.stock_heat(code, bars),
        "fundamental": fundamental.summarize(code),
        "signal": sc["signal"], "score": sc["total"], "breakdown": sc["breakdown"],
        "entry_date": ind["date"], "entry_close": ind["close"],
        "plan_stop": jd["structural_stop"]["stop"], "plan_target": target,
        "indicators": ind, "bars": bars, "signals": sig, "signal_summary": sig_sum,
        "chips": chip, "chip_comment": chips.control_comment(chip),
        "fund_flow": flow, "judge": jd, "fresh_msg": fresh_msg, "vetoes": sc["vetoes"],
    }


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法: python analyze.py 603005=晶方科技 002756=永兴材料"); return
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = dt.date.today()
    picks = []
    for a in args:
        code, _, name = a.partition("=")
        code = code.strip().zfill(6)
        name = name.strip() or code
        print(f"分析 {name}({code}) ...")
        try:
            picks.append(analyze_one(code, name, cfg))
        except Exception as e:  # noqa: BLE001
            print(f"  失败:{e}")
    sidecar = {"date": str(today), "source": "点名分析", "scanned": len(picks),
               "indexes": fetcher.get_index_snapshot(), "sectors": [], "picks": picks}
    day_dir = ROOT / "reports" / str(today).replace("-", "")
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"个股分析-{today}.json"
    out.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n侧车 → {out}")
    for p in picks:
        i = p["indicators"]
        print(f"\n===== {p['name']}({p['code']})  {p['signal']} 评分 {p['score']}/100 =====")
        print(f"  现价 {i['close']} ({i['pct_chg']:+}%) | {p['fresh_msg']}")
        print(f"  均线 {i['alignment']} MA5/10/20/60={i['ma5']}/{i['ma10']}/{i['ma20']}/{i['ma60']}")
        print(f"  MACD {i['macd_cross']}{'(零上)' if i['macd_above_zero'] else '(零下)'} "
              f"RSI6={i['rsi6']} 量比{i['vol_ratio']}({i['vol_pattern']}) MA5乖离{i['bias5']}%")
        print(f"  支撑 {i['support']} 压力 {i['pressure']} | 目标 {p['plan_target']} 止损 {p['plan_stop']}")
        print(f"  评分明细 {p['breakdown']} 否决 {p['vetoes'] or '无'}")
        print(f"  筹码 {p['chip_comment']}")
        fs = [x for x in p["fund_flow"]]
        if fs:
            m5 = round(sum(x["main"] for x in fs[-5:]), 1)
            m20 = round(sum(x["main"] for x in fs[-20:]), 1)
            print(f"  资金 主力近5日{m5}万 近20日{m20}万")


if __name__ == "__main__":
    main()
