# -*- coding: utf-8 -*-
"""离线自测:用构造的K线数据验证 指标计算→评分→报告渲染 全链路。
用法: python scripts/selftest.py
"""
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

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
print()
print("== 断言检查")
ok = True
for name, passed in checks:
    status = "PASS" if passed else "FAIL"
    print("   [{}] {}".format(status, name))
    ok = ok and passed

sys.exit(0 if ok else 1)
