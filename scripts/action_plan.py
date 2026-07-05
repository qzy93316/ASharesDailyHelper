# -*- coding: utf-8 -*-
"""每日作战方案 —— 把 持仓 + 荐股池 的个股研判跨股合成为"带价位、带条件的组合动作"。
不新增行情请求,只读当日三份侧车(持仓诊断/日报/全局池)里已算好的 judge/indicators/verdict。

产出(结构化,供报告渲染 + 盘中 watch.py 取计划价位):
  posture   组合层面:regime+情绪 → 总仓位建议 / 是否可新建仓 / 集中度预警
  holdings  持仓端动作:清仓/减仓/持有/逢低加仓 + 触发价 + 依据
  pool      荐股端动作:建仓/逢低吸/逢高突破建仓/放弃 + 计划价位(介入/止损/目标)
  swaps     换股建议:卖弱(持仓)→ 买强(荐股 quality 高)

纪律:全部基于事先算好的计划价位,系统帮守纪律;研究参考,非实时喊单、不代下单。

用法:
  python action_plan.py                       # 当日,持仓+荐股池全量
  python action_plan.py --date 2026-07-05
  python action_plan.py --focus 持仓           # 只看持仓
  python action_plan.py --focus 002171,冰轮     # 只看指定代码/名称(逗号分隔)
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).parent.parent


def _load(date: str, kind: str):
    """读当日某类侧车(kind: 持仓诊断/日报/全局池)。缺失返回 None。"""
    day = date.replace("-", "")
    p = ROOT / "reports" / day / f"{kind}-{date}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def _pos_advice(regime, emotion):
    """组合总仓位建议:regime 定基准仓,情绪周期微调,给是否可新建仓。"""
    level = (regime or {}).get("level", "谨慎")
    phase = (emotion or {}).get("phase", "分歧")
    base = {"进攻": 8, "谨慎": 6, "防守": 4}.get(level, 5)
    adj = {"高潮": 0, "发酵": 1, "分歧": 0, "退潮": -1, "冰点": -2}.get(phase, 0)
    target = max(2, min(9, base + adj))
    new_buy_ok = not (level == "防守" and phase in ("退潮", "冰点"))
    note = (f"大盘{level} + 情绪{phase} → 建议总仓位约 {target} 成;"
            + ("可择机新建仓(严守计划价位)" if new_buy_ok
               else "防守+退潮,原则上不新建仓,以调结构/降仓为主"))
    return {"target_position_pct": target * 10, "new_buy_ok": new_buy_ok,
            "regime_level": level, "emotion_phase": phase, "note": note}


def _holding_action(p):
    """持仓端:据诊断标签(holding_tag)+ judge 映射为动作 + 触发价。"""
    tag = p.get("holding_tag", "")
    jd = p.get("judge") or {}
    stop = (jd.get("structural_stop") or {}).get("stop")
    i = p.get("indicators") or {}
    cost, price = p.get("cost"), i.get("close")
    press = i.get("pressure")
    ma10 = i.get("ma10")
    if tag == "止损":
        return {"action": "清仓/止损", "trigger": stop, "ratio": "100%",
                "note": f"已破结构止损 {stop},纪律离场,勿扛"}
    if tag == "重亏警戒":
        return {"action": "减仓", "trigger": press or cost, "ratio": "1/3~1/2",
                "note": f"浮亏过深,反弹到 {press or cost} 附近减亏,不补仓摊低;破 {stop} 清"}
    if tag == "逢反弹减":
        return {"action": "减仓", "trigger": press or cost, "ratio": "1/3",
                "note": f"趋势资金双弱,反弹到压力/成本 {press or cost} 减,守 {stop}"}
    if tag == "持有观察":
        # 逆势吸筹的强结构可考虑逢低加仓
        add = ma10 if (ma10 and price and price > ma10) else stop
        return {"action": "逢低加仓/持有", "trigger": add, "ratio": "≤1/3",
                "note": f"资金逆势吸筹,回踩 {add} 不破可小幅加,守 {stop}"}
    # 持有 / 观察
    return {"action": "持有", "trigger": stop, "ratio": "—",
            "note": f"结构未破,守结构止损 {stop};跌破离场"}


def _pool_action(p):
    """荐股端:据 judge/indicators 映射为 建仓/逢低吸/逢高突破/放弃 + 计划价位。"""
    i = p.get("indicators") or {}
    jd = p.get("judge") or {}
    rr = (jd.get("risk_reward") or {}).get("rr", 0)
    q = jd.get("quality", 0)
    align = i.get("alignment", "")
    bias, ma10, close = i.get("bias5"), i.get("ma10"), i.get("close")
    stop = (jd.get("structural_stop") or {}).get("stop")
    target = p.get("plan_target") or i.get("pressure")
    press = i.get("pressure")
    common = {"entry": jd.get("entry", ""), "stop": stop, "target": target,
              "rr": rr, "quality": q, "invalidation": jd.get("invalidation", "")}
    if p.get("shadow"):
        return {"action": "观察(超跌影子)", "plan_price": (jd.get("structural_stop") or {}).get("support_ref"),
                **common, "note": "左侧超跌企稳候选,仅轻仓试,不追"}
    if rr is not None and rr < 1:
        return {"action": "放弃", "plan_price": None, **common,
                "note": f"盈亏比 {rr}<1 不值博,放弃或等更好点位"}
    if bias is not None and bias >= 3:
        return {"action": "逢低吸", "plan_price": ma10, **common,
                "note": f"乖离{bias}%偏高,等回踩 MA10({ma10}) 再介入"}
    if ma10 and close and close >= ma10 and align in ("多头排列", "弱多"):
        return {"action": "建仓", "plan_price": ma10, **common,
                "note": f"站稳 MA10({ma10}),介入止损 {stop},目标 {target}"}
    if ma10 and close and close < ma10:
        return {"action": "逢高突破建仓", "plan_price": press, **common,
                "note": f"暂在 MA10 下,放量突破压力 {press} 再确认介入"}
    return {"action": "观察", "plan_price": ma10, **common,
            "note": "等站上 MA10 或明确企稳信号"}


def _match_focus(p, focus):
    if not focus:
        return True
    if focus == "持仓" or focus == "荐股":
        return True  # 范围过滤在上层做
    keys = [k.strip() for k in focus.split(",") if k.strip()]
    return any(k == p.get("code") or k in (p.get("name") or "") for k in keys)


def build_action_plan(date: str, focus: str | None = None) -> dict:
    diag = _load(date, "持仓诊断")
    daily = _load(date, "日报")
    glob = _load(date, "全局池")
    regime = (daily or {}).get("regime") or (glob or {}).get("regime")
    emotion = (daily or {}).get("emotion") or (glob or {}).get("emotion")

    scope_holdings = focus != "荐股"
    scope_pool = focus != "持仓"
    holdings, pool = [], []
    if scope_holdings and diag:
        for p in diag.get("picks", []):
            if not _match_focus(p, focus if focus not in ("持仓", "荐股") else None):
                continue
            act = _holding_action(p)
            holdings.append({"code": p["code"], "name": p["name"], "cost": p.get("cost"),
                             "price": (p.get("indicators") or {}).get("close"),
                             "pnl_pct": p.get("pnl_pct"), "tag": p.get("holding_tag"),
                             "signal": p.get("signal"), **act})
    if scope_pool:
        seen = {h["code"] for h in holdings}
        for src in (daily, glob):
            for p in (src or {}).get("picks", []):
                if p["code"] in seen:
                    continue
                if not _match_focus(p, focus if focus not in ("持仓", "荐股") else None):
                    continue
                seen.add(p["code"])
                act = _pool_action(p)
                pool.append({"code": p["code"], "name": p["name"],
                             "sector": p.get("sector", ""), "pool": p.get("pool", "热点"),
                             "signal": p.get("signal"), "score": p.get("score"), **act})
        pool.sort(key=lambda x: (x.get("quality") or -99), reverse=True)

    # 换股:卖弱(持仓减/清)→ 买强(荐股 建仓/逢低吸 且 quality 高)
    sells = [h for h in holdings if h["action"] in ("清仓/止损", "减仓")]
    buys = [p for p in pool if p["action"] in ("建仓", "逢低吸")]
    swaps = []
    for s, b in zip(sells, buys[:len(sells)]):
        swaps.append({"sell": f"{s['name']}({s['code']})", "buy": f"{b['name']}({b['code']})",
                      "note": f"{s['action']} {s['name']} 腾资金,择机换入 {b['name']}"
                              f"(质量分{b.get('quality')},{b['action']} @ {b.get('plan_price')})"})

    posture = _pos_advice(regime, emotion)
    if diag and diag.get("portfolio"):
        pf = diag["portfolio"]
        if pf.get("top_concentration_pct") and pf["top_concentration_pct"] >= 40:
            posture["concentration_warn"] = (
                f"最大单票 {pf.get('heaviest')} 占 {pf['top_concentration_pct']}%,集中度偏高,注意分散")
        posture["position_pct"] = pf.get("position_pct")
    return {"date": date, "focus": focus, "posture": posture,
            "holdings": holdings, "pool": pool, "swaps": swaps,
            "has_holdings": bool(diag)}


def render_md(ap: dict) -> str:
    L = [f"# 今日作战方案 — {ap['date']}", "",
         "> ⚠️ 研究性组合研判,**非实时喊单、不代下单**;动作均绑事先算好的计划价位,系统帮守纪律。", "",
         "## 组合姿态", "", f"- {ap['posture']['note']}"]
    if ap["posture"].get("position_pct") is not None:
        L.append(f"- 当前仓位 {ap['posture']['position_pct']}%")
    if ap["posture"].get("concentration_warn"):
        L.append(f"- ⚠️ {ap['posture']['concentration_warn']}")
    if ap["holdings"]:
        L += ["", "## 持仓端动作", "",
              "| 股票 | 成本/现价 | 盈亏% | 动作 | 触发价 | 幅度 | 依据 |",
              "|---|---|---|---|---|---|---|"]
        for h in ap["holdings"]:
            L.append(f"| {h['name']}({h['code']}) | {h.get('cost')}/{h.get('price')} | "
                     f"{h.get('pnl_pct')}% | **{h['action']}** | {h.get('trigger')} | "
                     f"{h.get('ratio')} | {h['note']} |")
    if ap["pool"]:
        L += ["", "## 荐股端动作", "",
              "| 股票 | 池/板块 | 信号 | 动作 | 计划价 | 止损 | 目标 | 盈亏比 | 依据 |",
              "|---|---|---|---|---|---|---|---|---|"]
        for p in ap["pool"]:
            L.append(f"| {p['name']}({p['code']}) | {p.get('pool')}·{p.get('sector')} | {p.get('signal')} | "
                     f"**{p['action']}** | {p.get('plan_price')} | {p.get('stop')} | {p.get('target')} | "
                     f"{p.get('rr')} | {p['note']} |")
    if ap["swaps"]:
        L += ["", "## 换股建议(卖弱买强)", ""]
        for s in ap["swaps"]:
            L.append(f"- {s['note']}")
    L += ["", "---", "*动作由代码据 judge 计划价位合成(AI零计算);仅供研究,请自行复核决策。*"]
    return "\n".join(L)


def main() -> None:
    ap_arg = argparse.ArgumentParser(description="每日作战方案")
    ap_arg.add_argument("--date", default=dt.date.today().isoformat())
    ap_arg.add_argument("--focus", default=None, help="持仓 / 荐股 / 代码或名称(逗号分隔)")
    args = ap_arg.parse_args()
    ap = build_action_plan(args.date, args.focus)
    if not ap["holdings"] and not ap["pool"]:
        print(f"{args.date} 无持仓诊断/荐股池侧车,或 focus 无匹配。先跑 run_workflow / diagnose_portfolio。")
        return
    day_dir = ROOT / "reports" / args.date.replace("-", "")
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"作战方案-{args.date}.json").write_text(
        json.dumps(ap, ensure_ascii=False, indent=2), encoding="utf-8")
    (day_dir / f"作战方案-{args.date}.md").write_text(render_md(ap), encoding="utf-8")
    print(f"完成 → {day_dir / f'作战方案-{args.date}.md'}")
    print(f"\n{ap['posture']['note']}")
    for h in ap["holdings"]:
        print(f"  [持仓·{h['action']}] {h['name']} 成本{h.get('cost')}/现{h.get('price')}"
              f"({h.get('pnl_pct')}%) 触发{h.get('trigger')} — {h['note']}")
    for p in ap["pool"]:
        print(f"  [荐股·{p['action']}] {p['name']} 计划价{p.get('plan_price')} 止损{p.get('stop')}"
              f" 盈亏比{p.get('rr')} — {p['note']}")
    for s in ap["swaps"]:
        print(f"  [换股] {s['note']}")


if __name__ == "__main__":
    main()
