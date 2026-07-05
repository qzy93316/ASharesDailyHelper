# -*- coding: utf-8 -*-
"""筹码分布(CYQ 成本分布)估算 —— 纯由 K线 + 换手率计算,不依赖付费接口。

原理(经典三角形分布衰减模型,业界通用做法,"拿来主义"):
  - 每个交易日把当日成交量按「三角形分布」撒在当日 [最低, 最高] 价区间,
    峰值在均价((高+低+收)/3)附近;
  - 已有筹码按 (1 - 当日换手率×衰减系数) 衰减(老筹码随换手逐步被换手离场);
  - 逐日累加,得到「当前」在各价位上的持仓筹码量。

由此派生的控盘类指标:
  - 平均成本、获利比例(现价之下的筹码占比)
  - 90% / 70% 成本集中区间(区间越窄 = 筹码越集中 = 控盘度越高)

注:分布为估算值(非真实账户持仓),用于研判筹码结构,不作精确依据。
"""
from __future__ import annotations


def compute_chips(bars: list[dict], bins: int = 60, decay: float = 1.0):
    """bars: [{o,c,l,h,v, 换手}]，需含 '换手'(小数换手率)。
    返回 dict:price_levels/amounts/current/avg_cost/profit_ratio/conc_90/conc_70 等。"""
    if not bars:
        return None
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    edges = [lo + i * step for i in range(bins + 1)]
    centers = [(edges[i] + edges[i + 1]) / 2 for i in range(bins)]
    chips = [0.0] * bins  # 各价格桶的累计筹码量

    for b in bars:
        low, high, close = b["l"], b["h"], b["c"]
        vol = float(b.get("v") or 0)
        turn = float(b.get("换手") or 0)
        turn = min(max(turn, 0.0), 1.0)
        if vol <= 0 or high < low:
            continue
        avg = (high + low + close) / 3.0
        # 当日筹码在 [low,high] 上的三角形权重(峰值在均价)
        weights = []
        for i in range(bins):
            c = centers[i]
            if c < low or c > high:
                weights.append(0.0)
            elif c <= avg:
                weights.append((c - low) / (avg - low) if avg > low else 1.0)
            else:
                weights.append((high - c) / (high - avg) if high > avg else 1.0)
        wsum = sum(weights)
        if wsum <= 0:
            continue
        # 老筹码衰减,新筹码按换手比例注入(单支股票总量归一到"换手主导"的相对分布)
        d = turn * decay
        today_vol = vol  # 以成交量为当日新增筹码量的度量
        for i in range(bins):
            chips[i] = chips[i] * (1 - d) + today_vol * d * (weights[i] / wsum)

    total = sum(chips)
    if total <= 0:
        return None
    current = bars[-1]["c"]
    # 获利比例:现价之下(成本低于现价)的筹码占比
    profit = sum(chips[i] for i in range(bins) if centers[i] <= current) / total
    avg_cost = sum(centers[i] * chips[i] for i in range(bins)) / total

    def concentration(pct: float):
        """取累计占 pct 的最窄价格区间,返回 (low, high, 集中度%)。"""
        order = sorted(range(bins), key=lambda i: chips[i], reverse=True)
        acc, chosen = 0.0, []
        for i in order:
            chosen.append(i)
            acc += chips[i]
            if acc >= pct * total:
                break
        clo = min(centers[i] for i in chosen)
        chi = max(centers[i] for i in chosen)
        conc = (chi - clo) / (chi + clo) * 2 * 100 if (chi + clo) else 0.0  # 区间宽度/中值
        return round(clo, 2), round(chi, 2), round(conc, 1)

    lo90, hi90, conc90 = concentration(0.90)
    lo70, hi70, conc70 = concentration(0.70)
    return {
        "price_levels": [round(c, 2) for c in centers],
        "amounts": [round(x / total * 100, 3) for x in chips],  # 各价位占比 %
        "current": round(current, 2),
        "avg_cost": round(avg_cost, 2),
        "profit_ratio": round(profit * 100, 1),
        "conc_90": {"low": lo90, "high": hi90, "spread_pct": conc90},
        "conc_70": {"low": lo70, "high": hi70, "spread_pct": conc70},
    }


def control_comment(chip: dict) -> str:
    """把筹码指标翻成控盘研判文字。"""
    if not chip:
        return "筹码数据不足,无法估算。"
    pr = chip["profit_ratio"]
    c90 = chip["conc_90"]["spread_pct"]
    avg = chip["avg_cost"]
    cur = chip["current"]
    conc_txt = ("高度集中(控盘度高)" if c90 < 15 else
                "较集中" if c90 < 25 else "较分散(散户化)")
    prof_txt = ("多数持仓者获利(上方抛压小)" if pr >= 70 else
                "获利盘适中" if pr >= 40 else "多数套牢(上方套牢盘重,反弹有压力)")
    pos = "现价高于平均成本,主力/多数筹码浮盈" if cur >= avg else "现价低于平均成本,浮亏筹码为主"
    return (f"90%筹码集中于 {chip['conc_90']['low']}~{chip['conc_90']['high']}(集中度{c90}%,{conc_txt});"
            f"平均成本 {avg},{pos};获利比例 {pr}%,{prof_txt}。")
