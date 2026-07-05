# -*- coding: utf-8 -*-
"""研判合成器 —— 把滞后指标综合成"有立场、带矛盾点、可执行"的交易研判,
替代机械的指标罗列。全部由代码计算(AI 零计算),字段对齐 indicators/chan 的真实输出。

产出:
  structural_stop 结构化止损(现价下方最近有效支撑-缓冲,非硬套-8%;过远则预警)
  risk_reward     基于结构止损与目标位的真实盈亏比 + 是否值博
  stance          一句话立场
  tensions        指标间矛盾点(趋势多头但RSI高/量价背离/浮盈盘重…)
  grey            灰色地带预警(RSI/乖离/换手接近危险区,不到硬否决)
  entry/invalidation 带价位的介入提示与失效条件
  quality         机会质量分(盈亏比为主,矛盾扣分,相对强度加分)—— 用于"今日之选"排序
"""
from __future__ import annotations


def _f(v):
    return None if v is None else round(float(v), 2)


def flow_sum(flow):
    """资金流序列 → 近5/20日各档(超大/大/中/小单)净额汇总。"""
    if not flow:
        return None
    out = {}
    for kk in ("super", "big", "mid", "small"):
        out["sum5_" + kk] = round(sum(x.get(kk, 0) for x in flow[-5:]), 1)
        out["sum20_" + kk] = round(sum(x.get(kk, 0) for x in flow[-20:]), 1)
    return out


def structural_stop(ind, chan_res, dd_pct):
    """结构化止损:现价下方最近的真实支撑(20日支撑/MA20/MA10/中枢下沿/缠论支撑)- 1.5%缓冲;
    与 -dd% 硬止损比较,结构止损更深则用硬止损保护;止损距离过远则预警。"""
    close = ind["close"]
    cands = []
    for key, label in [("support", "20日支撑"), ("ma20", "MA20"), ("ma10", "MA10")]:
        v = ind.get(key)
        if v and v < close:
            cands.append((float(v), label))
    if chan_res:
        zs = chan_res.get("zhongshu") or []
        if zs and zs[-1].get("zd") and zs[-1]["zd"] < close:
            cands.append((float(zs[-1]["zd"]), "中枢下沿"))
        sr = (chan_res.get("sr") or {}).get("support")
        if sr and sr < close:
            cands.append((float(sr), "缠论支撑"))
    hard = round(close * (1 - dd_pct / 100), 2)
    if cands:
        sup, src = max(cands, key=lambda x: x[0])  # 最近(最高)的下方支撑
        stop = round(sup * 0.985, 2)
        if stop < hard:
            stop, src, sup = hard, f"-{dd_pct:.0f}%硬止损(结构支撑{_f(sup)}更深)", sup
    else:
        stop, src, sup = hard, f"-{dd_pct:.0f}%硬止损(下方无明确支撑)", None
    dist = round((close - stop) / close * 100, 1)
    return {"stop": stop, "basis": src, "support_ref": _f(sup),
            "dist_pct": dist, "too_far": dist > dd_pct + 3}


def risk_reward(close, stop, target):
    risk = max(close - stop, 1e-6)
    reward = max(target - close, 0)
    rr = round(reward / risk, 2)
    verdict = ("盈亏比佳,值博" if rr >= 2 else "盈亏比尚可" if rr >= 1.5 else
               "盈亏比一般,轻仓" if rr >= 1 else "盈亏比不佳,不值博")
    return {"rr": rr, "verdict": verdict,
            "risk_pct": round(risk / close * 100, 1), "reward_pct": round(reward / close * 100, 1)}


def _tensions(ind, chip, fs):
    t = []
    align = ind.get("alignment", "")
    rsi, bias, vr = ind.get("rsi6"), ind.get("bias5"), ind.get("vol_ratio", 1) or 1
    bullish = align in ("多头排列", "弱多")
    if bullish and rsi is not None and rsi >= 65:
        t.append(f"趋势健康但 RSI{rsi} 偏高,短线过热,追高易套")
    if bullish and bias is not None and bias >= 3 and vr >= 1.5:
        t.append(f"放量拉升(量比{vr})+乖离{bias}%,动能强但已透支,防回踩")
    if fs:
        m5 = fs.get("sum5_super", 0) + fs.get("sum5_big", 0)
        if ind.get("pct_chg", 0) > 2 and m5 < 0:
            t.append(f"股价上涨但主力近5日净流出{abs(round(m5))}万,量价背离,谨防诱多")
        if not bullish and m5 > 0:
            t.append(f"趋势偏弱但主力近5日净流入{round(m5)}万,资金逆势吸筹,可跟踪")
    if chip:
        prof = chip.get("profit_ratio")
        if prof is not None and prof >= 90:
            t.append(f"获利比例{prof}%,几乎全员浮盈,上方抛压重,防兑现")
        elif prof is not None and prof <= 15 and bullish:
            t.append(f"获利比例仅{prof}%,上方套牢盘重,反弹到套牢区易受阻")
    if align in ("空头排列", "弱空") and rsi is not None and rsi <= 35:
        t.append(f"空头排列+RSI{rsi}超卖,或有超跌反弹,但趋势未反转属左侧博弈")
    return t


def _grey(ind, turn_pct):
    g = []
    rsi, bias = ind.get("rsi6"), ind.get("bias5")
    if rsi is not None and 65 <= rsi <= 80:
        g.append(f"RSI{rsi} 进入偏热区(65-80),离超买不远")
    if bias is not None and 3 <= bias <= 5:
        g.append(f"MA5乖离{bias}% 接近追高线(5%)")
    if turn_pct is not None and turn_pct > 25:
        g.append(f"换手{turn_pct}% 过高,分歧大(常见于出货/情绪顶)")
    return g


def synthesize(ind, chip, fs, chan_res, target, dd_pct, turn_pct=None, strategy=""):
    close = ind["close"]
    ss = structural_stop(ind, chan_res, dd_pct)
    rr = risk_reward(close, ss["stop"], target)
    tensions = _tensions(ind, chip, fs)
    grey = _grey(ind, turn_pct)
    align = ind.get("alignment", "")
    rsi, bias = ind.get("rsi6"), ind.get("bias5")
    ma10 = ind.get("ma10")

    if strategy == "超跌回调":
        stance = "超跌企稳,左侧低吸候选(趋势未反转,轻仓试)"
    elif align == "多头排列" and (bias is None or bias < 3) and (rsi is None or rsi < 65):
        stance = "顺势多头,结构健康,回踩可介入"
    elif align in ("多头排列", "弱多"):
        stance = "偏多但短线有透支,不宜追高、等回踩"
    elif align in ("空头排列", "弱空"):
        stance = "趋势偏弱,反弹为主、不宜恋战"
    else:
        stance = "多空交织,观望为宜"

    if strategy == "超跌回调":
        entry = f"贴近支撑{ss['support_ref']}分批试,不破可留、破位走"
    elif bias is not None and bias >= 3:
        entry = f"当前乖离偏高,等回踩 MA10({_f(ma10)}) 附近再介入更稳"
    elif ma10 and close >= ma10:
        entry = f"站稳 MA10({_f(ma10)}) 之上可跟,回踩不破持有"
    else:
        entry = "等待站上 MA10 或明确企稳信号"

    invalidation = f"跌破结构止损 {ss['stop']}({ss['basis']})离场;或所属板块龙头转弱、热点降温"

    q = min(rr["rr"], 3) * 10 - len(tensions) * 4 - (6 if ss["too_far"] else 0)
    q += 6 if align == "多头排列" else (2 if align == "弱多" else 0)

    return {"stance": stance, "structural_stop": ss, "risk_reward": rr,
            "tensions": tensions, "grey": grey, "entry": entry,
            "invalidation": invalidation, "quality": round(q, 1)}
