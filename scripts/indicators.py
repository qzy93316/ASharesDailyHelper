# -*- coding: utf-8 -*-
"""技术指标计算 — 全部由代码计算,AI 只解读不计算。
输入: 日K DataFrame,需含列 [日期, 开盘, 收盘, 最高, 最低, 成交量]
"""
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> dict:
    """返回最新一根K线的全部指标快照。"""
    close, vol = df["收盘"], df["成交量"]

    # --- 均线 ---
    ma = {n: close.rolling(n).mean() for n in (5, 10, 20, 60)}
    ma5, ma10, ma20, ma60 = (ma[n].iloc[-1] for n in (5, 10, 20, 60))
    if ma5 > ma10 > ma20:
        alignment = "多头排列"
    elif ma5 < ma10 < ma20:
        alignment = "空头排列"
    elif ma5 > ma10:
        alignment = "弱多"
    else:
        alignment = "弱空"

    # --- MACD (12/26/9) ---
    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)
    hist = (dif - dea) * 2
    if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]:
        macd_cross = "金叉"
    elif dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]:
        macd_cross = "死叉"
    elif dif.iloc[-1] > dea.iloc[-1]:
        macd_cross = "多头持续"
    else:
        macd_cross = "空头持续"
    macd_above_zero = bool(dif.iloc[-1] > 0)

    # --- RSI (Wilder) ---
    def rsi(n):
        diff = close.diff()
        up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        rs = up / dn.replace(0, 1e-10)
        return (100 - 100 / (1 + rs)).iloc[-1]

    rsi6, rsi12, rsi24 = rsi(6), rsi(12), rsi(24)

    # --- 量比: 当日成交量 / 前5日均量 ---
    vol_ratio = vol.iloc[-1] / max(vol.iloc[-6:-1].mean(), 1)
    pct_chg = (close.iloc[-1] / close.iloc[-2] - 1) * 100
    if vol_ratio > 1.5 and pct_chg > 0:
        vol_pattern = "放量上涨"
    elif vol_ratio > 1.5 and pct_chg < 0:
        vol_pattern = "放量下跌"
    elif vol_ratio < 0.7 and pct_chg < 0:
        vol_pattern = "缩量回调"
    else:
        vol_pattern = "量能正常"

    # --- 乖离率 / 支撑压力 ---
    bias5 = (close.iloc[-1] / ma5 - 1) * 100
    support = round(min(ma20, df["最低"].iloc[-20:].min()), 2)
    pressure = round(max(df["最高"].iloc[-20:].max(), ma60 if pd.notna(ma60) else 0), 2)

    return {
        "date": str(df["日期"].iloc[-1]),
        "close": round(float(close.iloc[-1]), 2),
        "pct_chg": round(float(pct_chg), 2),
        "ma5": round(float(ma5), 2), "ma10": round(float(ma10), 2),
        "ma20": round(float(ma20), 2),
        "ma60": round(float(ma60), 2) if pd.notna(ma60) else None,
        "alignment": alignment,
        "dif": round(float(dif.iloc[-1]), 3), "dea": round(float(dea.iloc[-1]), 3),
        "macd_hist": round(float(hist.iloc[-1]), 3),
        "macd_cross": macd_cross, "macd_above_zero": macd_above_zero,
        "rsi6": round(float(rsi6), 1), "rsi12": round(float(rsi12), 1),
        "rsi24": round(float(rsi24), 1),
        "vol_ratio": round(float(vol_ratio), 2), "vol_pattern": vol_pattern,
        "bias5": round(float(bias5), 2),
        "support": float(support), "pressure": float(pressure),
    }


# 强弱研判"原子":全项目统一的趋势强弱判定,一律以均线排列 alignment 为准。
# judge/diagnose/action_plan 的 bull/weak/strong 判定都走这里,消除各处 `align in (...)` 口径漂移。
_BULL_ALIGN = ("多头排列", "弱多")
_WEAK_ALIGN = ("空头排列", "弱空")


def strength(ind: dict) -> dict:
    """趋势强弱单一真源。bull/weak 互补(alignment 恒为四值之一);strong 专指多头排列。
    grade 供展示统一口径,不参与决策。"""
    align = ind.get("alignment", "")
    return {"align": align,
            "bull": align in _BULL_ALIGN,
            "weak": align in _WEAK_ALIGN,
            "strong": align == "多头排列",
            "grade": {"多头排列": "强多", "弱多": "偏多",
                      "弱空": "偏弱", "空头排列": "弱势"}.get(align, "中性")}
