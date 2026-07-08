# -*- coding: utf-8 -*-
"""逐日技术信号层 —— 从已算好的指标序列(MACD/KDJ/RSI/量能)提取买卖信号点。

设计(呼应「AI 零计算」):信号全在本模块用纯函数算好,
① 供图表在副指标区画买卖箭头;② (Phase 2)供 judge/scoring 反哺研判。
输入是 report_to_html._series 产出的同构序列 dict(dates/dif/dea/macd/kdj_k/kdj_d/
kdj_j/rsi6/vol/mavol5…),不联网、无第三方依赖,便于 selftest 断言。

每个信号点:{"d": 日期, "dir": "buy"|"sell", "y": 该副指标上的纵坐标, "kind": 触发原因}
  dir=buy → 图上红色↑(看多);dir=sell → 绿色↓(看空)。y 用于 markPoint 定位到对应副图。
"""


def _f(v):
    """安全取 float;None/NaN/空 → None。"""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else v  # NaN


def _dedup(points: list[dict], min_gap: int = 3) -> list[dict]:
    """同方向信号在 min_gap 根内只留第一个,降噪(索引已随点携带 _i)。"""
    out, last = [], {}
    for p in points:
        i, d = p["_i"], p["dir"]
        if d in last and i - last[d] < min_gap:
            continue
        last[d] = i
        out.append(p)
    return out


def _cross(fast: list, slow: list, dates: list, y_from_slow=True):
    """fast 上穿 slow → buy;下穿 → sell。返回带 _i 的点(未去重)。"""
    pts = []
    for i in range(1, len(fast)):
        a0, a1 = _f(fast[i - 1]), _f(fast[i])
        b0, b1 = _f(slow[i - 1]), _f(slow[i])
        if None in (a0, a1, b0, b1):
            continue
        y = _f(slow[i]) if y_from_slow else _f(fast[i])
        if a0 <= b0 and a1 > b1:
            pts.append({"_i": i, "d": dates[i], "dir": "buy", "y": y})
        elif a0 >= b0 and a1 < b1:
            pts.append({"_i": i, "d": dates[i], "dir": "sell", "y": y})
    return pts


def macd_signals(ser: dict) -> list[dict]:
    """MACD 金叉(DIF 上穿 DEA)=buy、死叉=sell;要求柱(macd)方向与之一致做确认降噪。"""
    dif, dea, hist, dates = ser.get("dif", []), ser.get("dea", []), ser.get("macd", []), ser.get("dates", [])
    out = []
    for p in _cross(dif, dea, dates):
        h = _f(hist[p["_i"]])
        # 确认:金叉时柱应转正/近正,死叉时柱应转负/近负,过滤零轴附近来回穿的噪声
        if h is not None and ((p["dir"] == "buy" and h < -1e-6) or (p["dir"] == "sell" and h > 1e-6)):
            continue
        p["kind"] = "MACD金叉" if p["dir"] == "buy" else "MACD死叉"
        out.append(p)
    return _dedup(out)


def kdj_signals(ser: dict) -> list[dict]:
    """KDJ:K 上穿 D 且在中低位(D<80)=buy;K 下穿 D 且在中高位(D>20)=sell。"""
    k, d, dates = ser.get("kdj_k", []), ser.get("kdj_d", []), ser.get("dates", [])
    out = []
    for p in _cross(k, d, dates):
        dv = _f(d[p["_i"]])
        if dv is None:
            continue
        if p["dir"] == "buy" and dv >= 80:      # 高位金叉意义弱,过滤
            continue
        if p["dir"] == "sell" and dv <= 20:     # 低位死叉意义弱,过滤
            continue
        p["kind"] = "KDJ金叉" if p["dir"] == "buy" else "KDJ死叉"
        out.append(p)
    return _dedup(out)


def rsi_signals(ser: dict, low: float = 20, high: float = 80) -> list[dict]:
    """RSI6 上穿超卖线(<low→上)=buy;下穿超买线(>high→下)=sell。"""
    r, dates = ser.get("rsi6", []), ser.get("dates", [])
    out = []
    for i in range(1, len(r)):
        a0, a1 = _f(r[i - 1]), _f(r[i])
        if None in (a0, a1):
            continue
        if a0 <= low and a1 > low:
            out.append({"_i": i, "d": dates[i], "dir": "buy", "y": a1, "kind": "RSI超卖回升"})
        elif a0 >= high and a1 < high:
            out.append({"_i": i, "d": dates[i], "dir": "sell", "y": a1, "kind": "RSI超买回落"})
    return _dedup(out)


def vol_signals(ser: dict, ratio: float = 2.0) -> list[dict]:
    """放量异动:量 ≥ MAVOL5×ratio。dir 依当日 K 线涨跌(收≥开=buy 放量上攻,否则 sell)。"""
    vol, dates, kline = ser.get("vol", []), ser.get("dates", []), ser.get("kline", [])
    mav = ser.get("mavol5") or _sma(vol, 5)
    out = []
    for i in range(len(vol)):
        v, m = _f(vol[i]), _f(mav[i]) if i < len(mav) else None
        if None in (v, m) or m <= 0 or v < m * ratio:
            continue
        up = True
        if i < len(kline) and kline[i] and len(kline[i]) >= 2:
            up = _f(kline[i][1]) >= _f(kline[i][0])   # kline=[o,c,l,h]
        out.append({"_i": i, "d": dates[i], "dir": "buy" if up else "sell",
                    "y": v, "kind": "放量上攻" if up else "放量下杀"})
    return _dedup(out, min_gap=2)


def _sma(arr: list, n: int) -> list:
    out, s, q = [], 0.0, []
    for v in arr:
        f = _f(v)
        q.append(f if f is not None else 0.0)
        s += q[-1]
        if len(q) > n:
            s -= q.pop(0)
        out.append(round(s / len(q), 1) if q else None)
    return out


def series_from_bars(bars: list[dict]) -> dict:
    """从 bars(OHLCV)算出信号所需的逐日指标序列,公式与 report_to_html._series 一致,
    保证图上箭头与研判信号同源一致。供 analyze_one 在研判前调用。"""
    import pandas as pd
    df = pd.DataFrame(bars)
    close, high, low = df["c"], df["h"], df["l"]

    def r(s, nd=3):
        return [None if pd.isna(v) else round(float(v), nd) for v in s]

    def ema(s, n):
        return s.ewm(span=n, adjust=False).mean()
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    hist = (dif - dea) * 2

    def rsi(n):
        d = close.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, 1e-10))
    ln, hn = low.rolling(9).min(), high.rolling(9).max()
    rsv = (close - ln) / (hn - ln).replace(0, 1e-10) * 100
    kk = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    dd = kk.ewm(alpha=1 / 3, adjust=False).mean()
    jj = 3 * kk - 2 * dd
    vol = df["v"] / 1e4
    return {"dates": [b["d"] for b in bars],
            "kline": [[b["o"], b["c"], b["l"], b["h"]] for b in bars],
            "vol": [round(v, 1) for v in vol], "mavol5": r(vol.rolling(5).mean(), 1),
            "dif": r(dif), "dea": r(dea), "macd": r(hist),
            "kdj_k": r(kk, 1), "kdj_d": r(dd, 1), "kdj_j": r(jj, 1), "rsi6": r(rsi(6), 1)}


def compute(ser: dict) -> dict:
    """一次性算出四类逐日信号(图上画箭头用)。去掉内部 _i。"""
    def clean(pts):
        for p in pts:
            p.pop("_i", None)
        return pts
    return {"macd": clean(macd_signals(ser)), "kdj": clean(kdj_signals(ser)),
            "rsi": clean(rsi_signals(ser)), "vol": clean(vol_signals(ser))}


def latest_summary(sig: dict, dates: list) -> dict:
    """(Phase 2 备用)每类最新信号 + 距今根数,供 judge 判"新近"程度。"""
    n = len(dates)
    idx = {d: i for i, d in enumerate(dates)}
    out = {}
    for key, pts in sig.items():
        if not pts:
            continue
        last = pts[-1]
        out[key] = {"dir": last["dir"], "kind": last.get("kind", ""),
                    "d": last["d"], "bars_ago": n - 1 - idx.get(last["d"], n - 1)}
    return out
