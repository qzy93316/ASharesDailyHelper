# -*- coding: utf-8 -*-
"""左侧反转候选检测器 —— 选股"反转赛道"的信号源(AI 零计算)。

背景:scoring 是趋势追涨器,系统性漏掉"即将转强"的左侧反转高价值股(见记忆 scoring-reversal-blindspot)。
本模块识别四类反转信号,**≥2 共振**方作反转候选,交由 global_scan/daily_report 以影子(shadow)方式入池,
不污染趋势评分。知识来源:kb/macd-divergence · volume-price-relations · gap-theory · trendline-drawing。

四类信号:
  底背离     —— 复用缠论 chan.divergence(1B),价新低而 MACD 不新低
  低位量增   —— 低位 + 放量 + 价平/温和上涨(主力打压建仓,量价关系"量增价平/价升")
  突破缺口   —— 向上跳空 + 放量 + 突破压力(真突破,缺口理论)
  强支撑反弹 —— 支撑≥2 触点 + 回踩贴近 + 当日收阳(画线操盘法强支撑)
"""
from __future__ import annotations

VOL_SURGE = 1.5      # 放量阈值(量比)
NEAR_SUP_PCT = 1.03  # 贴近支撑:现价 ≤ 支撑×1.03
LOW_POS_PCT = 0.25   # 低位:收盘处于近 N 日区间下 25%


def detect_gap(bars: list[dict]) -> dict | None:
    """最近一根 K 的跳空缺口。向上=今低>昨高,向下=今高<昨低。返回 {dir,size_pct} 或 None。"""
    if len(bars) < 2:
        return None
    prev, cur = bars[-2], bars[-1]
    if cur["l"] > prev["h"] and prev["h"] > 0:
        return {"dir": "up", "size_pct": round((cur["l"] - prev["h"]) / prev["h"] * 100, 2)}
    if cur["h"] < prev["l"] and prev["l"] > 0:
        return {"dir": "down", "size_pct": round((prev["l"] - cur["h"]) / prev["l"] * 100, 2)}
    return None


def _low_position(ind: dict, bars: list[dict], lookback: int = 60) -> bool:
    """低位:均线空头/弱空,或收盘处于近 lookback 日区间下 LOW_POS_PCT 分位。"""
    if ind.get("alignment", "") in ("空头排列", "弱空"):
        return True
    seg = bars[-lookback:]
    if not seg:
        return False
    lo = min(b["l"] for b in seg)
    hi = max(b["h"] for b in seg)
    if hi <= lo:
        return False
    return (ind["close"] - lo) / (hi - lo) <= LOW_POS_PCT


def _support_touches(chan_res: dict, ref: float, tol: float = 0.015) -> int:
    """支撑触点数:缠论笔的底分型中,价位落在 ref±tol 带内的个数(≥2 视为"强支撑")。"""
    if not chan_res:
        return 0
    bots = [p["price"] for p in (chan_res.get("bi") or []) if p.get("type") == "bottom"]
    if not bots or not ref:
        return 0
    return sum(1 for p in bots if abs(p - ref) / ref <= tol)


def reversal_signals(ind: dict, bars: list[dict], chan_res: dict | None = None) -> list[str]:
    """返回命中的反转信号名列表(去重)。调用方按 len(...)>=2 判定反转候选。"""
    sigs = []
    vr = ind.get("vol_ratio", 1) or 1
    pct = ind.get("pct_chg", 0) or 0
    low = _low_position(ind, bars)

    # 1) 底背离(缠论 1B)——价新低而 MACD 不新低
    if chan_res and (chan_res.get("divergence") or {}).get("bs") == "1B":
        sigs.append("底背离")

    # 2) 低位量增价平/价升 —— 低位 + 放量 + 价平到温和上涨(不追已大涨)
    if low and vr >= VOL_SURGE and -1 <= pct <= 6:
        sigs.append("低位量增")

    # 3) 突破缺口放量 —— 向上跳空 + 放量 + 收在压力位上方(真突破)
    gap = detect_gap(bars)
    press = ind.get("pressure")
    if gap and gap["dir"] == "up" and vr >= VOL_SURGE and press and ind["close"] >= press:
        sigs.append("突破缺口")

    # 4) 强支撑多触点反弹 —— 支撑≥2 触点 + 回踩贴近 + 当日收阳
    sup = ind.get("support")
    if sup and _support_touches(chan_res, sup) >= 2 and ind["close"] <= sup * NEAR_SUP_PCT \
            and bars and bars[-1]["c"] >= bars[-1]["o"]:
        sigs.append("强支撑反弹")

    return sigs


def is_reversal_candidate(ind: dict, bars: list[dict], chan_res: dict | None = None,
                          min_signals: int = 2) -> dict | None:
    """≥min_signals 共振则为反转候选。返回 {signals, n, entry, note} 或 None。"""
    sigs = reversal_signals(ind, bars, chan_res)
    if len(sigs) < min_signals:
        return None
    return {"signals": sigs, "n": len(sigs),
            "note": "反转共振:" + "、".join(sigs) + "(左侧候选,轻仓试,破支撑走)"}
