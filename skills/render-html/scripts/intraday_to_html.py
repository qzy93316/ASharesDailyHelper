# -*- coding: utf-8 -*-
"""盘中机会池 → 轻量 HTML(超短线试错面板)。

复用盘前报告渲染器的资产,保持视觉统一:
  · CSS 暗色主题(卡片/表格/徽章/涨跌红绿)—— 从 report_to_html 直接 import
  · 大盘环境卡 _regime_card / 情绪温度计卡 _emotion_card —— 同一数据源
无 ECharts:盘中池定位「午间快速一眼看、快进快出」,只出结论 + 集群表 + 候选卡。

用法:python intraday_to_html.py <盘中机会池-YYYY-MM-DD.json> [-o out.html]
"""
import argparse
import html as _h
import json
from pathlib import Path

from report_to_html import CSS, SIGNAL_CLS, _regime_card, _emotion_card, _news_html  # noqa: E402


def _badge(sig: str) -> str:
    sig = sig or ""
    cls = "b-gray"
    for emo, c in SIGNAL_CLS.items():
        if emo in sig:
            cls = c
            break
    return f"<span class='badge {cls}'>{_h.escape(sig)}</span>"


def _pick_card(p: dict, ew: str, per, tgt, dd) -> str:
    i = p.get("indicators") or {}
    close, pct = p.get("close"), p.get("pct_chg", 0) or 0
    pcls = "up" if pct >= 0 else "down"
    sign = "+" if pct >= 0 else ""
    lb = p.get("lb") or 0
    lb_txt = f"当前{lb}连板" if lb else "未涨停"
    heat = p.get("heat") or {}
    heat_line = ""
    if heat:
        tags = "; ".join(heat.get("tags") or [])
        heat_line = (f"<div class='spot-kv'>情绪画像:{_h.escape(str(heat.get('grade', '')))}"
                     + (f" · {_h.escape(tags)}" if tags else "") + "</div>")
    return (
        "<div class='spot'>"
        f"<div class='spot-nm'>{_h.escape(str(p.get('name', '')))} <span class='cd'>{p.get('code', '')}</span>"
        f"{_badge(p.get('signal', ''))}<span class='badge b-score'>评分 {p.get('score', '')}/100</span></div>"
        f"<div class='spot-st'>{_h.escape(str(p.get('sector', '')))}({p.get('sector_pct', 0):+.2f}%)</div>"
        f"<div class='spot-kv'>现价 <b>{close}</b> <span class='{pcls}'>{sign}{pct}%</span>"
        f" · 量比 <b>{i.get('vol_ratio', p.get('vol_ratio'))}</b>({i.get('vol_pattern', '')})"
        f" · 换手 <b>{p.get('turnover')}%</b> · {lb_txt}</div>"
        f"<div class='spot-kv'>{i.get('alignment', '')} · MACD {i.get('macd_cross', '')}"
        f" · RSI6={i.get('rsi6', '')} · MA5乖离{i.get('bias5', '')}%</div>"
        f"{heat_line}"
        f"<div class='spot-rz'><b>入场纪律</b>:{ew} 且分时走强才下手;单笔≤{per}%;"
        f"硬止损 -{dd}%(≈{p.get('soft_stop')});软止损=次日不接力/低开走弱早盘即走,不等-{dd}%;"
        f"达+{tgt}%或情绪转弱先兑现。</div>"
        "<div class='spot-rz' style='color:#ffb0b0'><b>反方理由(必读)</b>:"
        "集群若午后退潮/该股炸板,信号立即失效,不补仓、不扛单。</div>"
        "</div>")


def render(data: dict) -> str:
    date, src = data.get("date", ""), data.get("source", "")
    worth, gate = data.get("worth_trading"), data.get("gate", "")
    prm = data.get("params") or {}
    ew = prm.get("entry_window", "14:30-14:57")
    cap = prm.get("position_cap_pct", 20)
    per = prm.get("per_trade_pct", 10)
    tgt = prm.get("target_gain_pct", 5)
    dd = prm.get("max_drawdown_pct", 7)

    parts = [f"<h1>盘中机会池 · {date} <span style='font-size:14px;color:var(--muted)'>(超短线试错)</span></h1>",
             "<div class='note'>⚠️ 个人研究工具自动生成,<b>不构成投资建议</b>。实盘量比/换手/分时以你本机实时数据为准。"
             f"定位:小仓试错(≤{cap}%),{ew} 择机手动入场,快进快出。数据源:{src or '—'}。</div>"]

    rc = _regime_card(data.get("regime"))
    if rc:
        parts.append(rc)
    ecard = _emotion_card(data.get("emotion"), hd="🌡️ 短线情绪温度计(超短线核心闸门)")
    if ecard:
        parts.append(ecard)

    vcls = "act-bull" if worth else "act-bear"
    verdict = "✅ 梯队成型,可小仓试错" if worth else "🛑 情绪不支持,建议今日空仓"
    parts.append(f"<div class='action {vcls}'><div class='act-hd'>🎯 今日结论</div>"
                 f"<div class='act-main'>{verdict}</div>"
                 f"<div class='act-sec'>{_h.escape(gate)}</div></div>")

    if not worth:
        parts.append("<div class='note' style='color:#8fbaff;border-color:#4c8dff'>"
                     "<b>今日不为做而做。</b> 空仓等下一个有清晰梯队的日子,是超短线最重要的纪律。</div>")
        return _wrap(date, "\n".join(parts))

    # 消息面 · 催化剂(超短线核心):有则卡片展示,无则占位提示
    news = data.get("news")
    parts.append("<h2>二、消息面 · 催化剂(超短线核心)</h2>")
    if news:
        parts.append(_news_html(news))
    else:
        parts.append("<div class='note' style='border-color:#ff8a3d;color:#ffb37a'>"
                     "⏳ 待联网补充催化剂/异动/公告。<b>超短线吃题材催化,没有催化剂 = 盲选强势票,"
                     "务必先看这一节再决定是否出手。</b></div>")

    clusters = data.get("clusters") or []
    if clusters:
        parts.append("<h2>三、最强题材集群(涨停家数聚合)</h2>"
                     "<table><thead><tr><th>集群(行业)</th><th>涨停家数</th><th>集群内最高连板</th>"
                     "</tr></thead><tbody>")
        for c in clusters:
            parts.append(f"<tr><td>{_h.escape(str(c.get('industry', '')))}</td>"
                         f"<td>{c.get('zt_count', '')}</td><td>{c.get('max_height', '')}</td></tr>")
        parts.append("</tbody></table>")

    picks = data.get("picks") or []
    parts.append(f"<h2>四、候选个股(共 {len(picks)} 只)</h2>"
                 "<div class='note'>口径:最强集群板块内、温和放量、换手活跃、非封死涨停、非3板以上高位、结构达标。</div>")
    if not picks:
        parts.append("<p>集群内暂无满足超短线门槛的可买候选(可能强票都已封板)。宁可空手,不追高。</p>")
    else:
        parts.append("<div class='spots'>"
                     + "".join(_pick_card(p, ew, per, tgt, dd) for p in picks) + "</div>")

    parts.append("<div class='note' style='border-color:#ff8a3d;color:#ffb37a'>"
                 "<b>离场提醒</b>:超短线是吃情绪溢价,不恋战。达标或转弱立刻走;"
                 "连续负和请把 config.yaml 的 intraday_pool.enabled 设为 false 暂停。</div>")
    return _wrap(date, "\n".join(parts))


def _wrap(date: str, body: str) -> str:
    return (f"<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>盘中机会池 {date}</title><style>{CSS}</style></head>"
            f"<body><div class=wrap>{body}"
            f"<div class=foot>A股投资分析助手 · 盘中机会池(超短线)· 仅供研究参考,不构成投资建议</div>"
            f"</div></body></html>")


def main() -> None:
    ap = argparse.ArgumentParser(description="盘中机会池 → 轻量 HTML")
    ap.add_argument("input", help="盘中机会池侧车 JSON")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"找不到侧车:{src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    out = Path(args.output) if args.output else src.with_suffix(".html")
    out.write_text(render(data), encoding="utf-8")
    print(f"完成 → {out}")


if __name__ == "__main__":
    main()
