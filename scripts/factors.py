# -*- coding: utf-8 -*-
"""增量选股因子库(第二、三梯队) —— 从价格维度扩展到 筹码/资金/形态/事件/换手 维度。

设计原则("影子运行"):新因子只产出 **信号 + 分数调整 + 否决标记**,由 global_scan 决定
是否纳入正式池;所有命中都写入侧车的 `factors` 字段,供 backtest-review 按因子分组回测胜率,
经数据验证后再决定转正/剔除。凡涉及外部数据(解禁/业绩)的失败都优雅降级,不阻断主流程。
"""
from __future__ import annotations

import datetime as dt

import akshare as ak

# ---------------------------------------------------------------------------
# 第二梯队
# ---------------------------------------------------------------------------
def chip_factor(chip: dict, ind: dict, is_breakout: bool) -> dict:
    """筹码因子转筛选条件:
    - 趋势突破时 获利比例<60%(上方套牢盘少、阻力小)→ 加分;
    - 获利比例>90% 且 高位放量 → 主力兑现窗口,否决。"""
    if not chip:
        return {"delta": 0, "veto": False, "note": None}
    prof = chip.get("profit_ratio")
    if prof is None:
        return {"delta": 0, "veto": False, "note": None}
    vol_ratio = ind.get("vol_ratio", 1)
    if prof > 90 and vol_ratio > 1.5:
        return {"delta": 0, "veto": True,
                "note": f"获利比例{prof}%且高位放量(量比{vol_ratio}),主力兑现风险,否决"}
    if is_breakout and prof < 60:
        return {"delta": 5, "veto": False,
                "note": f"获利比例{prof}%<60%,上方套牢盘轻、突破阻力小(+5)"}
    return {"delta": 0, "veto": False, "note": None}


def fund_continuity_factor(flow: list[dict], ind: dict) -> dict:
    """主力资金连续性:
    - 连续3日主力净流入 + 股价横盘(振幅小)= 温和吸筹 → 潜伏加分;
    - 股价上涨 + 主力净流出 = 诱多 → 剔除标记。"""
    if not flow or len(flow) < 3:
        return {"delta": 0, "reject": False, "note": None, "accumulating": False}
    last3 = flow[-3:]
    main3 = [(x.get("super", 0) + x.get("big", 0)) for x in last3]
    all_in = all(m > 0 for m in main3)
    pct = ind.get("pct_chg", 0)
    main_today = main3[-1]
    if pct > 3 and main_today < 0:
        return {"delta": 0, "reject": True, "accumulating": False,
                "note": f"股价+{pct}%但主力净流出,量价背离疑诱多,剔除"}
    if all_in:
        bias = ind.get("bias5", 0)
        flat = abs(bias) < 3  # 贴近MA5=横盘
        if flat:
            return {"delta": 5, "reject": False, "accumulating": True,
                    "note": "连续3日主力净流入+股价横盘,温和吸筹形态(+5)"}
        return {"delta": 3, "reject": False, "accumulating": True,
                "note": "连续3日主力净流入(+3)"}
    return {"delta": 0, "reject": False, "note": None, "accumulating": False}


def vcp_factor(bars: list[dict]) -> dict:
    """VCP/箱体平台突破:20日振幅收窄到低位 + 缩量整理≥10日 + 放量突破箱体上沿。
    胜率口碑最好的经典形态之一。返回是否命中平台突破。"""
    if len(bars) < 40:
        return {"hit": False, "delta": 0, "note": None}
    win = bars[-20:]
    highs = [b["h"] for b in win]
    lows = [b["l"] for b in win]
    box_hi, box_lo = max(highs[:-1]), min(lows[:-1])  # 不含当日的箱体
    if box_hi <= 0:
        return {"hit": False, "delta": 0, "note": None}
    amp = (box_hi - box_lo) / box_lo * 100  # 前19日振幅
    # 与更早20日振幅比,是否收窄
    prev = bars[-40:-20]
    prev_amp = (max(b["h"] for b in prev) - min(b["l"] for b in prev)) / min(b["l"] for b in prev) * 100
    narrowing = amp < prev_amp * 0.8 and amp < 25
    # 整理期缩量:前19日均量 vs 更早均量
    v_recent = sum(b["v"] for b in win[:-1]) / 19
    v_prev = sum(b["v"] for b in prev) / 20
    shrink = v_recent < v_prev
    # 当日放量突破箱体上沿
    today = bars[-1]
    breakout = today["c"] > box_hi and today["v"] > v_recent * 1.5
    if narrowing and shrink and breakout:
        return {"hit": True, "delta": 6,
                "note": f"VCP平台突破:振幅收窄至{round(amp,1)}%+缩量整理后放量破箱体上沿{round(box_hi,2)}(+6)"}
    return {"hit": False, "delta": 0, "note": None}


def turnover_factor(turn_pct: float) -> dict:
    """换手率区间:题材股 5%~15% 为健康活跃区;<3% 无资金关注;>25% 极端分歧(常见于出货)。"""
    if turn_pct is None:
        return {"delta": 0, "note": None}
    if turn_pct > 25:
        return {"delta": -3, "note": f"换手{turn_pct}%>25%,极端分歧(常见出货),减分"}
    if turn_pct < 3:
        return {"delta": -2, "note": f"换手{turn_pct}%<3%,无资金关注,减分"}
    if 5 <= turn_pct <= 15:
        return {"delta": 3, "note": f"换手{turn_pct}%处于5%~15%健康活跃区(+3)"}
    return {"delta": 0, "note": None}


# ---------------------------------------------------------------------------
# 第三梯队:事件黑名单(全市场清单,盘前拉一次,供批量剔除)
# ---------------------------------------------------------------------------
def _norm(code) -> str:
    import re
    m = re.search(r"(\d{6})", str(code))
    return m.group(1) if m else str(code)


def event_blacklist(days_ahead: int = 14, unlock_ratio_pct: float = 3.0) -> dict:
    """盘前拉全市场事件清单,返回需规避的代码集合与原因。优雅降级:任一源失败即跳过该源。
    - 近 days_ahead 天大额解禁(占流通市值比 > unlock_ratio_pct)
    - 业绩预告为 预减/首亏/续亏/略减 等负面类型
    返回 {code: reason}。"""
    bl = {}
    today = dt.date.today()
    end = today + dt.timedelta(days=days_ahead)
    # 解禁
    try:
        df = ak.stock_restricted_release_detail_em(start_date=today.strftime("%Y%m%d"),
                                                   end_date=end.strftime("%Y%m%d"))
        col_ratio = next((c for c in df.columns if "流通市值比例" in c), None)
        for _, r in df.iterrows():
            code = _norm(r.get("股票代码", ""))
            ratio = r.get(col_ratio) if col_ratio else None
            try:
                ratio = float(ratio)
            except (ValueError, TypeError):
                ratio = None
            if ratio is not None and ratio > unlock_ratio_pct:
                bl[code] = f"近{days_ahead}日解禁占流通市值{round(ratio,1)}%(>{unlock_ratio_pct}%)"
    except Exception as e:  # noqa: BLE001
        print(f"    [事件黑名单] 解禁数据获取失败,跳过 —— {e}")
    # 业绩预告(负面)
    try:
        q = f"{today.year}{ 'Q1' if today.month<=4 else 'Q2' if today.month<=7 else 'Q3' if today.month<=10 else 'Q4'}"
        # akshare 用报告期日期,取最近一期
        rpt = f"{today.year}0331" if today.month <= 4 else f"{today.year}0630" if today.month <= 8 else f"{today.year}0930"
        df2 = ak.stock_yjyg_em(date=rpt)
        col_code = next((c for c in df2.columns if "代码" in c), None)
        col_type = next((c for c in df2.columns if "类型" in c or "预告" in c), None)
        neg = ("预减", "首亏", "续亏", "略减", "减亏", "不确定")
        if col_code and col_type:
            for _, r in df2.iterrows():
                if any(t in str(r.get(col_type, "")) for t in ("预减", "首亏", "续亏", "略减")):
                    bl.setdefault(_norm(r[col_code]), f"业绩预告负面({r.get(col_type)})")
    except Exception as e:  # noqa: BLE001
        print(f"    [事件黑名单] 业绩预告获取失败,跳过 —— {e}")
    return bl
