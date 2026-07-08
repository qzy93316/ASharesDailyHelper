# -*- coding: utf-8 -*-
"""6维度100分评分系统(移植自 Stock-Analysis-Skill 的评分思想)。
硬性规则在此强制执行,AI 不可绕过。
"""


def score_stock(ind: dict, hard_rules: dict, sig: dict | None = None) -> dict:
    b = {}

    # 1. 趋势(均线排列) 30分
    b["趋势"] = {"多头排列": 30, "弱多": 20, "弱空": 8, "空头排列": 0}[ind["alignment"]]

    # 2. 乖离率 20分 — 贴近/略低于MA5最佳,追高扣分
    bias = ind["bias5"]
    if -3 <= bias <= 1:
        b["乖离率"] = 20
    elif 1 < bias <= 3:
        b["乖离率"] = 14
    elif 3 < bias <= 5:
        b["乖离率"] = 8
    elif bias > 5:
        b["乖离率"] = 2
    else:  # bias < -3, 深跌
        b["乖离率"] = 10

    # 3. MACD 15分
    if ind["macd_cross"] == "金叉":
        b["MACD"] = 15 if ind["macd_above_zero"] else 12
    elif ind["macd_cross"] == "多头持续":
        b["MACD"] = 10
    elif ind["macd_cross"] == "空头持续":
        b["MACD"] = 4
    else:  # 死叉
        b["MACD"] = 0

    # 4. 量能 15分
    b["量能"] = {"缩量回调": 15, "放量上涨": 12, "量能正常": 8, "放量下跌": 0}[ind["vol_pattern"]]

    # 5. RSI 10分
    r = ind["rsi6"]
    if r < 20:
        b["RSI"] = 10
    elif r < 40:
        b["RSI"] = 8
    elif r < 60:
        b["RSI"] = 6
    elif r < 80:
        b["RSI"] = 3
    else:
        b["RSI"] = 0

    # 6. 支撑 10分
    dist = (ind["close"] - ind["support"]) / ind["close"] * 100
    b["支撑"] = 10 if 0 <= dist <= 5 else (6 if dist <= 10 else 3)

    # 信号确认(可选,封顶 ±4):新近(≤3日)MACD/KDJ/RSI 买卖信号多源共振微调,
    # 仅在传入 sig 时生效(点名/持仓诊断);全市场扫描不传 → 0,不影响选股基线。不参与硬否决。
    adj = 0
    if sig:
        fb = sum(1 for k in ("macd", "kdj", "rsi")
                 if (sig.get(k) or {}).get("dir") == "buy" and (sig[k].get("bars_ago", 99)) <= 3)
        fbr = sum(1 for k in ("macd", "kdj", "rsi")
                  if (sig.get(k) or {}).get("dir") == "sell" and (sig[k].get("bars_ago", 99)) <= 3)
        adj = max(-4, min(4, (fb - fbr) * 2))
        b["信号确认"] = adj

    total = sum(b.values())

    # --- 硬性规则(一票否决,禁止给出关注信号) ---
    vetoes = []
    if ind["rsi6"] > hard_rules.get("max_rsi6", 80):
        vetoes.append(f"RSI6={ind['rsi6']} 超买(>{hard_rules.get('max_rsi6', 80)}),禁止追入")
    if ind["bias5"] > hard_rules.get("max_bias5_pct", 5):
        vetoes.append(f"MA5乖离率 {ind['bias5']}% 过高(>{hard_rules.get('max_bias5_pct', 5)}%),不追高")

    if vetoes:
        signal = "⚪ 观望(硬规则否决)"
    elif total >= 75 and ind["alignment"] == "多头排列":
        signal = "🟢 重点关注"
    elif total >= 60:
        signal = "🔵 关注"
    elif total >= 45:
        signal = "🟡 持有观察"
    elif total >= 30:
        signal = "⚪ 观望"
    else:
        signal = "🔴 回避"

    return {"total": total, "breakdown": b, "signal": signal, "vetoes": vetoes}
