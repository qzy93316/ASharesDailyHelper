# -*- coding: utf-8 -*-
"""K线组合形态检测器 —— 判定逻辑据知识库 knowledge/kb/candlestick-patterns.json
(源:70种经典K线组合PPT)实现。元数据(中文名/多空/类别/可靠度)从 JSON 读取,
判定用 OHLC 编码;扫描近端K线,输出命中的形态,供图表标注与研判。

v3.7 全量补全:从 demo 的 17 种扩到全部 74 种(单/双/三根经典 + 多根趋势形)。
趋势形(冉冉上升/绵绵阴跌等)用收盘斜率+实体大小启发式判定,可靠度以 KB 标注为准。
"""
import json
from pathlib import Path

_KB_PATH = Path(__file__).parent.parent / "knowledge" / "kb" / "candlestick-patterns.json"
try:
    _KB = {e["id"]: e for e in json.loads(_KB_PATH.read_text(encoding="utf-8"))}
except Exception:  # noqa: BLE001
    _KB = {}


# ---- 基础度量 ----
def _b(k):
    return abs(k["c"] - k["o"])
def _rng(k):
    return max(k["h"] - k["l"], 1e-9)
def _up(k):
    return k["h"] - max(k["o"], k["c"])
def _lo(k):
    return min(k["o"], k["c"]) - k["l"]
def _yang(k):
    return k["c"] >= k["o"]
def _mid(k):
    return (k["o"] + k["c"]) / 2
def _avgbody(b, i, n=10):
    j = max(0, i - n)
    s = [_b(x) for x in b[j:i]]
    return (sum(s) / len(s)) if s else max(_b(b[i]), 1e-9)
def _big(b, i):
    k = b[i]
    return _b(k) >= _rng(k) * 0.6 and _b(k) >= _avgbody(b, i) * 1.3
def _small(b, i):
    return _b(b[i]) <= _avgbody(b, i) * 0.6
def _gapup(b, i):
    return i >= 1 and b[i]["l"] > b[i - 1]["h"]
def _gapdn(b, i):
    return i >= 1 and b[i]["h"] < b[i - 1]["l"]
def _near(a, c, tol=0.005):
    return abs(a - c) / max(abs(a), 1e-9) < tol
def _downtrend(b, i, n=4):
    return i >= n and b[i - n]["c"] > b[i - 1]["c"]
def _uptrend(b, i, n=4):
    return i >= n and b[i - n]["c"] < b[i - 1]["c"]
def _slope(b, i, n):
    j = max(0, i - n + 1)
    ys = [x["c"] for x in b[j:i + 1]]
    m = len(ys)
    if m < 3:
        return 0.0
    xs = list(range(m))
    mx, my = sum(xs) / m, sum(ys) / m
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    return sum((xs[t] - mx) * (ys[t] - my) for t in range(m)) / den
def _accel(b, i, n):
    """收盘价二阶差分均值(>0 加速上行,<0 加速下行)。"""
    j = max(0, i - n + 1)
    ys = [x["c"] for x in b[j:i + 1]]
    if len(ys) < 3:
        return 0.0
    d1 = [ys[t] - ys[t - 1] for t in range(1, len(ys))]
    d2 = [d1[t] - d1[t - 1] for t in range(1, len(d1))]
    return sum(d2) / len(d2) if d2 else 0.0


# ================= 单根 =================
def _hammer(b, i):
    k = b[i]; return _downtrend(b, i) and _b(k) < _rng(k) * 0.4 and _lo(k) >= 2 * _b(k) and _up(k) <= _b(k) * 0.5
def _inverted_hammer(b, i):
    k = b[i]; return _downtrend(b, i) and _b(k) < _rng(k) * 0.4 and _up(k) >= 2 * _b(k) and _lo(k) <= _b(k) * 0.5
def _shooting_star(b, i):
    k = b[i]; return _uptrend(b, i) and _b(k) < _rng(k) * 0.4 and _up(k) >= 2 * _b(k) and _lo(k) <= _b(k) * 0.5
def _hanging_man(b, i):
    k = b[i]; return _uptrend(b, i) and _b(k) < _rng(k) * 0.4 and _lo(k) >= 2 * _b(k) and _up(k) <= _b(k) * 0.5
def _doji(b, i):
    k = b[i]; return _b(k) <= _rng(k) * 0.1
def _dragonfly_doji(b, i):
    k = b[i]; return _doji(b, i) and _lo(k) >= _rng(k) * 0.6 and _up(k) <= _rng(k) * 0.1
def _gravestone_doji(b, i):
    k = b[i]; return _doji(b, i) and _up(k) >= _rng(k) * 0.6 and _lo(k) <= _rng(k) * 0.1
def _one_word(b, i):
    k = b[i]; return _rng(k) <= max(k["c"], 1) * 0.005
def _long_legged_doji(b, i):
    k = b[i]; return _doji(b, i) and _up(k) >= _rng(k) * 0.3 and _lo(k) >= _rng(k) * 0.3 and _rng(k) >= _avgbody(b, i) * 1.5
def _spinning_top(b, i):
    k = b[i]; return (not _doji(b, i)) and _small(b, i) and _up(k) >= _b(k) and _lo(k) >= _b(k)
def _big_yang(b, i):
    return _yang(b[i]) and _big(b, i)
def _big_yin(b, i):
    return (not _yang(b[i])) and _big(b, i)
def _small_yang(b, i):
    return _yang(b[i]) and _small(b, i) and not _doji(b, i)
def _small_yin(b, i):
    return (not _yang(b[i])) and _small(b, i) and not _doji(b, i)


# ================= 双根 =================
def _bullish_engulfing(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]; return not _yang(p) and _yang(k) and k["o"] < p["c"] and k["c"] > p["o"]
def _bearish_engulfing(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]; return _yang(p) and not _yang(k) and k["o"] > p["c"] and k["c"] < p["o"]
def _harami(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _b(p) > _b(k) * 2 and max(k["o"], k["c"]) < max(p["o"], p["c"]) and min(k["o"], k["c"]) > min(p["o"], p["c"])
def _dark_cloud_cover(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _uptrend(b, i) and _yang(p) and not _yang(k) and k["o"] > p["h"] and _mid(p) > k["c"] > p["o"]
def _piercing_line(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _downtrend(b, i) and not _yang(p) and _yang(k) and k["o"] < p["l"] and p["o"] > k["c"] > _mid(p)
def _bullish_counterattack(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _downtrend(b, i) and not _yang(p) and _big(b, i - 1) and k["o"] < p["c"] and _yang(k) and _near(k["c"], p["c"], 0.01)
def _bearish_counterattack(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _uptrend(b, i) and _yang(p) and _big(b, i - 1) and k["o"] > p["c"] and not _yang(k) and _near(k["c"], p["c"], 0.01)
def _rising_sun(b, i):  # 旭日东升
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _downtrend(b, i) and not _yang(p) and _yang(k) and k["c"] > p["o"] and k["o"] < p["c"]
def _pouring_rain(b, i):  # 倾盆大雨
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _uptrend(b, i) and _yang(p) and not _yang(k) and k["o"] < p["c"] and k["c"] < p["o"]
def _tweezer_bottom(b, i):
    if i < 1: return False
    return _downtrend(b, i) and _near(b[i]["l"], b[i - 1]["l"], 0.005)
def _tweezer_top(b, i):
    if i < 1: return False
    return _uptrend(b, i) and _near(b[i]["h"], b[i - 1]["h"], 0.005)
def _upside_gap(b, i):  # 跳空上扬形
    if i < 2: return False
    p, k = b[i - 1], b[i]
    return _yang(p) and p["l"] > b[i - 2]["h"] and not _yang(k) and k["c"] > b[i - 2]["h"]
def _kneading_line(b, i):  # 搓揉线:T字线 + 倒T字线(任一顺序)
    if i < 1: return False
    a, k = b[i - 1], b[i]
    def _tx(x): return _doji(b, b.index(x)) if False else (_b(x) <= _rng(x) * 0.2)
    tt = _lo(a) >= _rng(a) * 0.6 and _up(k) >= _rng(k) * 0.6 and _tx(a) and _tx(k)
    tt2 = _up(a) >= _rng(a) * 0.6 and _lo(k) >= _rng(k) * 0.6 and _tx(a) and _tx(k)
    return _uptrend(b, i) and (tt or tt2)
def _end_line(b, i):  # 尽头线
    if i < 1: return False
    p, k = b[i - 1], b[i]
    up = _uptrend(b, i) and _yang(p) and _big(b, i - 1) and _up(p) >= _b(p) * 0.5 and _small(b, i) and max(k["o"], k["c"]) <= p["h"] and min(k["o"], k["c"]) > p["c"]
    dn = _downtrend(b, i) and not _yang(p) and _big(b, i - 1) and _lo(p) >= _b(p) * 0.5 and _small(b, i) and min(k["o"], k["c"]) >= p["l"] and max(k["o"], k["c"]) < p["c"]
    return up or dn


# ================= 三根 =================
def _three_white_soldiers(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return all(_yang(t) for t in (x, y, z)) and x["c"] < y["c"] < z["c"] and y["o"] > x["o"] and z["o"] > y["o"]
def _three_black_crows(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return all(not _yang(t) for t in (x, y, z)) and x["c"] > y["c"] > z["c"] and y["o"] < x["o"] and z["o"] < y["o"]
def _morning_star(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _downtrend(b, i - 1) and not _yang(x) and _b(y) < _b(x) * 0.5 and max(y["o"], y["c"]) < x["c"] and _yang(z) and z["c"] > _mid(x)
def _evening_star(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i - 1) and _yang(x) and _b(y) < _b(x) * 0.5 and min(y["o"], y["c"]) > x["c"] and not _yang(z) and z["c"] < _mid(x)
def _morning_doji_star(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _downtrend(b, i - 1) and not _yang(x) and _doji(b, i - 1) and max(y["o"], y["c"]) < x["c"] and _yang(z) and z["c"] >= _mid(x)
def _evening_doji_star(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i - 1) and _yang(x) and _doji(b, i - 1) and min(y["o"], y["c"]) > x["c"] and not _yang(z) and z["c"] <= _mid(x)
def _two_crows(b, i):  # 双飞乌鸦
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i) and _yang(x) and not _yang(y) and not _yang(z) and y["c"] > x["c"] and z["o"] > y["o"] and z["c"] < y["c"]
def _three_black_soldiers(b, i):  # 黑三兵
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return all(not _yang(t) for t in (x, y, z)) and x["l"] > y["l"] > z["l"] and x["c"] > y["c"] > z["c"] and all(_small(b, j) for j in (i - 2, i - 1, i))
def _low_side_by_side_yang(b, i):
    if i < 2: return False
    p, k = b[i - 1], b[i]
    return _downtrend(b, i) and _yang(p) and p["h"] < b[i - 2]["l"] and _yang(k) and _near(k["o"], p["o"], 0.01) and _near(k["c"], p["c"], 0.01)
def _high_side_by_side_yang(b, i):
    if i < 2: return False
    p, k = b[i - 1], b[i]
    return _uptrend(b, i) and _yang(p) and p["l"] > b[i - 2]["h"] and _yang(k) and _near(k["o"], p["o"], 0.01) and _near(k["c"], p["c"], 0.01)
def _gapping_three_yin_bottom(b, i):
    if i < 2: return False
    return all(not _yang(b[j]) for j in (i - 2, i - 1, i)) and _gapdn(b, i - 1) and _gapdn(b, i)
def _gapping_three_yang_top(b, i):
    if i < 2: return False
    return all(_yang(b[j]) for j in (i - 2, i - 1, i)) and _gapup(b, i - 1) and _gapup(b, i)
def _rising_two_stars(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _yang(x) and _big(b, i - 2) and _small(b, i - 1) and _small(b, i) and min(y["o"], y["c"]) > x["c"] and min(z["o"], z["c"]) > x["c"]
def _two_red_one_black(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _yang(x) and not _yang(y) and _yang(z) and _b(y) < _b(x) and _b(y) < _b(z)
def _two_black_one_red(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return (not _yang(x)) and _yang(y) and (not _yang(z)) and _b(y) < _b(x) and _b(y) < _b(z)
def _reverse_three_yang(b, i):  # 倒三阳
    if i < 3: return False
    ok = all(b[j]["o"] < b[j - 1]["c"] and _yang(b[j]) for j in (i - 2, i - 1, i))
    down = all(b[j]["c"] <= b[j - 1]["o"] for j in (i - 1, i))
    return _downtrend(b, i - 2, 3) and ok and down
def _rising_blocked(b, i):  # 升势受阻
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i) and all(_yang(t) for t in (x, y, z)) and _b(x) > _b(y) > _b(z) and _up(z) >= _b(z) * 1.5
def _rising_pause(b, i):  # 升势停顿
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i) and _yang(x) and _yang(y) and _big(b, i - 2) and _big(b, i - 1) and _yang(z) and _b(z) < _b(y) * 0.4
def _yang_slope_foot(b, i):  # 阳线坡脚形
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i) and all(_yang(t) for t in (x, y, z)) and y["o"] < x["c"] and z["o"] < y["c"] and z["c"] < y["c"]
def _forceps_line(b, i):  # 镊子线(3根最高价近似相等)
    if i < 2: return False
    hs = [b[j]["h"] for j in (i - 2, i - 1, i)]
    return (max(hs) - min(hs)) / (sum(hs) / 3) < 0.005


# ================= 四根 =================
def _gapping_three_stars_bottom(b, i):  # 跳空下跌三颗星
    if i < 3: return False
    return _gapdn(b, i - 2) and all((not _yang(b[j])) and _small(b, j) for j in (i - 2, i - 1, i))
def _falling_three_stars(b, i):  # 下跌三颗星
    if i < 3: return False
    x = b[i - 3]
    return not _yang(x) and _big(b, i - 3) and all(_small(b, j) and max(b[j]["o"], b[j]["c"]) < x["c"] for j in (i - 2, i - 1, i))
def _descending_cover_line(b, i):  # 下降覆盖线
    if i < 3: return False
    return _bullish_engulfing(b, i - 2) and _yang(b[i - 1]) and not _yang(b[i]) and b[i]["c"] < _mid(b[i - 1])


# ================= 五根 =================
def _rising_three_methods(b, i):  # 上升三部曲
    if i < 4: return False
    x = b[i - 4]; z = b[i]
    mid = [b[j] for j in (i - 3, i - 2, i - 1)]
    return _yang(x) and _big(b, i - 4) and all((not _yang(m)) and m["l"] > x["o"] for m in mid) and _yang(z) and _big(b, i) and z["c"] > x["c"]
def _falling_three_methods(b, i):  # 下降三部曲
    if i < 4: return False
    x = b[i - 4]; z = b[i]
    mid = [b[j] for j in (i - 3, i - 2, i - 1)]
    return not _yang(x) and _big(b, i - 4) and all(_yang(m) and m["h"] < x["o"] for m in mid) and not _yang(z) and _big(b, i) and z["c"] < x["c"]
def _low_five_yang(b, i):
    if i < 4: return False
    return _downtrend(b, i - 4, 3) and all(_yang(b[j]) for j in range(i - 4, i + 1))
def _tower_bottom(b, i):
    if i < 4: return False
    return _downtrend(b, i - 4) and _big_yin(b, i - 4) and all(_small(b, j) for j in (i - 3, i - 2, i - 1)) and _big_yang(b, i)
def _tower_top(b, i):
    if i < 4: return False
    return _uptrend(b, i - 4) and _big_yang(b, i - 4) and all(_small(b, j) for j in (i - 3, i - 2, i - 1)) and _big_yin(b, i)
def _gradual_rising(b, i):  # 徐缓上升形
    if i < 4: return False
    return all(_yang(b[j]) and _small(b, j) for j in (i - 4, i - 3, i - 2)) and _yang(b[i]) and _big(b, i)
def _gradual_decline(b, i):  # 徐缓下跌形
    if i < 4: return False
    return all((not _yang(b[j])) and _small(b, j) for j in (i - 4, i - 3, i - 2)) and not _yang(b[i]) and _big(b, i)


# ================= 六根+(趋势形,斜率/实体启发式) =================
def _steady_rising(b, i):
    if i < 5: return False
    yn = sum(1 for j in range(i - 5, i + 1) if _yang(b[j]))
    return yn >= 4 and _slope(b, i, 6) > 0
def _endless_decline(b, i):
    if i < 5: return False
    yn = sum(1 for j in range(i - 5, i + 1) if not _yang(b[j]))
    return yn >= 4 and _slope(b, i, 6) < 0
def _rising_resistance(b, i):
    if i < 5: return False
    return all(b[j]["o"] > b[j - 1]["c"] and b[j]["c"] > b[j - 1]["c"] for j in range(i - 2, i + 1))
def _descending_resistance(b, i):
    if i < 5: return False
    return all(b[j]["o"] < b[j - 1]["c"] and b[j]["c"] < b[j - 1]["c"] for j in range(i - 2, i + 1))
def _arc_rising(b, i):
    return i >= 5 and _slope(b, i, 6) > 0 and _accel(b, i, 6) > 0
def _acceleration_line(b, i):
    if i < 5: return False
    a = _accel(b, i, 6)
    return (a > 0 and _yang(b[i]) and _big(b, i)) or (a < 0 and not _yang(b[i]) and _big(b, i))
def _probe_down_rise(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _yang(k) and _big(b, i) and k["o"] < p["c"] * 0.98 and k["c"] > p["c"]
def _high_open_escape(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _uptrend(b, i) and not _yang(k) and _big(b, i) and k["o"] > p["c"] * 1.02 and k["c"] < p["c"]
def _bull_vanguard(b, i):
    if i < 5: return False
    for v in range(i - 5, i - 1):
        if _up(b[v]) >= _b(b[v]) and _up(b[v]) >= _rng(b[v]) * 0.3:
            if b[i]["c"] > b[v]["h"]:
                return True
    return False
def _bear_vanguard(b, i):
    if i < 5: return False
    for v in range(i - 5, i - 1):
        if _lo(b[v]) >= _b(b[v]) and _lo(b[v]) >= _rng(b[v]) * 0.3:
            if b[i]["c"] < b[v]["l"]:
                return True
    return False
def _high_five_yin(b, i):
    if i < 5: return False
    return _yang(b[i - 5]) and all(not _yang(b[j]) for j in range(i - 4, i + 1))
def _slowly_rising(b, i):
    if i < 7: return False
    sm = sum(1 for j in range(i - 7, i + 1) if _small(b, j))
    yn = sum(1 for j in range(i - 7, i + 1) if _yang(b[j]))
    return sm >= 6 and yn >= 5 and _slope(b, i, 8) > 0
def _continuous_decline(b, i):
    if i < 7: return False
    sm = sum(1 for j in range(i - 7, i + 1) if _small(b, j))
    yn = sum(1 for j in range(i - 7, i + 1) if not _yang(b[j]))
    return sm >= 6 and yn >= 5 and _slope(b, i, 8) < 0
def _rounding_bottom(b, i):
    if i < 7: return False
    lows = [b[j]["l"] for j in range(i - 7, i + 1)]
    mn = lows.index(min(lows))
    return 2 <= mn <= 5 and _gapup(b, i)
def _rounding_top(b, i):
    if i < 7: return False
    highs = [b[j]["h"] for j in range(i - 7, i + 1)]
    mx = highs.index(max(highs))
    return 2 <= mx <= 5 and _gapdn(b, i)
def _high_consolidation(b, i):
    if i < 5: return False
    if not (_yang(b[i - 5]) and _big(b, i - 5)):
        return False
    cs = [b[j]["c"] for j in range(i - 4, i + 1)]
    return all(_small(b, j) for j in range(i - 4, i + 1)) and (max(cs) - min(cs)) / (sum(cs) / len(cs)) < 0.04
def _low_consolidation(b, i):
    if i < 5: return False
    if not all(_small(b, j) for j in range(i - 5, i - 1)):
        return False
    p, k = b[i - 1], b[i]
    return _downtrend(b, i - 5) and k["o"] > p["c"] and not _yang(k) and _b(k) >= _avgbody(b, i)


_DETECTORS = {
    # 单根
    "hammer": _hammer, "inverted_hammer": _inverted_hammer, "shooting_star": _shooting_star,
    "hanging_man": _hanging_man, "doji": _doji, "dragonfly_doji": _dragonfly_doji,
    "gravestone_doji": _gravestone_doji, "one_word_line": _one_word,
    "long_legged_doji": _long_legged_doji, "spinning_top": _spinning_top,
    "big_yang": _big_yang, "big_yin": _big_yin, "small_yang": _small_yang, "small_yin": _small_yin,
    # 双根
    "bullish_engulfing": _bullish_engulfing, "bearish_engulfing": _bearish_engulfing,
    "pregnant_harami": _harami, "dark_cloud_cover": _dark_cloud_cover, "piercing_line": _piercing_line,
    "bullish_counterattack": _bullish_counterattack, "bearish_counterattack": _bearish_counterattack,
    "bullish_engulfing_rising_sun": _rising_sun, "pouring_rain": _pouring_rain,
    "tweezer_bottom": _tweezer_bottom, "tweezer_top": _tweezer_top, "upside_gap": _upside_gap,
    "kneading_line": _kneading_line, "end_line": _end_line,
    # 三根
    "three_white_soldiers": _three_white_soldiers, "three_black_crows": _three_black_crows,
    "morning_star": _morning_star, "evening_star": _evening_star,
    "morning_doji_star": _morning_doji_star, "evening_doji_star": _evening_doji_star,
    "two_crows": _two_crows, "three_black_soldiers": _three_black_soldiers,
    "low_side_by_side_yang": _low_side_by_side_yang, "high_side_by_side_yang": _high_side_by_side_yang,
    "gapping_three_yin_bottom": _gapping_three_yin_bottom, "gapping_three_yang_top": _gapping_three_yang_top,
    "rising_two_stars": _rising_two_stars, "two_red_one_black": _two_red_one_black,
    "two_black_one_red": _two_black_one_red, "reverse_three_yang": _reverse_three_yang,
    "rising_blocked": _rising_blocked, "rising_pause": _rising_pause,
    "yang_slope_foot": _yang_slope_foot, "forceps_line": _forceps_line,
    # 四根
    "gapping_three_stars_bottom": _gapping_three_stars_bottom, "falling_three_stars": _falling_three_stars,
    "descending_cover_line": _descending_cover_line,
    # 五根
    "rising_three_methods": _rising_three_methods, "falling_three_methods": _falling_three_methods,
    "low_five_yang": _low_five_yang, "tower_bottom": _tower_bottom, "tower_top": _tower_top,
    "gradual_rising": _gradual_rising, "gradual_decline": _gradual_decline,
    # 六根+趋势形
    "steady_rising": _steady_rising, "endless_decline": _endless_decline,
    "rising_resistance": _rising_resistance, "descending_resistance": _descending_resistance,
    "arc_rising": _arc_rising, "acceleration_line": _acceleration_line,
    "probe_down_rise": _probe_down_rise, "high_open_escape": _high_open_escape,
    "bull_vanguard": _bull_vanguard, "bear_vanguard": _bear_vanguard,
    "high_five_yin": _high_five_yin, "slowly_rising": _slowly_rising,
    "continuous_decline": _continuous_decline, "rounding_bottom": _rounding_bottom,
    "rounding_top": _rounding_top, "high_consolidation": _high_consolidation,
    "low_consolidation": _low_consolidation,
}

# 自定义拆分检测器(KB 无独立条目):名称+方向+可靠度
_CUSTOM = {
    "bullish_engulfing": ("看涨穿头破脚", "bull", "高"),
    "bearish_engulfing": ("看跌穿头破脚", "bear", "高"),
}
# 单根方向性形态:KB 标 neutral,但按出现位置有明确多空含义
_BIAS_OVERRIDE = {
    "hammer": "bull", "inverted_hammer": "bull", "shooting_star": "bear", "hanging_man": "bear",
    "dragonfly_doji": "bull", "gravestone_doji": "bear",
}


def _meta(det_id: str) -> dict:
    if det_id in _CUSTOM:
        n, bs, r = _CUSTOM[det_id]
        return {"name_cn": n, "bias": bs, "reliability": r, "category": ""}
    e = _KB.get(det_id, {})
    return {"name_cn": e.get("name_cn", det_id),
            "bias": _BIAS_OVERRIDE.get(det_id, e.get("bias", "neutral")),
            "reliability": e.get("reliability", "中"), "category": e.get("category", "")}


_REL_RANK = {"高": 0, "中": 1, "低": 2}


def detect(bars: list[dict], lookback: int = 20, per_bar: int = 2) -> list[dict]:
    """扫描最近 lookback 根K线,返回命中的形态(按日期)。
    per_bar:每根K线最多保留几个形态(按 可靠度→根数 优先),降噪防图上堆叠。
    全部 75 个检测器仍会跑,只是同一根 K 线上的低优先形态被折叠。"""
    out = []
    n = len(bars)
    if n < 5:
        return out
    for i in range(max(4, n - lookback), n):
        hits = []
        for det_id, fn in _DETECTORS.items():
            try:
                if fn(bars, i):
                    m = _meta(det_id)
                    kb_bars = (_KB.get(det_id, {}) or {}).get("bars", 1)
                    hits.append({"id": det_id, "name_cn": m["name_cn"], "bias": m["bias"],
                                 "reliability": m["reliability"], "d": bars[i]["d"],
                                 "price": bars[i]["h"] if m["bias"] == "bear" else bars[i]["l"],
                                 "_rk": (_REL_RANK.get(m["reliability"], 3), -int(kb_bars or 1))})
            except Exception:  # noqa: BLE001
                continue
        # 同一根K线:按 可靠度高→形态根数多 排序,取前 per_bar 个,避免堆叠
        hits.sort(key=lambda h: h["_rk"])
        for h in hits[:max(1, per_bar)]:
            h.pop("_rk", None)
            out.append(h)
    return out


def latest_comment(hits: list[dict]) -> str:
    """最近命中的形态转研判文字。**按交易日降序**取最近的日子(必含最新交易日),
    不足则并入次新日,保证信息量。旧实现按可靠度排序取前3,会把最新一两天的形态挤掉,
    导致文字停留在几天前、与图上最新K线形态对不上 —— 已改为以最新日期为准。"""
    if not hits:
        return "近端未识别到明显的经典K线组合形态。"
    tag = {"bull": "看涨", "bear": "看跌", "neutral": "中性"}
    by_day: dict[str, list[dict]] = {}
    for h in hits:                       # hits 为日期升序
        by_day.setdefault(h["d"], []).append(h)
    out, days_used = [], 0
    for d in sorted(by_day, reverse=True):
        out += [f"{h['d']} {h['name_cn']}({tag.get(h['bias'],'')},可靠度{h['reliability']})"
                for h in by_day[d]]
        days_used += 1
        if len(out) >= 3 or days_used >= 2:  # 够3个形态或已含最近2个交易日即停
            break
    return "近端K线形态:" + ";".join(out)
