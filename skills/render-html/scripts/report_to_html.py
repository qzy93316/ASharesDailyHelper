# -*- coding: utf-8 -*-
"""富HTML日报渲染器(交互版 v2.2) —— 读取 daily_report/analyze/global_scan 的 JSON 侧车,
产出类同花顺的交互面板。

个股详析:
  · 信息栏(开/收/高/低/涨跌幅/换手/成交额/量比)
  · 主图蜡烛 + MA5/10/20/60 + BOLL + 目标/止损/现价(中文 tooltip)
  · 主图 Tab:K线 / 缠论(分型/笔/线段/中枢/买卖点) / 形态(支撑压力/颈线) / K线形态(70种)
  · 动态副指标区:默认 MACD,可 + 增至 3 个,每个下拉切换(MACD/RSI/KDJ/WR/量/BIAS,不重复),严格对齐K线
  · 筹码分布(CYQ,价格刻度对齐)+ 资金流向(超大/大/中/小单四档)
  · 研判文字:关键指标可鼠标悬浮看小白解释+事例(glossary.json)

图表用 Apache ECharts(CDN);指标/缠论/形态由 pandas + chan.py 计算,JS 只绘制。
用法:python report_to_html.py <侧车.json> [-o out.html]
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills" / "trading-memory" / "scripts"))
import chan  # noqa: E402
import candle_patterns  # noqa: E402
import signals as _signals  # noqa: E402  逐日买卖信号(图上画箭头)
try:
    import ledger as _ledger  # noqa: E402  操盘台账(B/T/S 标注 + 成本线)
except Exception:  # noqa: BLE001
    _ledger = None

ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"
GLOSSARY_PATH = Path(__file__).resolve().parents[3] / "knowledge" / "kb" / "glossary.json"


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _series(bars):
    df = pd.DataFrame(bars)
    close, high, low = df["c"], df["h"], df["l"]

    def r(s, nd=3):
        return [None if pd.isna(v) else round(float(v), nd) for v in s]

    ma = {n: close.rolling(n).mean() for n in (5, 10, 20, 60)}
    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)
    hist = (dif - dea) * 2

    def rsi(n):
        d = close.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, 1e-10))

    # KDJ(9,3,3)
    ln = low.rolling(9).min()
    hn = high.rolling(9).max()
    rsv = (close - ln) / (hn - ln).replace(0, 1e-10) * 100
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d_ = k.ewm(alpha=1 / 3, adjust=False).mean()
    j = 3 * k - 2 * d_
    # WR(14):0(顶)~100(底)
    hn14, ln14 = high.rolling(14).max(), low.rolling(14).min()
    wr = (hn14 - close) / (hn14 - ln14).replace(0, 1e-10) * 100
    # BIAS(6)
    bias = (close - close.rolling(6).mean()) / close.rolling(6).mean() * 100
    # BOLL(20,2)
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()

    vol_s = df["v"] / 1e4
    return {
        "dates": [b["d"] for b in bars],
        "kline": [[b["o"], b["c"], b["l"], b["h"]] for b in bars],
        "vol": [round(v, 1) for v in vol_s],
        "mavol5": r(vol_s.rolling(5).mean(), 1), "mavol10": r(vol_s.rolling(10).mean(), 1),
        "turn": [round((b.get("换手") or 0) * 100, 2) for b in bars],
        "ma5": r(ma[5], 2), "ma10": r(ma[10], 2), "ma20": r(ma[20], 2), "ma60": r(ma[60], 2),
        "boll_mid": r(mid, 2), "boll_up": r(mid + 2 * std, 2), "boll_dn": r(mid - 2 * std, 2),
        "dif": r(dif), "dea": r(dea), "macd": r(hist),
        "rsi6": r(rsi(6), 1), "rsi12": r(rsi(12), 1),
        "kdj_k": r(k, 1), "kdj_d": r(d_, 1), "kdj_j": r(j, 1),
        "wr": r(wr, 1), "bias": r(bias, 2),
    }


def _flow_summary(flow):
    if not flow:
        return None
    last = flow[-min(len(flow), 40):]
    keys = ["super", "big", "mid", "small"]
    out = {"dates": [x["d"] for x in last]}
    for kk in keys:
        out[kk] = [x.get(kk, 0) for x in last]
    for kk in keys:
        out["sum5_" + kk] = round(sum(x.get(kk, 0) for x in flow[-5:]), 1)
        out["sum20_" + kk] = round(sum(x.get(kk, 0) for x in flow[-20:]), 1)
    return out


def _flow_comment(fs):
    if not fs:
        return "资金流数据暂不可用(东财接口限流,稍后重跑可补全)。"
    def io(v):
        return ("净流入" if v >= 0 else "净流出") + f" {abs(v)} 万"
    s = (f"近5日:机构(超大单){io(fs['sum5_super'])}、游资(大单){io(fs['sum5_big'])}、"
         f"散户(小单){io(fs['sum5_small'])};近20日机构{io(fs['sum20_super'])}。")
    inst5, retail5 = fs["sum5_super"] + fs["sum5_big"], fs["sum5_small"] + fs["sum5_mid"]
    if inst5 > 0 and retail5 < 0:
        s += "近期机构游资在吸、散户在抛,筹码由散向集中(偏积极)。"
    elif inst5 < 0 and retail5 > 0:
        s += "近期机构游资在撤、散户在接,谨防高位派发。"
    else:
        s += "机构与散户方向未明显分化。"
    return s


def _explain(ind):
    """技术面速读,统一成 {icon,title,text},与缠论/筹码/资金同版式。"""
    tips = []
    tips.append({"icon": "📈", "title": "均线",
                 "text": {"多头排列": "MA5>MA10>MA20,均线多头排列,趋势向上,回踩均线是短线介入点",
                          "空头排列": "均线空头排列,趋势向下,反弹遇均线压力,不宜追",
                          "弱多": "均线偏多但未完全发散,趋势待确认", "弱空": "均线偏空,观望为宜"
                          }.get(ind.get("alignment", ""), ind.get("alignment", ""))})
    zero = "零轴上方(多头区)" if ind.get("macd_above_zero") else "零轴下方(空头区)"
    tips.append({"icon": "〰️", "title": "MACD", "text": f"{ind.get('macd_cross','')},位于{zero}"})
    rsi = ind.get("rsi6")
    if rsi is not None:
        tips.append({"icon": "🌡️", "title": "RSI", "text": f"RSI6={rsi}," + (
            "超买(>80),短线过热警惕回调" if rsi >= 80 else
            "超卖(<20),短线或有反弹" if rsi <= 20 else "中性区,无超买超卖极端")})
    tips.append({"icon": "📊", "title": "量能", "text": f"{ind.get('vol_pattern','')}(量比{ind.get('vol_ratio')})"})
    bias = ind.get("bias5")
    if bias is not None:
        t = (f"{bias}%,已超 +5%,短线追高风险高" if bias > 5 else
             f"{bias}%,深度负乖离(超跌),有均值回归动能" if bias < -5 else
             f"{bias}%,处于 ±5% 安全区,未追高")
        tips.append({"icon": "📏", "title": "乖离", "text": "MA5乖离 " + t})
    return tips


def _action_signal(s, ind):
    """规则化操作建议(短线+波段)。纯基于历史/技术数据的倾向性研判,非实时喊单。
    综合:评分/均线/缠论买卖点/筹码位置/资金流/乖离/RSI/中枢位置。"""
    chan_ = s.get("chan") or {}
    tbs, dv = chan_.get("third_bs"), chan_.get("divergence")
    chip = s.get("chip") or {}
    fs = s.get("flow") or {}
    inst5 = (fs.get("sum5_super", 0) + fs.get("sum5_big", 0)) if fs else 0
    score = s.get("score", 0)
    align = ind.get("alignment", "")
    rsi, bias = ind.get("rsi6"), ind.get("bias5")
    close, ma20 = s.get("entry"), ind.get("ma20")
    prof = chip.get("profit_ratio")
    bull = align in ("多头排列", "弱多") and (ma20 is None or close is None or close >= ma20)
    bear = align in ("空头排列", "弱空") or (ma20 and close and close < ma20)

    reasons, cons = [], []
    if (tbs and tbs["type"] == "3B") or (dv and dv["bs"] == "1B"):
        action, tone = "偏多 · 可建仓 / 回踩加仓", "bull"
        reasons.append("出现缠论买点(" + (tbs["type"] if tbs else dv["bs"]) + "),趋势/转折信号偏积极")
    elif bull and score >= 70 and (rsi is None or rsi < 75) and (bias is None or bias <= 5):
        action, tone = "偏多 · 轻仓建仓,回踩不破MA10续持", "bull"
        reasons.append(f"多头排列+评分{score},乖离/RSI未过热,可顺势参与")
    elif (tbs and tbs["type"] == "3S") or (dv and dv["bs"] == "1S") or bear:
        action, tone = "偏空 · 观望 / 反弹减仓,不追", "bear"
        reasons.append("空头结构或出现缠论卖点/顶背离,右侧未立不抄底")
    elif bias is not None and bias > 5:
        action, tone = "谨慎 · 短线过热,勿追高,回踩再看", "neutral"
        reasons.append(f"MA5乖离 {bias}% 偏高,追高风险大")
    else:
        action, tone = "中性 · 持有观察 / 轻仓,等信号明确", "neutral"
        reasons.append("多空信号交织,趋势待确认")
    zs = chan_.get("zhongshu") or []
    if zs and close and zs[-1]["zd"] <= close <= zs[-1]["zg"]:
        reasons.append(f"价在中枢 {zs[-1]['zd']}~{zs[-1]['zg']} 内,可高抛低吸做T(上沿减、下沿回补)")
    if inst5 > 0:
        reasons.append(f"近5日机构+游资净流入 {round(inst5)} 万,资金面支持")
    elif inst5 < 0:
        cons.append(f"近5日机构+游资净流出 {round(abs(inst5))} 万,资金面偏弱")
    if prof is not None and prof < 20:
        cons.append(f"获利比例仅 {prof}%,上方套牢盘重,反弹有压力")
    if s.get("stop"):
        cons.append(f"跌破止损 {s['stop']} 则信号失效,离场")
    swing = (f"上看压力/目标 {s.get('pressure') or s.get('target')}、下方支撑 {s.get('support')};"
             + ("站稳中枢上沿看趋势延续,跌破下沿转弱" if zs else "以均线与中枢方向为准"))
    return {"action": action, "tone": tone, "reasons": reasons, "cons": cons, "swing": swing}


SIGNAL_CLS = {"🔵": "b-blue", "🟡": "b-yellow", "⚪": "b-gray", "🔴": "b-red"}


def _build_stock(p):
    ind = p["indicators"]
    s = _series(p["bars"])
    s["chan"] = chan.analyze(p["bars"])
    s["candles"] = candle_patterns.detect(p["bars"], 30)
    s["candle_comment"] = candle_patterns.latest_comment(s["candles"])
    fs = _flow_summary(p.get("fund_flow"))
    last = p["bars"][-1]
    s.update({
        "code": p["code"], "name": p["name"], "signal": p["signal"], "score": p["score"],
        "sector": p.get("sector", ""), "sector_pct": p.get("sector_pct", 0),
        "stop": p["plan_stop"], "target": p["plan_target"], "entry": p["entry_close"],
        "support": ind.get("support"), "pressure": ind.get("pressure"),
        "chip": p.get("chips"), "flow": fs,
        "info": {"date": p["entry_date"], "o": last["o"], "c": last["c"], "h": last["h"],
                 "l": last["l"], "pct": ind.get("pct_chg", 0),
                 "turn": round(last.get("换手", 0) * 100, 2),
                 "amt": round(last["c"] * last["v"] / 1e8, 2), "volr": ind.get("vol_ratio")},
        "tips": _explain(ind),
        "chip_comment": p.get("chip_comment", ""),
        "flow_comment": _flow_comment(fs),
        "chan_comment": chan.summary(s["chan"]),
        "pool": p.get("pool", ""), "strategy": p.get("strategy", ""),
        "shadow": p.get("shadow", False), "rps": p.get("rps"), "zt60": p.get("zt60"),
        "stabil_signal": p.get("stabil_signal"),
        "above_sector": p.get("above_sector", False), "sector_avg": p.get("sector_avg"),
        "judge": p.get("judge"),  # 研判合成结果(立场/结构止损/盈亏比/矛盾/入场/失效)
        "emotion_comment": _emotion_comment(p.get("stock_emotion")),
        "fund_comment": _fund_comment(p.get("fundamental")),
    })
    # 持仓诊断维度:成本感知字段(存在才透传,荐股日报无这些 key → 零影响)
    for k in ("cost", "qty", "mktval", "pnl_pct", "dist_stop_pct",
              "holding_tag", "holding_verdict", "main5"):
        if p.get(k) is not None:
            s[k] = p[k]
    if p.get("news"):        # 逐股消息面(server 端已转好的 HTML 串,见 main() --port-news)
        s["news"] = p["news"]
    # 逐日买卖信号:优先复用 analyze 已算并落盘的(与研判同源一致),缺失则从图序列现算兜底
    s["signals"] = p.get("signals") or _signals.compute(s)
    # 台账 B/T/S 标注 + 持仓成本线(台账驱动,全图通用;无记录则无标注)
    if _ledger is not None:
        try:
            tr = _ledger.daily_trades(p["code"])
            if tr:
                s["trades"] = tr
        except Exception:  # noqa: BLE001
            pass
    cost = p.get("cost")     # 持仓诊断优先用 portfolio 成本
    if cost is None and _ledger is not None:
        try:
            op = _ledger.open_cost(p["code"])
            if op:
                cost = op.get("avg_cost")
        except Exception:  # noqa: BLE001
            pass
    if cost is not None:
        s["cost_line"] = cost
    return s


def _fund_comment(f: dict | None) -> str:
    """基本面速览 → 一句话。"""
    if not f:
        return ""
    extra = ";".join(t for t in (f.get("tags") or []) if "分位" not in t)
    return f.get("comment", "") + (f" —— {extra}" if extra else "")


def _emotion_comment(heat: dict | None) -> str:
    """个股情绪画像 → 一句话(影子指标)。"""
    if not heat:
        return ""
    tags = ";".join(heat.get("tags") or [])
    return f"{heat['grade']}" + (f" — {tags}" if tags else "") + "(影子指标,不参与评分)"


CSS = """
:root{--up:#f6465d;--down:#2ebd85;--bg:#0d1420;--panel:#141d2e;--panel2:#1b2740;
--ink:#d5dced;--muted:#7a869c;--line:#25324a;--accent:#4c8dff;--gold:#f0b429}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1900px;margin:0 auto;padding:20px 2.2vw 60px}
h1{font-size:23px;margin:.2em 0 .5em;padding-bottom:.3em;border-bottom:2px solid var(--accent)}
h2{font-size:18px;margin:1.2em 0 .5em;padding-left:10px;border-left:4px solid var(--accent)}
.note{margin:1em 0;padding:10px 14px;background:#20222d;border-left:3px solid var(--gold);border-radius:6px;color:#d9b84a;font-size:13px}
table{border-collapse:collapse;width:100%;margin:1em 0;background:var(--panel);border-radius:8px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:14px}
th{background:var(--panel2);color:#aebbd4}tr:last-child td{border-bottom:none}
.up{color:var(--up)}.down{color:var(--down)}
.ctrl{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0 12px}
select{background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font-size:15px;font-weight:600}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:8px;background:var(--panel2);color:var(--muted);cursor:pointer;font-size:14px;border:1px solid var(--line)}
.tab.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.subctrl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0 6px}
.subctrl .lbl{color:var(--muted);font-size:13px}
.subsel{padding:4px 8px;font-size:13px;font-weight:500}
.pfix{padding:4px 10px;font-size:13px;font-weight:600;color:#8fbaff;background:var(--panel2);border:1px solid var(--line);border-radius:6px}
.pbtn{width:26px;height:26px;border-radius:6px;background:var(--panel2);border:1px solid var(--line);color:var(--ink);cursor:pointer;font-size:15px;line-height:1}
.pbtn:hover{background:var(--accent);color:#fff}
.hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.hd .nm{font-size:20px;font-weight:700}.hd .cd{color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:11px;font-size:12.5px;font-weight:600}
.b-blue{background:#1b3a6b;color:#8fbaff}.b-yellow{background:#5a4a12;color:#f0cd6a}
.b-gray{background:#2a3550;color:#9fb0d0}.b-red{background:#5a1f2a;color:#ff9aa8}.b-score{background:#26324d;color:#c7d3ea}
.stats{display:flex;flex-wrap:wrap;background:var(--panel2);border-radius:8px;overflow:hidden;margin-bottom:10px}
.stat{flex:1 1 10%;min-width:84px;padding:7px 10px;border-right:1px solid var(--line)}
.stat .sl{display:block;color:var(--muted);font-size:11px}.stat .sv{display:block;font-size:15px;font-weight:600}
.row{display:flex;gap:14px;flex-wrap:wrap}
.kwrap{flex:4 1 720px;min-width:360px;display:flex;gap:2px}
.kside{flex:0 0 82px;padding-top:30px;font-size:11px}
.ksrow{display:flex;justify-content:space-between;padding:1px 3px;line-height:1.35}
.ksl{color:var(--muted)}.ksv{font-weight:600;color:#c7d3ea}.ksv.up{color:var(--up)}.ksv.down{color:var(--down)}.ksv.gold{color:var(--gold)}
.kshr{height:1px;background:var(--line);margin:4px 2px}
.kmain{flex:1 1 auto;position:relative;min-width:0}
.phdrs{position:absolute;top:0;left:0;right:0;height:100%;pointer-events:none;z-index:3}
.phdr{position:absolute;left:56px;font-size:11px;font-weight:600;white-space:nowrap;color:#aebbd4;pointer-events:auto;transform:translateY(4px)}
.fsbtn{margin-left:auto;padding:5px 12px;border-radius:8px;background:var(--panel2);color:#8fbaff;
  border:1px solid var(--line);cursor:pointer;font-size:13px;white-space:nowrap}
.fsbtn:hover{background:#1f3050}
#stockPanel.fs{background:var(--bg);padding:14px 18px;overflow:auto}
#stockPanel.fs .kchart{height:82vh}
.kchart{width:100%;height:70vh;min-height:600px}
.chipchart{flex:1 1 300px;height:70vh;min-height:600px;min-width:260px}
.flowchart{width:100%;height:230px;margin-top:10px}
.judge{margin:8px 0 2px;padding:9px 13px;background:var(--panel2);border-radius:7px;font-size:13.5px}
.judge b{color:var(--gold)}
.tips{margin:6px 0 0;padding-left:1.1em}.tips li{margin:.24em 0;font-size:13.5px;color:#b9c4da}
.gl{border-bottom:1px dashed var(--accent);cursor:help;position:relative}
.gl:hover .tip{display:block}
.tip{display:none;position:absolute;left:0;bottom:1.6em;z-index:50;width:320px;background:#0b1220;
border:1px solid var(--accent);border-radius:8px;padding:10px 12px;font-size:12.5px;line-height:1.6;
color:#cdd6e4;box-shadow:0 6px 20px rgba(0,0,0,.5);white-space:normal;font-weight:400}
.tip .t{color:var(--gold);font-weight:700;font-size:13px}
.tip .e{color:#8fbaff}.tip .u{color:#8ce0c0}
.action{margin:12px 0 4px;padding:12px 16px;border-radius:10px;border:1px solid var(--line)}
.act-bull{background:#2a1a1f;border-color:#f6465d}.act-bear{background:#13241d;border-color:#2ebd85}.act-neutral{background:#20222d;border-color:#f0b429}
.act-hd{font-size:12.5px;color:var(--muted);margin-bottom:3px}
.act-main{font-size:19px;font-weight:800;margin-bottom:6px}
.act-bull .act-main{color:#ff9aa8}.act-bear .act-main{color:#8ce0c0}.act-neutral .act-main{color:#f0cd6a}
.act-sec{font-size:13px;margin:3px 0;color:#c3cee0}.act-sec.con{color:#ffb0b0}
.act-dis{font-size:11.5px;color:var(--muted);margin-top:7px;line-height:1.5}
.jgrid{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}
.jcell{flex:1 1 200px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.jcell .jl{display:block;color:var(--muted);font-size:11.5px}
.jcell .jv{display:block;font-size:18px;font-weight:700;margin:1px 0}
.jcell .jx{display:block;color:var(--muted);font-size:11.5px}
.spots{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0}
.spot{flex:1 1 340px;background:linear-gradient(135deg,#17233a,#141d2e);border:1px solid var(--accent);border-radius:12px;padding:14px 18px}
.spot-nm{font-size:18px;font-weight:700;display:flex;align-items:center;gap:8px}
.spot-st{color:var(--gold);font-size:14px;font-weight:600;margin:5px 0}
.spot-kv{font-size:13px;color:#c3cee0;margin:4px 0}
.spot-rz{font-size:12.5px;color:var(--muted);margin-top:4px;line-height:1.5}
/* 作战方案表格增强 */
.ap-sub{margin:12px 0 2px;font-size:14px;font-weight:700;color:#f0cd6a}
.ap-note{font-size:11.5px;color:var(--muted);line-height:1.6;margin-bottom:5px}
.ap-note b{color:#c3cee0}
td.gold{color:var(--gold);font-weight:600}
.ap-code{color:var(--muted);font-size:.82em;font-weight:400;margin-left:5px;font-family:Consolas,Menlo,monospace}
/* 消息面:卡片化,与上下 block 视觉统一 */
.news{margin:1em 0;display:flex;flex-direction:column;gap:10px}
.news h3{display:none}
.nwcard{background:var(--panel);border-left:3px solid #4c8dff;border-radius:8px;padding:10px 15px;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.nwcard.bull{border-left-color:var(--up)}.nwcard.bear{border-left-color:var(--down)}.nwcard.macro{border-left-color:var(--gold)}
.nwcard.risk{border-left-color:#ff8a3d;background:#241a15}
.nw-hd{font-size:15px;font-weight:700;color:#e6edfb;margin-bottom:6px;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.nw-row{font-size:13px;margin:4px 0;line-height:1.65;color:#c3cee0}
.nw-lbl{display:inline-block;min-width:4.6em;color:#8fbaff;font-weight:600}
.nw-lbl.cat{color:#f0b429}.nw-lbl.nat{color:#a0e0ff}.nw-lbl.cross{color:#8ce0c0}
.nw-p{font-size:13px;margin:4px 0;line-height:1.65;color:#c3cee0}
.stkchip{display:inline-block;background:#1f3050;color:#8fbaff;border:1px solid #345;border-radius:4px;
  padding:0 6px;margin:0 3px 2px 0;font-size:12.5px;font-weight:600;white-space:nowrap}
.stkchip.star{background:#3a2f14;color:#f0cd6a;border-color:#6b5620}
.news b{color:#e6edfb}
/* 持仓组合体检卡片(持仓诊断维度) */
.mod-h{margin:22px 0 2px;font-size:19px;font-weight:800;color:#e8eefc;border-left:4px solid var(--gold);padding-left:10px}
.mod-sub{color:var(--muted);font-size:13px;margin:4px 0 6px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}
.mc{flex:1 1 150px;min-width:130px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.mc .k{font-size:12px;color:var(--muted)}.mc .v{font-size:20px;font-weight:800;margin-top:2px}
.foot{margin-top:2em;color:var(--muted);font-size:12.5px;text-align:center}
/* K线形态:可折叠·按交易日降序·点击定位 */
.clist{margin:10px 0;padding:10px 12px;background:var(--panel2);border-radius:8px}
.clist-hd{font-size:13px;color:var(--muted);margin-bottom:6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ctoggle{cursor:pointer;color:#8fbaff;font-size:12.5px;border:1px solid #345;border-radius:5px;padding:1px 8px}
.ctoggle:hover{background:#1f3050}
.crow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 8px;border-radius:6px;cursor:pointer;font-size:13px}
.crow:hover{background:rgba(255,255,255,.04)}
.crow.on{background:rgba(240,205,106,.12);outline:1px solid #6b5620}
.crow .cdate{min-width:5.8em;color:#c3cee0;font-weight:600}
.cbadge{display:inline-block;padding:0 7px;border-radius:4px;font-size:12px;font-weight:600;white-space:nowrap}
.cbadge.bull{background:#2a1a1f;color:#ff9aa8;border:1px solid #5a2a35}
.cbadge.bear{background:#13241d;color:#8ce0c0;border:1px solid #22523f}
.cbadge.neutral{background:#20222d;color:#f0cd6a;border:1px solid #4a3f1a}
.nochart{color:#ff8a8a;font-size:13px;padding:12px}
@media(max-width:720px){.kwrap,.chipchart{flex:1 1 100%}.stat{flex:1 1 22%}}
"""

JS = r"""
var S=window.STOCKS||[], GL=window.GLOSSARY||{};
var state={i:0,tab:'k',panels:['量','MACD'],candleOpen:false,candleDay:null};  // 量强制置顶不可删,其下默认MACD
var ALL_IND=['MACD','KDJ','RSI','量'];
var kc,cc,fc;
function ud(v){return v>=0?'#f6465d':'#2ebd85';}
function tip(ps){
 // 分区(主图 / 各副指标)+ 分隔线 + 源色一致 + 趋势箭头;K线形态tab附当日形态;台账买卖点展开
 if(!ps||!ps.length)return '';
 var s=S[state.i];if(!s)return '';
 var i=ps[0].dataIndex;if(i==null||i<0||i>=s.dates.length)return '';
 var k=s.kline[i],prev=i>0?s.kline[i-1][1]:k[0];
 function col(v){return v>=prev?'#ff9aa8':'#8ce0c0';}
 var chg=prev?((k[1]-prev)/prev*100):0,amp=prev?((k[3]-k[2])/prev*100):0;
 var hr="<div style='border-top:1px solid #33415c;margin:5px 0'></div>";
 function kv(l,v,c){return "<span style='display:inline-block;min-width:82px'>"+l+" <span style='color:"+c+"'>"+v+"</span></span>";}
 // 主图数据跨行显示(每行两列),避免拥挤
 var out="<b style='font-size:13px'>"+s.dates[i]+"</b>";
 out+="<div style='margin-top:3px;line-height:1.75'>"+
   kv('开',k[0],col(k[0]))+kv('收',k[1],col(k[1]))+"<br/>"+
   kv('高',k[3],col(k[3]))+kv('低',k[2],col(k[2]))+"<br/>"+
   kv('涨幅',(chg>=0?'+':'')+chg.toFixed(2)+'%',chg>=0?'#ff9aa8':'#8ce0c0')+
   kv('振幅',amp.toFixed(2)+'%','#c3cee0')+"</div>";
 state.panels.forEach(function(ind){out+=hr+"<div>"+panelHeader(ind,s,i,true)+"</div>";});
 // 缠论 tab:补回当日缠论结构(分型/买卖点/背驰/最新中枢/笔线段端点)
 if(state.tab==='chan'&&s.chan)out+=chanTip(s.chan,s.dates[i],hr);
 // K线形态 tab:附当日命中的形态
 if(state.tab==='candle'&&s.candles){var cs=s.candles.filter(function(h){return h.d===s.dates[i];});
   if(cs.length){var tg={bull:'看涨',bear:'看跌',neutral:'中性'};
     out+=hr+"<div style='color:#f0cd6a'>🕯️ "+cs.map(function(h){return h.name_cn+"("+tg[h.bias]+")";}).join('、')+"</div>";}}
 // 台账买卖点
 ps.forEach(function(p){if(p.data&&p.data._t)out+=hr+tradeTip(p.data._t);});
 return out;
}
function chanTip(c,d,hr){
 var bsmap={'3B':'三类买点','3S':'三类卖点','1B':'一类买点','1S':'一类卖点'};
 var parts=[];
 (c.fractals||[]).forEach(function(f){if(f.d===d)parts.push(f.type==='top'?'顶分型':'底分型');});
 if(c.third_bs&&c.third_bs.d===d)parts.push(bsmap[c.third_bs.type]||c.third_bs.type);
 if(c.divergence&&c.divergence.d===d)parts.push((bsmap[c.divergence.bs]||c.divergence.bs)+'('+c.divergence.type+')');
 var line1=parts.length?"<div style='color:#ffd43b'>📐 缠论:"+parts.join(' · ')+"</div>":'';
 var info=[];
 var seg=(c.segments||[]);if(seg.length)info.push('线段 '+(seg[seg.length-1].dir==='up'?'向上':'向下'));
 var bi=(c.bi||[]);if(bi.length)info.push('笔 '+(bi[bi.length-1].type==='top'?'向上':'向下'));
 var zs=(c.zhongshu||[]);if(zs.length){var z=zs[zs.length-1];info.push('最新中枢 '+z.zd+'~'+z.zg);}
 var line2=info.length?"<div style='color:#aebbd4'>"+info.join(' · ')+"</div>":'';
 return (line1||line2)?hr+line1+line2:'';
}
function tradeTip(t){
 // 台账买卖点悬浮:隐藏账户名,显示 方向·日期·均价·数量(T 分两行)
 var L='';
 if(t.buy)L+="<br/><span style='color:#ff9aa8'>🔴 买入</span> "+t.d+" 均价 "+t.buy.avg+" 数量 "+t.buy.qty;
 if(t.sell)L+="<br/><span style='color:#8fbaff'>🔵 卖出</span> "+t.d+" 均价 "+t.sell.avg+" 数量 "+t.sell.qty;
 return L;
}
function sigMarkPoint(list){return {symbolSize:14,label:{show:false},data:list.map(function(g){var buy=g.dir==='buy';
  return {coord:[g.d,g.y],symbol:'arrow',symbolRotate:buy?0:180,symbolOffset:[0,buy?11:-11],
    itemStyle:{color:buy?'#f6465d':'#2ebd85'},value:g.kind||''};})};}
function _arr(cur,prev){if(cur==null||prev==null)return '';
 return cur>prev?"<span style='color:#f6465d'>↑</span>":(cur<prev?"<span style='color:#2ebd85'>↓</span>":'');}
// 副指标字段口径解释(独立于共享术语库,避免 glossify 误伤单字母 K/D/J);样式复用 .gl/.tip
var FIELDGL={
 'MACD(12,26,9)':{term:'MACD(12,26,9)',plain:'快慢EMA之差(DIF)与其9日EMA(DEA),及两者差×2的柱,衡量趋势动能与金叉死叉。'},
 'DIF':{term:'DIF 快线',plain:'12日EMA−26日EMA。DIF上穿DEA=金叉(偏多),下穿=死叉(偏空)。'},
 'DEA':{term:'DEA 慢线',plain:'DIF 的9日EMA(信号线)。与DIF的交叉给出金叉/死叉。'},
 '柱':{term:'MACD 柱',plain:'(DIF−DEA)×2。红柱放大=多头动能增强,绿柱放大=空头动能增强。'},
 'KDJ(9,3,3)':{term:'KDJ(9,3,3)',plain:'随机指标:K快、D慢、J最敏感。>80超买、<20超卖;K上穿D金叉。'},
 'K':{term:'KDJ K 值',plain:'快线。上穿D线=金叉(偏多)。'},
 'D':{term:'KDJ D 值',plain:'慢线。K下穿D=死叉(偏空)。'},
 'J':{term:'KDJ J 值',plain:'最敏感线。>100极度超买,<0极度超卖。'},
 'RSI(6,12)':{term:'RSI(6,12)',plain:'相对强弱:RSI6快、RSI12慢。>80超买、<20超卖。'},
 'RSI6':{term:'RSI6(快)',plain:'6日相对强弱。>80超买回落风险,<20超卖或反弹。'},
 'RSI12':{term:'RSI12(慢)',plain:'12日相对强弱,趋势确认更稳。'},
 '成交量':{term:'成交量',plain:'当日成交量(万手)。放量配合价格方向更可信。'},
 'MAVOL5':{term:'MAVOL5',plain:'5日成交量均线。量在其上方=相对放量。'},
 'MAVOL10':{term:'MAVOL10',plain:'10日成交量均线,量能中枢。'}};
function fgl(label,key){var g=FIELDGL[key||label];if(!g)return label;
 return "<span class='gl'>"+label+"<span class='tip'><span class='t'>"+g.term+"</span><br/>"+g.plain+"</span></span>";}
function panelHeader(ind,s,i,plain){
 function g(a){return (a&&a[i]!=null)?a[i]:'-';}
 function gp(a){return (a&&i>0&&a[i-1]!=null)?a[i-1]:null;}
 function T(label,key){return plain?label:fgl(label,key);}   // plain=true 用于 tooltip(不带术语悬浮)
 if(ind==='MACD')return "<b>"+T('MACD(12,26,9)')+"</b> <span style='color:#f0b429'>"+T('DIF')+" "+g(s.dif)+_arr(s.dif[i],gp(s.dif))+
   "</span> <span style='color:#4c8dff'>"+T('DEA')+" "+g(s.dea)+_arr(s.dea[i],gp(s.dea))+
   "</span> <span style='color:"+(((s.macd||[])[i]||0)>=0?'#f6465d':'#2ebd85')+"'>"+T('柱')+" "+g(s.macd)+"</span>";
 if(ind==='KDJ')return "<b>"+T('KDJ(9,3,3)')+"</b> <span style='color:#f0b429'>"+T('K')+" "+g(s.kdj_k)+_arr(s.kdj_k[i],gp(s.kdj_k))+
   "</span> <span style='color:#4c8dff'>"+T('D')+" "+g(s.kdj_d)+_arr(s.kdj_d[i],gp(s.kdj_d))+
   "</span> <span style='color:#c56cf0'>"+T('J')+" "+g(s.kdj_j)+_arr(s.kdj_j[i],gp(s.kdj_j))+"</span>";
 if(ind==='RSI')return "<b>"+T('RSI(6,12)')+"</b> <span style='color:#c56cf0'>"+T('RSI6')+" "+g(s.rsi6)+_arr(s.rsi6[i],gp(s.rsi6))+
   "</span> <span style='color:#4c8dff'>"+T('RSI12')+" "+g(s.rsi12)+_arr(s.rsi12[i],gp(s.rsi12))+"</span>";
 if(ind==='量')return "<b>"+T('成交量')+"</b> 总手 "+g(s.vol)+" <span style='color:#f0b429'>"+T('MAVOL5')+" "+g(s.mavol5)+
   "</span> <span style='color:#c56cf0'>"+T('MAVOL10')+" "+g(s.mavol10)+"</span>";
 return '';
}
function buildPanelHeaders(){var host=document.getElementById('phdrs');if(!host)return;
 var L=layout(state.panels.length);
 host.innerHTML=state.panels.map(function(ind,i){
   return "<div class='phdr' data-p='"+i+"' style='top:"+L[i+1].t+"'></div>";}).join('');}
function curIdx(ev){if(!ev||!ev.axesInfo||!ev.axesInfo.length)return -1;
 var v=ev.axesInfo[0].value,s=S[state.i];if(!s)return -1;
 if(typeof v==='number')return Math.round(v);
 return s.dates.indexOf(v);}
function updateHover(idx){var s=S[state.i];if(!s)return;
 var n=s.dates.length;if(idx==null||idx<0||idx>=n)idx=n-1;
 var el=document.getElementById('kside');
 if(el){var k=s.kline[idx],prev=idx>0?s.kline[idx-1][1]:k[0];
  var o=k[0],c=k[1],lo=k[2],hi=k[3];
  var chg=prev?((c-prev)/prev*100):0,amp=prev?((hi-lo)/prev*100):0,amt=(c*(s.vol[idx]||0)/1e4);
  function cc(v){return v>=prev?'up':'down';}
  function rw(l,v,cls,lc){return "<div class='ksrow'><span class='ksl'"+(lc?" style='color:"+lc+"'":"")+">"+l+
    "</span><span class='ksv "+(cls||'')+"'>"+v+"</span></div>";}
  el.innerHTML=rw('时间',s.dates[idx].slice(5),'')+rw('开盘',o,cc(o))+rw('最高',hi,cc(hi))+rw('最低',lo,cc(lo))+rw('收盘',c,cc(c))+
    rw('涨幅',(chg>=0?'+':'')+chg.toFixed(2)+'%',chg>=0?'up':'down')+rw('振幅',amp.toFixed(2)+'%','gold')+
    rw('成交量',s.vol[idx],c>=o?'up':'down')+rw('成交额',amt.toFixed(1)+'亿','')+
    (s.turn?rw('换手',(s.turn[idx]!=null?s.turn[idx]:'-')+'%',''):'')+"<div class='kshr'></div>"+
    rw('MA5',(s.ma5[idx]!=null?s.ma5[idx]:'-'),'','#f0b429')+rw('MA10',(s.ma10[idx]!=null?s.ma10[idx]:'-'),'','#4c8dff')+
    rw('MA20',(s.ma20[idx]!=null?s.ma20[idx]:'-'),'','#c56cf0')+rw('MA60',(s.ma60[idx]!=null?s.ma60[idx]:'-'),'','#7a869c');
 }
 [].forEach.call(document.querySelectorAll('#phdrs .phdr'),function(e){
   e.innerHTML=panelHeader(state.panels[+e.dataset.p],s,idx);});}
// ---- 副指标区构建器:返回 {yAxis, series[]} 绑定到 grid=gi ----
function ind_MACD(s,gi){return {yAxis:subY(gi,'MACD'),series:[
  {name:'MACD',type:'bar',xAxisIndex:gi,yAxisIndex:gi,data:s.macd,itemStyle:{color:function(p){return ud(p.data);}}},
  {name:'DIF',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.dif,showSymbol:false,lineStyle:{width:1,color:'#f0b429'}},
  {name:'DEA',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.dea,showSymbol:false,lineStyle:{width:1,color:'#4c8dff'}}]};}
function ind_RSI(s,gi){return {yAxis:subY(gi,'RSI',0,100),series:[
  {name:'RSI6',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.rsi6,showSymbol:false,lineStyle:{width:1,color:'#c56cf0'},
   markLine:{symbol:'none',data:[{yAxis:80,lineStyle:{color:'#5a2a2a',type:'dashed'}},{yAxis:20,lineStyle:{color:'#264a3a',type:'dashed'}}]}},
  {name:'RSI12',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.rsi12,showSymbol:false,lineStyle:{width:1,color:'#4c8dff'}}]};}
function ind_KDJ(s,gi){return {yAxis:subY(gi,'KDJ'),series:[
  {name:'K',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.kdj_k,showSymbol:false,lineStyle:{width:1,color:'#f0b429'}},
  {name:'D',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.kdj_d,showSymbol:false,lineStyle:{width:1,color:'#4c8dff'}},
  {name:'J',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.kdj_j,showSymbol:false,lineStyle:{width:1,color:'#c56cf0'}}]};}
function ind_VOL(s,gi){var vc=s.kline.map(function(k){return k[1]>=k[0]?'#f6465d':'#2ebd85';});
 return {yAxis:subY(gi,'量'),series:[
   {name:'量',type:'bar',xAxisIndex:gi,yAxisIndex:gi,data:s.vol.map(function(v,i){return {value:v,itemStyle:{color:vc[i]}};})},
   {name:'MAVOL5',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.mavol5,showSymbol:false,lineStyle:{width:1,color:'#f0b429'}},
   {name:'MAVOL10',type:'line',xAxisIndex:gi,yAxisIndex:gi,data:s.mavol10,showSymbol:false,lineStyle:{width:1,color:'#c56cf0'}}]};}
var INDI={'MACD':ind_MACD,'KDJ':ind_KDJ,'RSI':ind_RSI,'量':ind_VOL};
function subY(gi,name,mn,mx){var y={scale:true,gridIndex:gi,axisLabel:{color:'#7a869c',fontSize:9},
  splitLine:{show:false},axisLine:{lineStyle:{color:'#25324a'}}};   // 不再显示白色指标名(改由左上角 header)
  if(mn!=null){y.min=mn;y.max=mx;} return y;}
function layout(n){ // 返回 [{top,height}] 主图+ n 个副图(百分比);最多 4 个副图
 if(n<=1)return [{t:'4%',h:'62%'},{t:'72%',h:'20%'}];
 if(n===2)return [{t:'4%',h:'50%'},{t:'59%',h:'16%'},{t:'79%',h:'15%'}];
 if(n===3)return [{t:'4%',h:'40%'},{t:'48%',h:'13%'},{t:'64%',h:'13%'},{t:'80%',h:'13%'}];
 return [{t:'3%',h:'33%'},{t:'40%',h:'12.5%'},{t:'55%',h:'12.5%'},{t:'70%',h:'12.5%'},{t:'85%',h:'12.5%'}];
}
function catX(dates,gi,showLabel){return {type:'category',data:dates,gridIndex:gi,boundaryGap:true,
  axisLabel:{show:showLabel,color:'#7a869c',fontSize:10},axisLine:{lineStyle:{color:'#25324a'}},
  axisPointer:{label:{show:showLabel}}};}
function baseK(s,panels){
 var n=panels.length, L=layout(n), zoom=[];
 var grids=L.map(function(g){return {left:52,right:58,top:g.t,height:g.h};});
 var xAxis=[],yAxis=[],series=[];
 for(var gi=0;gi<=n;gi++){xAxis.push(catX(s.dates,gi,gi===n));zoom.push(gi);}
 yAxis.push({scale:true,gridIndex:0,axisLabel:{color:'#7a869c'},splitLine:{lineStyle:{color:'#1b2740'}}});
 var kml=[
     {name:'目标',yAxis:s.target,lineStyle:{color:'#2ebd85',type:'dashed'},label:{formatter:'目标 '+s.target,color:'#2ebd85'}},
     {name:'止损',yAxis:s.stop,lineStyle:{color:'#f0b429',type:'dashed'},label:{formatter:'止损 '+s.stop,color:'#f0b429'}},
     {name:'现价',yAxis:s.entry,lineStyle:{color:'#4c8dff',type:'dotted'},label:{formatter:'现价 '+s.entry,color:'#4c8dff'}}];
 series.push({name:'K线',type:'candlestick',xAxisIndex:0,yAxisIndex:0,data:s.kline,
   itemStyle:{color:'#f6465d',color0:'#2ebd85',borderColor:'#f6465d',borderColor0:'#2ebd85'},
   markLine:{symbol:'none',label:{position:'insideEndTop',fontSize:10},data:kml}});
 // 持仓成本线:独立 line series(常显、不可编辑、跨所有 tab 都在;不被各 tab 的 markLine 覆盖)
 if(s.cost_line!=null)series.push({name:'成本',type:'line',xAxisIndex:0,yAxisIndex:0,z:4,
   data:s.dates.map(function(){return s.cost_line;}),showSymbol:false,
   lineStyle:{width:1.4,color:'#ff922b',type:'solid'},
   endLabel:{show:true,formatter:'成本 '+s.cost_line,color:'#ff922b',fontSize:10}});
 [['MA5','#f0b429','ma5'],['MA10','#4c8dff','ma10'],['MA20','#c56cf0','ma20'],['MA60','#7a869c','ma60'],
  ['BOLL上','#5a6b8c','boll_up'],['BOLL中','#8a97b5','boll_mid'],['BOLL下','#5a6b8c','boll_dn']].forEach(function(m){
   series.push({name:m[0],type:'line',xAxisIndex:0,yAxisIndex:0,data:s[m[2]],smooth:true,showSymbol:false,
     lineStyle:{width:1,color:m[1],type:m[0].indexOf('BOLL')===0?'dashed':'solid',opacity:m[0].indexOf('BOLL')===0?0.7:1}});});
 var legend=['K线','MA5','MA10','MA20','MA60','BOLL中'];
 // 台账买卖点:小圆点(尽量不与K线重叠)+ 点外更小字体 B/S/T(B在下方、S/T在上方),悬浮在 tip() 展开
 if(s.trades&&s.trades.length){
   var kmap={};s.dates.forEach(function(d,i){kmap[d]=s.kline[i];});
   series.push({name:'台账',type:'scatter',xAxisIndex:0,yAxisIndex:0,z:6,symbolSize:7,
     data:s.trades.map(function(t){var k=kmap[t.d]||[0,0,0,0];
       var col=t.type==='B'?'#f6465d':(t.type==='S'?'#4c8dff':'#ff922b');
       var below=t.type==='B';                 // 买点在K线下方,卖/T点在上方
       var y=below?k[2]*0.978:k[3]*1.022;
       return {value:[t.d,y],_t:t,itemStyle:{color:col},
         label:{show:true,formatter:t.type,color:col,fontWeight:'bold',fontSize:9,
           position:below?'bottom':'top',distance:3}};})});
   legend.push('台账');
 }
 var SIGKEY={'MACD':'macd','KDJ':'kdj','RSI':'rsi'};
 var sigCut=s.dates[Math.max(0,s.dates.length-60)];   // 买卖箭头只画最近约60个交易日,降噪
 panels.forEach(function(ind,i){var gi=i+1,b=INDI[ind](s,gi);yAxis.push(b.yAxis);
   var sk=SIGKEY[ind];
   if(sk&&s.signals&&s.signals[sk]){var rec=s.signals[sk].filter(function(g){return g.d>=sigCut;});
     if(rec.length)b.series[0].markPoint=sigMarkPoint(rec);}
   series=series.concat(b.series);});
 return {backgroundColor:'transparent',animation:false,
  legend:{data:legend,top:0,textStyle:{color:'#aebbd4',fontSize:11}},
  tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'#1b2740',borderColor:'#25324a',textStyle:{color:'#d5dced'},formatter:tip},
  axisPointer:{link:[{xAxisIndex:'all'}]},grid:grids,xAxis:xAxis,yAxis:yAxis,
  dataZoom:[{type:'inside',xAxisIndex:zoom,start:35,end:100},{type:'slider',xAxisIndex:zoom,bottom:0,height:14,start:35,end:100,textStyle:{color:'#7a869c'}}],
  series:series};
}
function addChan(opt,s){
 var c=s.chan||{}; var biMap={}; (c.bi||[]).forEach(function(p){biMap[p.d]=p.price;});
 opt.legend.data=opt.legend.data.concat(['笔','线段']);
 opt.series.push({name:'笔',type:'line',xAxisIndex:0,yAxisIndex:0,data:s.dates.map(function(d){return biMap[d]!=null?biMap[d]:null;}),
   connectNulls:true,showSymbol:true,symbolSize:4,lineStyle:{width:1,color:'#ffd43b',opacity:0.55},itemStyle:{color:'#ffd43b'}});
 var segPts={};(c.segments||[]).forEach(function(g){segPts[g.d_start]=(g.dir==='up'?g.low:g.high);segPts[g.d_end]=(g.dir==='up'?g.high:g.low);});
 opt.series.push({name:'线段',type:'line',xAxisIndex:0,yAxisIndex:0,data:s.dates.map(function(d){return segPts[d]!=null?segPts[d]:null;}),
   connectNulls:true,showSymbol:true,symbolSize:7,lineStyle:{width:2.5,color:'#ff922b'},itemStyle:{color:'#ff922b'}});
 var mp=(c.fractals||[]).map(function(f){return {coord:[f.d,f.price],symbol:'triangle',symbolRotate:f.type==='top'?0:180,
   symbolOffset:[0,f.type==='top'?-9:9],itemStyle:{color:f.type==='top'?'#ff8a8a':'#8ce0c0'}};});
 var bs=c.third_bs,dv=c.divergence;
 function pin(d,pr,txt,up){return {coord:[d,pr],symbol:'pin',symbolSize:34,symbolRotate:up?0:180,
   itemStyle:{color:up?'#f6465d':'#2ebd85'},label:{show:true,formatter:txt,color:'#fff',fontSize:10,position:up?'top':'bottom'}};}
 if(bs)mp.push(pin(bs.d,bs.price,bs.type,bs.type==='3B'));
 if(dv)mp.push(pin(dv.d,dv.price,dv.bs,dv.bs==='1B'));
 opt.series[0].markPoint={symbolSize:11,data:mp};
 // 中枢 markArea
 opt.series[0].markArea={silent:true,itemStyle:{color:'rgba(76,141,255,0.10)',borderColor:'#4c8dff',borderWidth:1},
   label:{show:true,color:'#8fbaff',fontSize:10,position:'insideTop',formatter:'中枢'},
   data:(c.zhongshu||[]).map(function(z){return [{xAxis:z.d_start,yAxis:z.zd},{xAxis:z.d_end,yAxis:z.zg}];})};
}
function addPattern(opt,s){
 var c=s.chan||{},ml=[{name:'现价',yAxis:s.entry,lineStyle:{color:'#4c8dff',type:'dotted'},label:{formatter:'现价 '+s.entry,color:'#4c8dff',position:'insideEndTop',fontSize:10}}];
 if(s.support)ml.push({yAxis:s.support,lineStyle:{color:'#8ce0c0',type:'dashed'},label:{formatter:'支撑 '+s.support,color:'#8ce0c0',position:'insideEndTop',fontSize:10}});
 if(s.pressure)ml.push({yAxis:s.pressure,lineStyle:{color:'#ff8a8a',type:'dashed'},label:{formatter:'压力 '+s.pressure,color:'#ff8a8a',position:'insideEndTop',fontSize:10}});
 (c.patterns||[]).forEach(function(p){ml.push({yAxis:p.neckline,lineStyle:{color:'#f0b429',width:1.5},label:{formatter:p.type+' 颈线 '+p.neckline,color:'#f0b429',position:'insideEndTop',fontSize:10}});
   var pm={};(p.points||[]).forEach(function(pt){pm[pt.d]=pt.price;});
   opt.series.push({name:p.type,type:'line',xAxisIndex:0,yAxisIndex:0,data:s.dates.map(function(d){return pm[d]!=null?pm[d]:null;}),
     connectNulls:true,showSymbol:true,symbolSize:8,lineStyle:{width:1.5,type:'dashed',color:p.kind==='bull'?'#2ebd85':'#f6465d'},itemStyle:{color:p.kind==='bull'?'#2ebd85':'#f6465d'}});});
 opt.series[0].markLine={symbol:'none',label:{position:'insideEndTop',fontSize:10},data:ml};
}
function addCandle(opt,s){var col={bull:'#f6465d',bear:'#2ebd85',neutral:'#f0b429'};
 opt.series[0].markPoint={symbolSize:8,data:(s.candles||[]).map(function(h){
   return {coord:[h.d,h.bias==='bear'?h.price*1.01:h.price*0.99],symbol:'circle',itemStyle:{color:col[h.bias]||'#f0b429'},
     label:{show:true,formatter:h.name_cn,color:col[h.bias]||'#f0b429',fontSize:9,position:h.bias==='bear'?'top':'bottom'}};})};
 // 选中交易日:在K图上加一条竖向定位虚线(保留 baseK 已有的目标/止损横线)
 if(state.candleDay){var mlk=opt.series[0].markLine=opt.series[0].markLine||{symbol:'none',label:{position:'insideEndTop',fontSize:10},data:[]};
   mlk.data.push({xAxis:state.candleDay,lineStyle:{color:'#f0cd6a',type:'dashed',width:1.5},
     label:{show:true,formatter:state.candleDay,color:'#f0cd6a',fontSize:10,position:'insideEndTop'}});}}
function chipOpt(s){var ch=s.chip;if(!ch)return null;
 function nidx(a,v){var bi=0,bd=1e18;for(var i=0;i<a.length;i++){var d=Math.abs(a[i]-v);if(d<bd){bd=d;bi=i;}}return bi;}
 var cols=ch.price_levels.map(function(p){return p<=ch.current?'#f6465d':'#2ebd85';});
 return {backgroundColor:'transparent',animation:false,
  title:{text:'筹码分布(估算)',left:'center',top:2,textStyle:{color:'#aebbd4',fontSize:12}},
  tooltip:{trigger:'axis',axisPointer:{type:'shadow'},backgroundColor:'#1b2740',borderColor:'#25324a',textStyle:{color:'#d5dced'},
    formatter:function(a){var i=a[0].dataIndex;return '价位 '+ch.price_levels[i]+'<br/>占比 '+ch.amounts[i]+'%';}},
  grid:{left:44,right:38,top:30,bottom:20},
  xAxis:{type:'value',axisLabel:{color:'#7a869c',fontSize:9,formatter:'{value}%'},splitLine:{lineStyle:{color:'#1b2740'}}},
  yAxis:{type:'category',data:ch.price_levels,axisLabel:{color:'#7a869c',fontSize:9,interval:4},axisLine:{lineStyle:{color:'#25324a'}}},
  series:[{type:'bar',data:ch.amounts.map(function(v,i){return {value:v,itemStyle:{color:cols[i]}};}),
    markLine:{symbol:'none',data:(function(){var m=[
      {yAxis:nidx(ch.price_levels,ch.current),lineStyle:{color:'#4c8dff'},label:{formatter:'现价 '+ch.current,color:'#4c8dff',fontSize:9,position:'insideEndTop'}},
      {yAxis:nidx(ch.price_levels,ch.avg_cost),lineStyle:{color:'#f0b429',type:'dashed'},label:{formatter:'均本 '+ch.avg_cost,color:'#f0b429',fontSize:9,position:'insideEndBottom'}}];
      // 持仓成本线并入筹码分布,直观看成本落在筹码峰的哪一侧(套牢盘/获利盘)
      if(s.cost_line!=null)m.push({yAxis:nidx(ch.price_levels,s.cost_line),lineStyle:{color:'#ff922b',width:1.4},label:{formatter:'持仓成本 '+s.cost_line,color:'#ff922b',fontSize:9,position:'insideEndTop'}});
      return m;})()}}]};}
function flowOpt(s){var f=s.flow;if(!f)return null;
 var lines=[['超大单(机构)','super','#f6465d'],['大单(游资)','big','#ff922b'],['中单(中户)','mid','#4c8dff'],['小单(散户)','small','#8a97b5']];
 return {backgroundColor:'transparent',animation:false,
  title:{text:'资金流向:机构/游资/中户/散户 净额(万元)',left:'center',top:0,textStyle:{color:'#aebbd4',fontSize:12}},
  legend:{data:lines.map(function(x){return x[0];}),top:0,right:8,textStyle:{color:'#aebbd4',fontSize:11}},
  tooltip:{trigger:'axis',backgroundColor:'#1b2740',borderColor:'#25324a',textStyle:{color:'#d5dced'},formatter:tip},
  grid:{left:60,right:14,top:26,bottom:22},
  xAxis:{type:'category',data:f.dates,axisLabel:{color:'#7a869c',fontSize:9},axisLine:{lineStyle:{color:'#25324a'}}},
  yAxis:{type:'value',axisLabel:{color:'#7a869c',fontSize:9},splitLine:{lineStyle:{color:'#1b2740'}}},
  series:lines.map(function(x,i){return {name:x[0],type:i===0?'bar':'line',data:f[x[1]],showSymbol:false,
    itemStyle:{color:i===0?function(p){return ud(p.data);}:x[2]},lineStyle:{color:x[2],width:1}};})};}
function stat(l,v,cls){return "<div class='stat'><span class='sl'>"+l+"</span><span class='sv "+(cls||'')+"'>"+v+"</span></div>";}
// ---- 术语悬浮:把研判文字里的关键字包裹成可 hover 的 span ----
function glossify(html){
 // 占位符两段式:先把每个关键字首次出现替换成不含字母的占位符,最后统一换成 span,
 // 避免"解释文本里又含关键字"导致的嵌套错乱。
 var keys=Object.keys(GL).sort(function(a,b){return b.length-a.length;});
 var store=[];
 keys.forEach(function(k){
  var i=html.indexOf(k);if(i<0)return;
  var g=GL[k];
  store.push("<span class='gl'>"+k+"<span class='tip'><span class='t'>"+g.term+"</span><br/>"+g.plain+
   (g.example?"<br/><span class='e'>例:"+g.example+"</span>":"")+(g.usage?"<br/><span class='u'>用:"+g.usage+"</span>":"")+"</span></span>");
  html=html.substring(0,i)+"@@GL"+(store.length-1)+"@@"+html.substring(i+k.length);
 });
 store.forEach(function(sp,n){html=html.replace("@@GL"+n+"@@",sp);});
 return html;
}
function badge(sig){var m={'🔵':'b-blue','🟡':'b-yellow','⚪':'b-gray','🔴':'b-red'};for(var e in m){if(sig.indexOf(e)>=0)return "<span class='badge "+m[e]+"'>"+sig+"</span>";}return "<span class='badge b-gray'>"+sig+"</span>";}
function pinVol(){ // 成交量强制置顶且唯一:确保 panels[0]==='量'
 var p=state.panels,j=p.indexOf('量');
 if(j<0)p.unshift('量');else if(j>0){p.splice(j,1);p.unshift('量');}
}
function renderSubCtrl(){
 pinVol();
 var used=state.panels, html="<span class='lbl'>副指标区("+used.length+"/4):</span>";
 used.forEach(function(ind,i){
  if(i===0){ // 成交量:强制展示、不可编辑、不可删除
   html+="<span class='pfix'>成交量(固定)</span>";return;
  }
  html+="<select class='subsel' data-i='"+i+"'>"+ALL_IND.filter(function(o){return o!=='量';}).map(function(o){
    var dis=(used.indexOf(o)>=0&&o!==ind)?" disabled":"";
    return "<option value='"+o+"'"+(o===ind?" selected":"")+dis+">"+o+"</option>";}).join("")+"</select>";
  html+="<button class='pbtn' data-rm='"+i+"'>×</button>";
 });
 if(used.length<4)html+="<button class='pbtn' data-add='1'>+</button>";
 var el=document.getElementById('subctrl');el.innerHTML=html;
 [].forEach.call(el.querySelectorAll('.subsel'),function(sel){sel.onchange=function(){state.panels[+this.dataset.i]=this.value;render();};});
 [].forEach.call(el.querySelectorAll('[data-rm]'),function(b){b.onclick=function(){state.panels.splice(+this.dataset.rm,1);render();};});
 var add=el.querySelector('[data-add]');if(add)add.onclick=function(){var avail=ALL_IND.filter(function(o){return o!=='量'&&state.panels.indexOf(o)<0;});if(avail.length){state.panels.push(avail[0]);render();}};
}
// 单个术语 → 带悬浮解释的 span(术语库命中才包裹,否则原样返回;供 K线形态 chip 复用 hover)
function glTerm(name){
 var g=GL[name];if(!g)return name;
 return "<span class='gl'>"+name+"<span class='tip'><span class='t'>"+g.term+"</span><br/>"+g.plain+
   (g.example?"<br/><span class='e'>例:"+g.example+"</span>":"")+(g.usage?"<br/><span class='u'>用:"+g.usage+"</span>":"")+"</span></span>";
}
// ---- K线形态:按交易日降序分组,可折叠(默认只显最近一日),点击某日在K图上定位 ----
function renderCandleList(s){
 var el=document.getElementById('candleList');if(!el)return;
 var cs=s.candles||[];
 if(!cs.length){el.innerHTML='';state.candleDay=null;return;}
 var tag={bull:'看涨',bear:'看跌',neutral:'中性'};
 var map={},order=[];
 cs.forEach(function(h){if(!map[h.d]){map[h.d]=[];order.push(h.d);}map[h.d].push(h);});
 order.sort(function(a,b){return a<b?1:-1;});      // 日期降序
 var days=order.slice(0,10);                        // 最多10个交易日
 if(!state.candleDay||days.indexOf(state.candleDay)<0)state.candleDay=days[0];
 var shown=state.candleOpen?days:days.slice(0,1);
 var rows=shown.map(function(d){
  var chips=map[d].map(function(h){return "<span class='cbadge "+(h.bias||'neutral')+"'>"+glTerm(h.name_cn)+" · "+tag[h.bias]+" · 可靠度"+h.reliability+"</span>";}).join(' ');
  return "<div class='crow"+(d===state.candleDay?' on':'')+"' onclick=\"pickCandleDay('"+d+"')\"><span class='cdate'>"+d+"</span>"+chips+"</div>";
 }).join('');
 var more=days.length>1?"<span class='ctoggle' onclick='toggleCandle()'>"+(state.candleOpen?'收起 ▴':('展开全部 '+days.length+' 个交易日 ▾'))+"</span>":'';
 el.innerHTML="<div class='clist'><div class='clist-hd'><b style='color:#e8eefc'>🕯️ K线形态</b>"+
   "<span>近 "+days.length+" 个交易日 · 降序 · 点击某日在K图定位</span>"+more+"</div>"+rows+"</div>";
}
function toggleCandle(){state.candleOpen=!state.candleOpen;renderCandleList(S[state.i]);}
function pickCandleDay(d){
 state.candleDay=d;state.tab='candle';
 [].forEach.call(document.querySelectorAll('.tab'),function(x){x.classList.toggle('on',x.dataset.t==='candle');});
 render();
}
function render(){
 var s=S[state.i];if(!s)return;
 var pc=s.info.pct>=0?'up':'down',ps=s.info.pct>=0?'+':'';
 var extra="";
 if(s.strategy)extra+="<span class='badge b-gray'>"+s.strategy+"</span>";
 if(s.shadow)extra+="<span class='badge b-yellow'>影子·观察</span>";
 if(s.stabil_signal)extra+="<span class='badge b-blue'>企稳:"+s.stabil_signal+"</span>";
 if(s.above_sector)extra+="<span class='badge b-blue'>高于板块结构</span>";
 if(s.rps!=null)extra+="<span class='badge b-gray'>RPS "+s.rps+"</span>";
 if(s.zt60!=null&&s.zt60>0)extra+="<span class='badge b-red'>60日涨停×"+s.zt60+"</span>";
 if(s.holding_tag)extra+="<span class='badge b-yellow'>["+s.holding_tag+"]</span>";
 document.getElementById('hd').innerHTML="<span class='nm'>"+s.name+"</span><span class='cd'>"+s.code+"</span>"+
   badge(s.signal)+"<span class='badge b-score'>评分 "+s.score+"/100</span>"+extra+(s.sector?"<span class='cd'>"+s.sector+"</span>":"");
 document.getElementById('stats').innerHTML=stat('日期',s.info.date)+stat('开盘',s.info.o)+stat('收盘',s.info.c,pc)+
   stat('最高',s.info.h)+stat('最低',s.info.l)+stat('涨跌幅',ps+s.info.pct+'%',pc)+stat('换手率',s.info.turn+'%')+stat('成交额',s.info.amt+'亿')+stat('量比',s.info.volr);
 renderSubCtrl();
 var opt=baseK(s,state.panels);
 if(state.tab==='chan')addChan(opt,s);else if(state.tab==='pattern')addPattern(opt,s);else if(state.tab==='candle')addCandle(opt,s);
 kc.setOption(opt,true);
 buildPanelHeaders();updateHover(null);   // 副图左上角 header + 左栏数值(默认显最新一根)
 var co=chipOpt(s);if(co){cc.setOption(co,true);document.getElementById('chip').style.display='';}else{document.getElementById('chip').style.display='none';}
 var fo=flowOpt(s);if(fo){fc.setOption(fo,true);document.getElementById('flow').style.display='';}else{document.getElementById('flow').style.display='none';}
 var items=[];
 if(s.cost!=null){var lc=(s.pnl_pct!=null&&s.pnl_pct<0)?'down':'up';
   items.push({i:'💼',t:'持仓',x:'成本 <b>'+s.cost+'</b> · 现价 <b>'+s.info.c+'</b> · 盈亏 <b class="'+lc+'">'+(s.pnl_pct!=null?s.pnl_pct+'%':'—')+'</b> · 市值 '+(s.mktval!=null?s.mktval:'—')+' · <b>['+(s.holding_tag||'')+']</b> '+(s.holding_verdict||'')});}
 if(state.tab==='chan'||state.tab==='pattern')items.push({i:'📐',t:'缠论/形态',x:s.chan_comment});
 if(s.emotion_comment)items.push({i:'🔥',t:'个股情绪',x:s.emotion_comment});
 if(s.fund_comment)items.push({i:'📊',t:'基本面速览',x:s.fund_comment});
 if(s.news)items.push({i:'📰',t:'消息面',x:s.news});
 items.push({i:'🧭',t:'筹码控盘',x:s.chip_comment});
 items.push({i:'💰',t:'资金流向',x:s.flow_comment});
 (s.tips||[]).forEach(function(tp){items.push({i:tp.icon,t:tp.title,x:tp.text});});
 var jhtml=items.map(function(it){return "<div class='judge'>"+it.i+" <b>"+it.t+"</b>:"+it.x+"</div>";}).join('');
 // 研判决策卡(judge):立场 + 结构化止损 + 盈亏比 + 矛盾点 + 灰色预警 + 入场/失效
 var j=s.judge||{}, ss=j.structural_stop||{}, rr=j.risk_reward||{};
 var tone=/多头|健康|值博/.test((j.stance||'')+(rr.verdict||''))?'act-bull':(/偏弱|转弱|不值/.test((j.stance||'')+(rr.verdict||''))?'act-bear':'act-neutral');
 var rrcls=rr.rr>=1.5?'up':(rr.rr>=1?'':'down');
 var ah="<div class='action "+tone+"'>"+
   "<div class='act-hd'>🎯 研判(短线+波段 · 基于历史/技术数据,非实时喊单)</div>"+
   "<div class='act-main'>"+(j.stance||'—')+"</div>"+
   "<div class='jgrid'>"+
     "<div class='jcell'><span class='jl'>结构止损</span><span class='jv'>"+(ss.stop!=null?ss.stop:'—')+"</span><span class='jx'>"+(ss.basis||'')+(ss.too_far?" · <span class='down'>止损偏远"+ss.dist_pct+"%风险大</span>":" · 距现价"+(ss.dist_pct!=null?ss.dist_pct:'-')+"%")+"</span></div>"+
     "<div class='jcell'><span class='jl'>盈亏比</span><span class='jv "+rrcls+"'>"+(rr.rr!=null?rr.rr+" : 1":'—')+"</span><span class='jx'>"+(rr.verdict||'')+(rr.reward_pct!=null?"(赚"+rr.reward_pct+"% / 亏"+rr.risk_pct+"%)":"")+"</span></div>"+
   "</div>"+
   ((j.signal_notes&&j.signal_notes.length)?"<div class='act-sec'>📡 信号:"+j.signal_notes.join(';')+"</div>":"")+
   ((j.tensions&&j.tensions.length)?"<div class='act-sec con'>⚠️ 矛盾点:"+j.tensions.join(';')+"</div>":"<div class='act-sec'>✓ 未见明显指标矛盾</div>")+
   ((j.grey&&j.grey.length)?"<div class='act-sec con'>🟡 灰色预警:"+j.grey.join(';')+"</div>":"")+
   "<div class='act-sec'>🎯 介入:"+(j.entry||'—')+"</div>"+
   "<div class='act-sec con'>🛑 失效:"+(j.invalidation||'—')+"</div>"+
   "<div class='act-dis'>止损为结构化(现价下方最近有效支撑),非机械百分比;盈亏比<1 表示不值博。仅供研究,请自行复核。</div></div>";
 document.getElementById('judge').innerHTML=glossify(ah+jhtml);
 renderCandleList(s);
}
document.addEventListener('DOMContentLoaded',function(){
 if(!S.length)return;
 kc=echarts.init(document.getElementById('k'));cc=echarts.init(document.getElementById('chip'));fc=echarts.init(document.getElementById('flow'));
 var sel=document.getElementById('sel');
 S.forEach(function(s,i){var o=document.createElement('option');o.value=i;o.text=(s.pool?'【'+s.pool+'】':'')+s.name+' '+s.code;sel.appendChild(o);});
 sel.onchange=function(){state.i=+this.value;render();};
 [].forEach.call(document.querySelectorAll('.tab'),function(t){t.onclick=function(){state.tab=this.dataset.t;
   [].forEach.call(document.querySelectorAll('.tab'),function(x){x.classList.remove('on');});this.classList.add('on');render();};});
 // 放大/全屏:整块个股分析面板进入/退出全屏,再点还原
 var fsBtn=document.getElementById('fsBtn'),panel=document.getElementById('stockPanel');
 function afterResize(){kc.resize();cc.resize();fc.resize();buildPanelHeaders();updateHover(null);}
 if(fsBtn&&panel){fsBtn.onclick=function(){
   if(document.fullscreenElement)document.exitFullscreen();
   else if(panel.requestFullscreen)panel.requestFullscreen();};
  document.addEventListener('fullscreenchange',function(){
    var on=!!document.fullscreenElement;
    panel.classList.toggle('fs',on);fsBtn.textContent=on?'⤡ 还原':'⤢ 全屏';
    setTimeout(afterResize,60);});}
 // 游标联动:悬浮某交易日 → 刷新左栏 + 副图 header;移出恢复最新
 kc.on('updateAxisPointer',function(ev){var i=curIdx(ev);if(i>=0)updateHover(i);});
 kc.getZr().on('globalout',function(){updateHover(null);});
 window.addEventListener('resize',afterResize);
 render();
 // 静态区(消息面 + 作战方案/大盘/情绪卡)也做术语高亮;#judge 是动态区已在 render 内 glossify,跳过
 ['.news','.action'].forEach(function(sel){[].forEach.call(document.querySelectorAll(sel),function(el){
   if(el.closest('#judge'))return; el.innerHTML=glossify(el.innerHTML);});});
});
"""


def _news_html(md: str) -> str:
    """消息面 Markdown → 卡片化 HTML:每个板块/主题一张卡(左侧色条),受益个股高亮成 chip,
    催化剂/性质/交叉结论 带标签,涨跌红绿,加粗高亮,风险提示单独橙色卡 —— 与上下 block 视觉统一。"""
    import re
    import html as _h

    def fmt(t: str) -> str:
        t = _h.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        # markdown 链接 [text](url) → 去链接保留文字(报告内不外跳)
        t = re.sub(r"\[([^\]]+)\]\((?:https?|ftp)[^)]+\)", r"\1", t)
        t = re.sub(r"([+＋]\d+(?:\.\d+)?%)", r"<span class='up'>\1</span>", t)
        t = re.sub(r"(?<![\w>])(-\d+(?:\.\d+)?%)", r"<span class='down'>\1</span>", t)
        # 个股 名称(6位代码) → chip;紧跟 ★ 的标为荐股池(金色)
        t = re.sub(r"([一-龥A-Za-z0-9·\-]{2,10})\((\d{5,6})\)(★?)",
                   lambda m: f"<span class='stkchip{' star' if m.group(3) else ''}'>"
                             f"{m.group(1)}({m.group(2)}){m.group(3)}</span>", t)
        return t

    cards, cur = [], None
    for ln in md.splitlines():
        s = ln.rstrip()
        if not s.strip():
            continue
        if s.startswith("### "):
            title = s[4:]
            typ = ("risk" if "风险" in title else "macro"
                   if re.search(r"大盘|政策|宏观", title) else "topic")
            icon = {"risk": "⚠️", "macro": "🏛️", "topic": "🔥"}[typ]
            cur = {"title": title, "typ": typ, "icon": icon, "rows": []}
            cards.append(cur)
        else:
            if cur is None:
                cur = {"title": "消息面", "typ": "topic", "icon": "🔥", "rows": []}
                cards.append(cur)
            if s.lstrip().startswith("- "):
                cur["rows"].append(("li", s.lstrip()[2:]))
            elif s.startswith(">"):
                cur["rows"].append(("q", s.lstrip("> ").rstrip()))
            else:
                cur["rows"].append(("p", s))

    out = []
    for c in cards:
        rows = []
        for kind, content in c["rows"]:
            if kind == "q":
                rows.append(f"<div class='nw-p' style='color:#7a869c;font-size:12px'>{fmt(content)}</div>")
                continue
            if kind == "p":
                rows.append(f"<div class='nw-p'>{fmt(content)}</div>")
                continue
            m = re.match(r"\*\*(.+?)\*\*[::](.*)", content)
            if m:
                label, rest = m.group(1), m.group(2)
                lc = ("cat" if "催化" in label else "nat" if "性质" in label
                      else "cross" if ("结论" in label or "交叉" in label) else "")
                rows.append(f"<div class='nw-row'><span class='nw-lbl {lc}'>{_h.escape(label)}</span>{fmt(rest)}</div>")
            else:
                rows.append(f"<div class='nw-row'>• {fmt(content)}</div>")
        out.append(f"<div class='nwcard {c['typ']}'><div class='nw-hd'>{c['icon']} {fmt(c['title'])}</div>"
                   f"{''.join(rows)}</div>")
    return "<div class='news'>" + "".join(out) + "</div>"


def _action_plan_html(ap: dict) -> str:
    """作战方案卡片:组合姿态 + 持仓动作 + 荐股动作 + 换股。"""
    if not ap:
        return ""
    po = ap.get("posture") or {}
    acls = {"进攻": "act-bull", "谨慎": "act-neutral", "防守": "act-bear"}.get(po.get("regime_level"), "act-neutral")
    h = [f"<div class='action {acls}'><div class='act-hd'>🎯 今日作战方案(研究性组合研判 · 非喊单 · 均绑事先算好的计划价位)</div>",
         f"<div class='act-main'>{po.get('note','')}</div>"]
    extra = []
    if po.get("position_pct") is not None:
        extra.append(f"当前仓位 {po['position_pct']}%")
    if po.get("concentration_warn"):
        extra.append("⚠️ " + po["concentration_warn"])
    if extra:
        h.append(f"<div class='act-sec con'>{' · '.join(extra)}</div>")

    def _name_code(r):
        n = r.get("name") or ""
        c = r.get("code")
        return f"{n}<span class='ap-code'>{c}</span>" if c else n

    def _tbl(title, rows, headers, keys, cls_map=None, note=None):
        if not rows:
            return ""
        th = "".join(f"<th>{x}</th>" for x in headers)
        trs = []
        for r in rows:
            tds = []
            for k in keys:
                v = r.get(k)
                cls = (cls_map or {}).get(k, "")
                if callable(cls):
                    cls = cls(r)
                tds.append(f"<td class='{cls}'>{v if v is not None and v != '' else '—'}</td>")
            trs.append(f"<tr>{''.join(tds)}</tr>")
        cap = f"<div class='ap-note'>{note}</div>" if note else ""
        return (f"<div class='ap-sub'>{title}</div>{cap}"
                f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>")

    # 持仓端:成本感知,列更实用(市值/距触发价%/止损/建议比例)
    hrows = []
    for x in ap.get("holdings", []):
        trig, dist = x.get("trigger"), x.get("dist_to_trigger")
        hrows.append({**x, "name_code": _name_code(x), "cp": f"{x.get('cost')}/{x.get('price')}",
                      "mkt": (f"{round(x['mktval'])}" if x.get("mktval") else "—"),
                      "trig_disp": (f"{trig} ({dist:+}%)" if trig and dist is not None else (trig or "—")),
                      "ratio_disp": (x.get("ratio") if x.get("ratio") not in (None, "—") else "—")})
    h.append(_tbl(
        "持仓端(结合成本的加/减/守)", hrows,
        ["股票", "成本/现价", "盈亏%", "市值", "动作", "触发价(距现价)", "止损", "建议比例", "依据"],
        ["name_code", "cp", "pnl_pct", "mkt", "action", "trig_disp", "stop", "ratio_disp", "note"],
        cls_map={"pnl_pct": lambda r: "up" if (r.get("pnl_pct") or 0) >= 0 else "down",
                 "action": "gold"},
        note=("<b>触发价</b>=到该价位就执行动作(减/加/清),括号是现价到触发价的距离;"
              "<b>止损</b>=结构化止损线,跌破离场;<b>建议比例</b>=该动作占该股持仓的仓位比例"
              "(如 1/3=减/加三分之一,≤1/3=最多加三分之一,100%=清空,—=持有不动)。")))
    h.append(_tbl(
        "荐股端(新标的建/低吸/突破)", [{**p, "name_code": _name_code(p)} for p in ap.get("pool", [])],
        ["股票", "池", "动作", "计划价", "止损", "目标", "盈亏比", "依据"],
        ["name_code", "pool", "action", "plan_price", "stop", "target", "rr", "note"],
        cls_map={"action": "gold", "rr": lambda r: "up" if (r.get("rr") or 0) >= 1.5 else ("down" if (r.get("rr") or 0) < 1 else "")},
        note="<b>计划价</b>=挂单参考价(回踩介入/突破确认);<b>盈亏比</b>=(目标−现价)/(现价−止损),≥1.5 值博、<1 不值博。"))
    if ap.get("swaps"):
        h.append("<div class='act-sec'>🔁 换股:" + ";".join(s["note"] for s in ap["swaps"]) + "</div>")
    h.append("<div class='act-dis'>动作由代码据 judge 计划价位合成;仅供研究,请自行复核,不构成投资建议、不代下单。</div></div>")
    return "".join(h)


def _regime_card(regime) -> str:
    """大盘环境总开关卡片(regime gate)。无数据返回空串。盘中机会池复用同一版式。"""
    if not (regime and regime.get("level")):
        return ""
    rc = {"进攻": "act-bull", "谨慎": "act-neutral", "防守": "act-bear"}.get(regime["level"], "act-neutral")
    return (f"<div class='action {rc}'><div class='act-hd'>🧭 大盘环境总开关(regime gate)</div>"
            f"<div class='act-main'>{regime['level']}</div>"
            f"<div class='act-sec'>上证 {regime.get('close')} · MA20 {regime.get('ma20')} · MA60 {regime.get('ma60')}"
            f" —— {regime.get('note','')}</div></div>")


def _emotion_card(emo, hd: str = "🌡️ 短线情绪温度计(影子指标,暂不参与选股)") -> str:
    """短线情绪温度计卡片。hd 可定制(盘前=影子指标;盘中=超短线核心闸门)。无数据返回空串。"""
    if not (emo and emo.get("phase")):
        return ""
    ec = {"高潮": "act-bull", "发酵": "act-bull", "分歧": "act-neutral",
          "退潮": "act-bear", "冰点": "act-bear"}.get(emo["phase"], "act-neutral")
    ladder = " ".join(f"{k}板×{v}" for k, v in (emo.get("ladder") or {}).items()) or "无"
    prom = f"{emo['promotion_rate']}%" if emo.get("promotion_rate") is not None else "—"
    prem = f"{emo['yzt_premium']:+}%" if emo.get("yzt_premium") is not None else "—"
    turning = f" · {emo['turning']}" if emo.get("turning") else ""
    return (f"<div class='action {ec}'><div class='act-hd'>{hd}</div>"
            f"<div class='act-main'>{emo['temperature']}/100 · {emo['phase']}</div>"
            f"<div class='act-sec'>涨停{emo.get('zt_count')}家 · 炸板率{emo.get('break_rate')}% · "
            f"最高{emo.get('max_height')}连板 · 梯队 {ladder} · 晋级率{prom} · 昨停溢价{prem}{turning}"
            f" —— {emo.get('note','')}</div></div>")


def _attach_port_news(md: str, picks: list) -> int:
    """逐股消息面 Markdown(约定每股 `## 名称(代码)` 一节)→ 按 6 位代码回填到对应
    pick['news'](紧凑 HTML 串,供个股详析区的"📰 消息面"块展示)。返回命中股数。"""
    import re
    import html as _h

    def fmt(t: str) -> str:
        t = _h.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"\[([^\]]+)\]\((?:https?|ftp)[^)]+\)", r"\1", t)  # 去链接留文字
        t = re.sub(r"([+＋]\d+(?:\.\d+)?%)", r"<span class='up'>\1</span>", t)
        t = re.sub(r"(?<![\w>])(-\d+(?:\.\d+)?%)", r"<span class='down'>\1</span>", t)
        return t

    sections, cur = {}, None
    for ln in md.splitlines():
        head = re.match(r"\s*#{1,4}\s", ln)
        code_m = re.search(r"(\d{6})", ln) if head else None
        if head and code_m:            # 标题行含 6 位代码 → 后续正文归属此股
            cur = code_m.group(1)
            sections.setdefault(cur, [])
            continue
        if cur is not None:
            sections[cur].append(ln)
    by_code = {p.get("code"): p for p in picks}
    hit = 0
    for code, lines in sections.items():
        p = by_code.get(code)
        if not p:
            continue
        rows = [fmt(s) for s in (ln.strip().lstrip("-*·").strip() for ln in lines) if s]
        if rows:
            p["news"] = "<br>".join(rows)
            hit += 1
    return hit


def _portfolio_card(summ) -> str:
    """持仓组合体检卡片(持仓/市值/浮盈亏/亏损/仓位/主力净流出)+ 最大单票集中度。无数据返空。"""
    import html as _h
    if not summ:
        return ""
    pnl = summ.get("total_pnl")
    pnl_pct = summ.get("total_pnl_pct")
    pnl_cls = "down" if (pnl is not None and pnl < 0) else "up"
    pos = summ.get("position_pct")
    cards = [("持仓", f"{summ.get('holdings','—')} 只"),
             ("市值", f"{summ.get('total_mktval','—')}"),
             ("浮盈亏", f"<span class='{pnl_cls}'>{pnl if pnl is not None else '—'}"
                       f"({pnl_pct if pnl_pct is not None else '—'}%)</span>"),
             ("亏损", f"{summ.get('losers','—')} 只"),
             ("仓位", f"{pos}%" if pos is not None else "—"),
             ("主力净流出", f"{summ.get('outflow_count','—')}/{summ.get('holdings','—')} 只")]
    body = ["<div class='mod-h'>组合体检</div>",
            "<div class='cards'>" + "".join(
                f"<div class='mc'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
                for k, v in cards) + "</div>"]
    if summ.get("heaviest"):
        body.append(f"<div class='mod-sub'>最大单票 <b>{_h.escape(str(summ['heaviest']))}</b> "
                    f"占 {summ.get('top_concentration_pct')}%"
                    + ("(集中度偏高,注意单票风险)" if (summ.get('top_concentration_pct') or 0) >= 40 else "")
                    + "</div>")
    return "".join(body)


def render(data, global_data=None, news_md=None, action_plan=None):
    date = data.get("date", "")
    src = data.get("source", "")
    is_port = src == "持仓诊断"        # 持仓诊断维度:组合体检 + 成本感知,与荐股日报分开
    is_stock_only = src in ("点名分析", "全局扫描") and not global_data
    if is_port:
        parts = [f"<h1>💼 持仓诊断报告 · {date}</h1>",
                 "<div class='note'>⚠️ 按你的<b>实际成本</b>做健康度诊断的研究工具,<b>不构成投资建议</b>;"
                 "只读持仓、不涉账户密码、不做实盘下单。缠论/形态/筹码为启发式估算,辅助研判。"
                 "研判中带下划虚线的术语可鼠标悬浮看解释。</div>"]
        pc = _portfolio_card(data.get("portfolio"))
        if pc:
            parts.append(pc)
    else:
        parts = [f"<h1>A股分析报告 · {date}</h1>",
                 "<div class='note'>⚠️ 个人研究工具自动生成,不构成投资建议。缠论/形态/筹码为启发式估算,辅助研判。"
                 f"数据源:{src or '—'}。研判中带下划虚线的术语可鼠标悬浮看解释。</div>"]
    regime = data.get("regime") or (global_data.get("regime") if global_data else None)
    rc = _regime_card(regime)
    if rc:
        parts.append(rc)
    emo = data.get("emotion") or (global_data.get("emotion") if global_data else None)
    ec = _emotion_card(emo)
    if ec:
        parts.append(ec)
    if action_plan:
        parts.append(_action_plan_html(action_plan))
    idx = 1
    if not is_stock_only and data.get("indexes"):
        parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、大盘</h2><table><thead><tr><th>指数</th><th>收盘</th><th>涨跌幅</th><th>日期</th></tr></thead><tbody>")
        for ix in data["indexes"]:
            cls = "up" if ix["pct"] >= 0 else "down"; sign = "+" if ix["pct"] >= 0 else ""
            parts.append(f"<tr><td>{ix['name']}</td><td>{ix['close']}</td><td class='{cls}'>{sign}{ix['pct']}%</td><td>{ix['date']}</td></tr>")
        parts.append("</tbody></table>"); idx += 1
    if data.get("sectors"):
        has_str = any("strength" in sx for sx in data["sectors"])
        if has_str:
            has_size = any(sx.get("size") for sx in data["sectors"])
            size_hdr = "<th>成分数</th><th>总市值(亿)</th>" if has_size else ""
            parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、板块强弱榜</h2>"
                         "<div class='note' style='color:#8fbaff;border-color:#4c8dff'>强弱分 = 动量(涨跌幅)+广度(上涨家数占比)+中位涨幅;广度高=普涨真强,只领涨股涨=假强。"
                         "排序分 = 强弱分 + 容量分(log10成分数×权重),大容量板块梯队深、资金承接强,同等强度优先(小容量板块已按门槛滤除)。</div>"
                         "<table><thead><tr><th>板块</th><th>强弱</th><th>强弱分</th><th>排序分</th>"
                         f"{size_hdr}<th>涨跌幅</th><th>上涨广度</th><th>中位涨幅</th></tr></thead><tbody>")
            gcls = {"强": "up", "中": "", "弱": "down"}
            for sx in data["sectors"]:
                cls = "up" if sx["pct"] >= 0 else "down"; sign = "+" if sx["pct"] >= 0 else ""
                adv = (f"{int(sx['adv_ratio']*100)}%({sx.get('n_up')}/{sx.get('n_total')})"
                       if sx.get("adv_ratio") is not None else "—")
                med = f"{sx['median_chg']:+.2f}%" if sx.get("median_chg") is not None else "—"
                mc = "up" if (sx.get("median_chg") or 0) >= 0 else "down"
                rk = sx.get("rank_score")
                rk_cell = (f"{rk}<span style='color:#7a869c'>(+{sx.get('size_score',0)})</span>"
                           if rk is not None and sx.get("size_score") else (rk if rk is not None else "—"))
                size_cell = (f"<td>{sx.get('size') or '—'}</td><td>{sx.get('mcap_yi') or '—'}</td>"
                             if has_size else "")
                parts.append(f"<tr><td>{sx['name']}</td><td class='{gcls.get(sx.get('grade'),'')}'>{sx.get('grade','')}</td>"
                             f"<td>{sx.get('strength')}</td><td>{rk_cell}</td>"
                             f"{size_cell}<td class='{cls}'>{sign}{sx['pct']:.2f}%</td>"
                             f"<td>{adv}</td><td class='{mc}'>{med}</td></tr>")
            parts.append("</tbody></table>")
        else:
            parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、强势板块</h2><table><thead><tr><th>板块</th><th>涨跌幅</th><th>领涨股</th></tr></thead><tbody>")
            for sx in data["sectors"]:
                cls = "up" if sx["pct"] >= 0 else "down"; sign = "+" if sx["pct"] >= 0 else ""
                parts.append(f"<tr><td>{sx['name']}</td><td class='{cls}'>{sign}{sx['pct']}%</td><td>{sx.get('leader','—')}</td></tr>")
            parts.append("</tbody></table>")
        idx += 1
    concepts = data.get("concepts") or (global_data.get("concepts") if global_data else None)
    if concepts:
        parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、🔥概念热榜(题材维度)</h2>"
                     "<div class='note' style='color:#f0a35a;border-color:#c8792e'>概念是题材聚合,与行业分类互补;"
                     "已滤除融资融券/沪股通类名单式伪概念及成分>上限的指数化巨型概念,按涨幅取紧凑热题材。</div>"
                     "<table><thead><tr><th>概念</th><th>涨跌幅</th><th>成分数</th></tr></thead><tbody>")
        for c in concepts:
            cls = "up" if c["pct"] >= 0 else "down"; sign = "+" if c["pct"] >= 0 else ""
            parts.append(f"<tr><td>{c['name']}</td><td class='{cls}'>{sign}{c['pct']:.2f}%</td>"
                         f"<td>{c.get('size','—')}</td></tr>")
        parts.append("</tbody></table>"); idx += 1
    if news_md:
        parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、消息面(联网热点)</h2>{_news_html(news_md)}"); idx += 1

    # 合并热点池 + 全局池,打标签(pool)供下拉区分
    picks = []
    for p in data.get("picks", []):
        if not p.get("pool"):        # 空串也要兜底(build_sidecar 写了 pool="");概念池已带"概念"
            p["pool"] = "热点池"
        picks.append(p)
    if global_data:
        for p in global_data.get("picks", []):
            p["pool"] = "全局池"
            picks.append(p)
    stocks = [_build_stock(p) for p in picks]
    n_hot = sum(1 for s in stocks if s.get("pool") == "热点池")
    n_glb = len(stocks) - n_hot
    cnt = f"热点池 {n_hot} 只 + 全局池 {n_glb} 只" if global_data else f"共 {len(stocks)} 只"
    if is_port:
        # 重点处置:按诊断标签严重度置顶最该动手的持仓(与荐股"今日之选"逻辑相反 —— 先看风险)
        sev = {"止损": 0, "重亏警戒": 1, "逢反弹减": 2, "持有观察": 3, "观察": 4, "持有": 5}
        ranked = sorted(stocks, key=lambda s: sev.get(s.get("holding_tag"), 9))
        focus = [s for s in ranked if sev.get(s.get("holding_tag"), 9) <= 2][:2]
        if focus:
            cards = []
            for s in focus:
                lc = "down" if (s.get("pnl_pct") is not None and s["pnl_pct"] < 0) else "up"
                ss = (s.get("judge") or {}).get("structural_stop") or {}
                cards.append(
                    f"<div class='spot'><div class='spot-nm'>{s['name']} <span class='cd'>{s['code']}</span>"
                    f"<span class='badge b-yellow'>[{s.get('holding_tag','')}]</span></div>"
                    f"<div class='spot-st'>{s.get('holding_verdict','—')}</div>"
                    f"<div class='spot-kv'>成本 <b>{s.get('cost','—')}</b> · 现价 <b>{s['info']['c']}</b>"
                    f" · 盈亏 <b class='{lc}'>{s.get('pnl_pct','—')}%</b> · 结构止损 <b>{ss.get('stop','—')}</b></div>"
                    f"<div class='spot-rz'>市值 {s.get('mktval','—')} · 主力近5日 {s.get('main5','—')}万</div></div>")
            parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、重点处置(按诊断标签严重度置顶)</h2>"
                         "<div class='note'>先看最该动手的持仓:止损 / 重亏警戒 / 逢反弹减 优先。"
                         "诊断为成本感知的规则化研判,非喊单,请自行复核决策。</div>"
                         "<div class='spots'>" + "".join(cards) + "</div>")
            idx += 1
    else:
        # 今日之选:非影子池中按 judge 机会质量分排序取前2只置顶聚焦(避免多只平铺无重点)
        ranked = sorted([s for s in stocks if not s.get("shadow")],
                        key=lambda s: (s.get("judge") or {}).get("quality", -999), reverse=True)
        if ranked:
            cards = []
            for s in ranked[:2]:
                j = s.get("judge") or {}; rr = j.get("risk_reward") or {}; ss = j.get("structural_stop") or {}
                tcls = "up" if (rr.get("rr") or 0) >= 1.5 else ("down" if (rr.get("rr") or 0) < 1 else "")
                reason = (j.get("tensions") or ["结构健康,无明显矛盾"])[0]
                cards.append(
                    f"<div class='spot'><div class='spot-nm'>{s['name']} <span class='cd'>{s['code']}</span>"
                    f"<span class='badge b-gray'>{s.get('pool','')}</span></div>"
                    f"<div class='spot-st'>{j.get('stance','—')}</div>"
                    f"<div class='spot-kv'>现价 <b>{s['entry']}</b> · 止损 <b>{ss.get('stop','—')}</b>({ss.get('basis','')})"
                    f" · 盈亏比 <b class='{tcls}'>{rr.get('rr','—')}:1</b> · {rr.get('verdict','')}</div>"
                    f"<div class='spot-rz'>关键:{reason};介入 {j.get('entry','—')}</div></div>")
            parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、今日之选(按机会质量·风险收益比排序)</h2>"
                         "<div class='note'>聚焦最值得看的标的:综合盈亏比、板块相对强度、指标矛盾扣分。质量高≠必涨,仅代表当前结构与风险收益相对占优。</div>"
                         "<div class='spots'>" + "".join(cards) + "</div>")
            idx += 1
    parts.append(f"<h2>{'一二三四五六七八'[idx-1]}、个股详析({cnt},下拉切换)</h2>")
    if not stocks:
        parts.append("<p>无入池标的。</p>")
    else:
        parts.append(
            "<div id='stockPanel'><div class='ctrl'><select id='sel'></select><div class='tabs'>"
            "<div class='tab on' data-t='k'>K线</div><div class='tab' data-t='chan'>缠论(分型/笔/线段/中枢/买卖点)</div>"
            "<div class='tab' data-t='pattern'>形态(支撑压力/颈线)</div><div class='tab' data-t='candle'>K线形态(70种)</div></div>"
            "<button class='fsbtn' id='fsBtn' title='放大 / 全屏(再点还原)'>⤢ 全屏</button></div>"
            "<div class='hd' id='hd'></div><div class='stats' id='stats'></div>"
            "<div class='subctrl' id='subctrl'></div>"
            "<div class='row'><div class='kwrap'><div class='kside' id='kside'></div>"
            "<div class='kmain'><div class='kchart' id='k'></div><div class='phdrs' id='phdrs'></div></div></div>"
            "<div class='chipchart' id='chip'></div></div>"
            "<div class='flowchart' id='flow'></div><div id='candleList'></div><div id='judge'></div></div>")

    glossary = {}
    if GLOSSARY_PATH.exists():
        try:
            glossary = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            glossary = {}
    # 闭环:记录报告里出现但术语库未收录的名词(K线形态名/策略名),写 misses 供 expand_glossary 补充
    seen_terms = set()
    for s in stocks:
        for c in s.get("candles", []):
            seen_terms.add(c.get("name_cn", ""))
        if s.get("strategy"):
            seen_terms.add(s["strategy"])
    missing = sorted(t for t in seen_terms if t and t not in glossary)
    if missing:
        miss_path = GLOSSARY_PATH.parent / "glossary_misses.json"
        try:
            prev = json.loads(miss_path.read_text(encoding="utf-8")) if miss_path.exists() else []
            merged = sorted(set(prev) | set(missing))
            miss_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  [术语闭环] 记录 {len(missing)} 个未命中术语 → {miss_path.name}(运行 expand_glossary.py 补充)")
        except Exception:  # noqa: BLE001
            pass
    body = "\n".join(parts)
    return (f"<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>A股分析 {date}</title><style>{CSS}</style>"
            f"<script src='{ECHARTS_CDN}'></script></head><body><div class=wrap>{body}"
            f"<div class=foot>A股投资分析助手 · ECharts · 缠论/形态/筹码为估算 · 仅供研究参考</div>"
            f"</div><script>window.STOCKS={json.dumps(stocks, ensure_ascii=False)};"
            f"window.GLOSSARY={json.dumps(glossary, ensure_ascii=False)};</script><script>{JS}</script></body></html>")


def main():
    ap = argparse.ArgumentParser(description="富HTML交互报告(可合并热点池+全局池+消息面)")
    ap.add_argument("input", help="日报/点名 侧车 JSON")
    ap.add_argument("--global", dest="glob", help="全局池侧车 JSON(合并展示)")
    ap.add_argument("--news", help="消息面 Markdown(整体章节,合并展示)")
    ap.add_argument("--port-news", dest="port_news",
                    help="逐股消息面 Markdown(持仓诊断用,每股 `## 名称(代码)` 一节,回填到个股详析)")
    ap.add_argument("--action-plan", dest="action", help="作战方案 JSON(顶部卡片)")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"找不到侧车:{src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    if args.port_news and Path(args.port_news).exists():
        hit = _attach_port_news(Path(args.port_news).read_text(encoding="utf-8"),
                                data.get("picks", []))
        print(f"  逐股消息面已并入 {hit} 只")
    gdata = None
    if args.glob and Path(args.glob).exists():
        gdata = json.loads(Path(args.glob).read_text(encoding="utf-8"))
    news = None
    if args.news and Path(args.news).exists():
        news = Path(args.news).read_text(encoding="utf-8")
    action = None
    if args.action and Path(args.action).exists():
        action = json.loads(Path(args.action).read_text(encoding="utf-8"))
    out = Path(args.output) if args.output else src.with_name(src.stem + "-图表版.html")
    out.write_text(render(data, gdata, news, action), encoding="utf-8")
    print(f"完成 → {out}")


if __name__ == "__main__":
    main()
