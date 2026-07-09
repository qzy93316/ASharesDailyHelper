# -*- coding: utf-8 -*-
"""离线自测:用构造的K线数据验证 指标计算→评分→报告渲染 全链路。
用法: python scripts/selftest.py
"""
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

# Windows GBK 控制台打印 emoji(信号/排列里的 🔵🟢 等)会 UnicodeEncodeError,兜底为 replace
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).parent))
from indicators import compute_indicators  # noqa: E402
from scoring import score_stock  # noqa: E402


def make_kline(days=180, trend=0.002, vol_shrink=False, seed=1) -> pd.DataFrame:
    """构造一段带趋势的日K。trend>0 上升趋势。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=dt.date.today(), periods=days)
    noise = abs(trend) * 0.8 + 0.003  # 噪声与趋势成比例,保证趋势主导
    ret = rng.normal(trend, noise, days)
    close = 10 * np.exp(np.cumsum(ret))
    high = close * (1 + rng.uniform(0, 0.02, days))
    low = close * (1 - rng.uniform(0, 0.02, days))
    open_ = close * (1 + rng.normal(0, 0.005, days))
    volume = rng.uniform(1e6, 2e6, days)
    if vol_shrink:  # 末端缩量回调
        volume[-1] = volume[-6:-1].mean() * 0.5
        close[-1] = close[-2] * 0.99
    return pd.DataFrame({"日期": dates.date, "开盘": open_, "收盘": close,
                         "最高": high, "最低": low, "成交量": volume})


HARD = {"max_bias5_pct": 5, "max_rsi6": 80}
CASES = [
    ("多头趋势+缩量回调(应得高分)", make_kline(trend=0.006, vol_shrink=True)),
    ("单边疯涨(应被硬规则否决)", make_kline(trend=0.02, seed=2)),
    ("阴跌空头(应低分回避)", make_kline(trend=-0.006, seed=3)),
]

for name, k in CASES:
    ind = compute_indicators(k)
    sc = score_stock(ind, HARD)
    print()
    print("==", name)
    print("   排列={} MACD={} RSI6={} 量比={} 乖离={}%".format(
        ind["alignment"], ind["macd_cross"], ind["rsi6"], ind["vol_ratio"], ind["bias5"]))
    print("   评分={}/100 信号={} 否决={}".format(
        sc["total"], sc["signal"], sc["vetoes"] or "无"))

i1 = compute_indicators(CASES[0][1]); s1 = score_stock(i1, HARD)
i2 = compute_indicators(CASES[1][1]); s2 = score_stock(i2, HARD)
i3 = compute_indicators(CASES[2][1]); s3 = score_stock(i3, HARD)
checks = [
    ("多头案例评分>空头案例", s1["total"] > s3["total"]),
    ("疯涨案例被否决或观望", bool(s2["vetoes"]) or "观望" in s2["signal"]),
    ("空头案例不给关注信号", "关注" not in s3["signal"]),
    ("多头排列识别正确", i1["alignment"] in ("多头排列", "弱多")),
    ("空头排列识别正确", i3["alignment"] in ("空头排列", "弱空")),
]
# ── 逐日信号层(signals.py):构造已知金叉/超卖/KDJ金叉序列断言命中 ──
import signals as _sig  # noqa: E402
_ser = {"dates": ["d0", "d1", "d2", "d3"],
        "dif": [-1, -0.5, 0.5, 1.0], "dea": [0, 0, 0, 0.2], "macd": [-2, -1, 1, 1.6],
        "kdj_k": [10, 20, 60, 70], "kdj_d": [30, 30, 40, 50],
        "rsi6": [15, 25, 50, 60], "vol": [1, 1, 1, 1], "kline": [[1, 1, 1, 1]] * 4}
_sg = _sig.compute(_ser)
checks += [
    ("MACD金叉被识别", any("金叉" in x["kind"] for x in _sg["macd"])),
    ("RSI超卖回升被识别", any(x["dir"] == "buy" for x in _sg["rsi"])),
    ("KDJ金叉被识别", any(x["dir"] == "buy" for x in _sg["kdj"])),
]

# ── Phase 2:信号反哺 judge/scoring ──
import judge as _judge  # noqa: E402
_sigsum = {"macd": {"dir": "buy", "kind": "金叉", "bars_ago": 1},
           "kdj": {"dir": "buy", "kind": "KDJ金叉", "bars_ago": 2}}
_jd = _judge.synthesize(i1, None, None, None, i1["close"] * 1.1, 8, None, sig=_sigsum)
_sc_none = score_stock(i1, HARD)                 # 不传 sig(全市场扫描口径)
_sc_buy = score_stock(i1, HARD, _sigsum)         # 传买入共振
_sc_sell = score_stock(i1, HARD, {"macd": {"dir": "sell", "kind": "死叉", "bars_ago": 1}})
checks += [
    ("judge消费信号产出signal_notes", bool(_jd.get("signal_notes"))),
    ("信号确认加分有界(|adj|≤4)", abs(_sc_buy["breakdown"].get("信号确认", 0)) <= 4),
    ("买入共振评分≥无信号基线", _sc_buy["total"] >= _sc_none["total"]),
    ("卖出信号评分≤无信号基线", _sc_sell["total"] <= _sc_none["total"]),
    ("不传sig与旧行为一致(无信号确认项)", "信号确认" not in _sc_none["breakdown"]),
]
# ── 均线阶梯减仓 + 结构止损容错下限(kb/ma-code-rules.md 接入) ──
_lad = _judge.ma_ladder({"close": 100.0, "ma5": 98.0, "ma10": 95.0, "ma20": 90.0, "ma60": 80.0, "bias5": 2.0})
_lad_lines = [r["line"] for r in _lad["rungs"]]
_lad_bias = _judge.ma_ladder({"close": 100.0, "ma5": 94.0, "ma10": 92.0, "ma20": 88.0, "ma60": 80.0, "bias5": 6.4})
_ss_tight = _judge.structural_stop({"close": 100.0, "support": 99.0, "ma20": 90.0, "ma10": 98.5}, None, 8)
checks += [
    ("均线阶梯:多头未破线时无已破档", _lad["broken"] == []),
    ("均线阶梯:含MA5/MA10/MA20/MA60四档", all(x in _lad_lines for x in ("MA5", "MA10", "MA20", "MA60"))),
    ("均线阶梯:最近减仓位为下方MA5", bool(_lad["next"]) and _lad["next"]["line"] == "MA5"),
    ("均线阶梯:偏离MA5>5%触发高抛做T", any(r["action"] == "高抛做T" for r in _lad_bias["rungs"])),
    ("结构止损:贴支撑放宽到止损距≥3.5%", _ss_tight["dist_pct"] >= 3.5 - 1e-9),
]
# ── 强弱研判单一真源 indicators.strength():与旧 `align in (...)` 口径逐值等价 ──
from indicators import strength as _strength  # noqa: E402
_str_ok = all(
    _strength({"alignment": a})["bull"] == (a in ("多头排列", "弱多"))
    and _strength({"alignment": a})["weak"] == (a in ("空头排列", "弱空"))
    and _strength({"alignment": a})["strong"] == (a == "多头排列")
    for a in ("多头排列", "弱多", "弱空", "空头排列"))
checks += [
    ("strength 与旧align口径逐值等价", _str_ok),
    ("strength bull/weak 四值互补", all(_strength({"alignment": a})["bull"] != _strength({"alignment": a})["weak"]
                                    for a in ("多头排列", "弱多", "弱空", "空头排列"))),
]
# ── diagnose 强弱单源 + grey 落地 ──
import diagnose_portfolio as _dp  # noqa: E402
_p_hold = {"indicators": {"alignment": "多头排列", "close": 10.0},
           "judge": {"stance": "顺势多头", "structural_stop": {"stop": 9.0, "basis": "MA20"},
                     "grey": ["RSI68 进入偏热区(65-80)"]}}
_txt_hold, _tag_hold = _dp._verdict(_p_hold, 9.5, 10.0, 9.0, 5.26, 8, 1000, 11.0)
_p_stop = {"indicators": {"alignment": "空头排列", "close": 8.9},
           "judge": {"stance": "趋势偏弱", "structural_stop": {"stop": 9.0, "basis": "MA20"},
                     "grey": ["换手30% 过高"]}}
_txt_stop, _tag_stop = _dp._verdict(_p_stop, 9.5, 8.9, 9.0, -6.3, 8, -1000, -1.1)
checks += [
    ("诊断 grey 落地到持有类文字", "灰区预警" in _txt_hold),
    ("诊断 止损类不叠加 grey 噪音", "灰区预警" not in _txt_stop),
    ("诊断 weak 单源:多头非弱→持有", _tag_hold == "持有"),
    ("诊断 weak 单源:空头破止损→止损", _tag_stop == "止损"),
]
# ── 信号有效性回测(signal_backtest.backtest_bars):V形序列前向收益+胜率+末端剔除 ──
import signal_backtest as _sbt  # noqa: E402
def _mkbars(seq):
    return [{"d": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "o": c, "c": c,
             "l": c * 0.995, "h": c * 1.005, "v": 1e6} for i, c in enumerate(seq)]
_seq = [60 - 0.7 * i for i in range(15)] + [50 + 0.6 * i for i in range(75)]  # 先跌后涨(V形)
_bars = _mkbars(_seq)
_rows = _sbt.backtest_bars(_bars, "TEST", primary=5)
_buys = [r for r in _rows if r["dir"] == "buy"]
_didx = {b["d"]: i for i, b in enumerate(_bars)}
checks += [
    ("信号回测:V形序列采到信号", len(_rows) > 0),
    ("信号回测:上升段买入信号皆胜(前向为正)", bool(_buys) and all(r["win"] for r in _buys)),
    ("信号回测:末端不足horizon的信号已剔除", all(_didx[r["d"]] + max(_sbt.HORIZONS) < len(_bars) for r in _rows)),
]
print()
print("== 断言检查")
ok = True
for name, passed in checks:
    status = "PASS" if passed else "FAIL"
    print("   [{}] {}".format(status, name))
    ok = ok and passed

sys.exit(0 if ok else 1)
