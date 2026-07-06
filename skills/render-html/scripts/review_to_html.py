# -*- coding: utf-8 -*-
"""复盘 → 图文 HTML(暗色主题,复用盘前报告 CSS)。两模块并列展示:
  ① 荐股维度复盘 —— 热点/全局/盘中三池,按荐股当初计划(建仓价/目标/止损)判对错
  ② 持仓维度诊断 —— 按你的实际成本做健康度体检(若传入持仓诊断侧车则并入)
两维度分开看:荐股维度与你的持仓成本无关,持仓维度才是你手上的钱。

用法:
  python review_to_html.py <复盘-YYYY-MM-DD.json> [--diag 持仓诊断-YYYY-MM-DD.json] [-o out.html]
"""
import argparse
import html as _h
import json
from pathlib import Path

from report_to_html import CSS, SIGNAL_CLS  # noqa: E402

# 复盘表专属微调:数字/短文本不换行(根治通用 md 渲染把"热点池"竖排的可读性问题)
EXTRA = ("<style>"
         ".rv table td,.rv table th{white-space:nowrap}"
         ".rv .nm{white-space:nowrap;font-weight:600}"
         ".mod-h{margin:26px 0 2px;font-size:20px;font-weight:800;color:#e8eefc;"
         "border-left:4px solid var(--gold);padding-left:10px}"
         ".mod-sub{color:var(--muted);font-size:13px;margin:2px 0 6px}"
         ".cards{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}"
         ".mc{flex:1 1 150px;min-width:130px;background:var(--panel);border:1px solid var(--line);"
         "border-radius:10px;padding:10px 14px}"
         ".mc .k{font-size:12px;color:var(--muted)}.mc .v{font-size:20px;font-weight:800;margin-top:2px}"
         "</style>")


def _badge(sig: str) -> str:
    sig = sig or ""
    cls = "b-gray"
    for emo, c in SIGNAL_CLS.items():
        if emo in sig:
            cls = c
            break
    return f"<span class='badge {cls}'>{_h.escape(sig)}</span>"


def _pct(v, plus=True) -> str:
    """带红绿的百分比;None → —。"""
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _h.escape(str(v))
    cls = "up" if f >= 0 else "down"
    sign = "+" if (plus and f >= 0) else ""
    return f"<span class='{cls}'>{sign}{f}%</span>"


def _outcome_html(o: str) -> str:
    o = o or ""
    if "达标" in o:
        return f"<span class='up'>✅ {_h.escape(o)}</span>"
    if "止损" in o:
        return f"<span class='down'>❌ {_h.escape(o)}</span>"
    if "小胜" in o:
        return f"<span class='up'>{_h.escape(o)}</span>"
    if "小负" in o:
        return f"<span class='down'>{_h.escape(o)}</span>"
    return _h.escape(o)


# ── 模块①:荐股维度复盘 ──────────────────────────────────────────────
def _review_module(rv: dict) -> str:
    summ = rv.get("summary") or {}
    by_pool = rv.get("by_pool") or {}
    hold = rv.get("hold", 1)
    span = "次日" if hold == 1 else f"{hold}日"
    parts = ["<div class='mod-h'>① 荐股维度复盘</div>",
             "<div class='mod-sub'>热点池 / 全局池 / 盘中池三池合验 · 持有期"
             f"{span} · 按荐股当初计划(建仓价/目标/止损)判对错,<b>与你的持仓成本无关</b></div>"]
    if summ.get("evaluated"):
        cards = [("已验证", f"{summ['evaluated']} 只"),
                 ("命中率", f"{summ['win_rate_pct']}%"),
                 ("达标/止损", f"{summ['hit_target']} / {summ['hit_stop']}"),
                 (f"{span}均收益", f"{summ['avg_ret_hold']}%"),
                 ("均最大浮盈", f"{summ['avg_max_gain']}%"),
                 ("均最大浮亏", f"{summ['avg_max_dd']}%")]
        parts.append("<div class='cards'>" + "".join(
            f"<div class='mc'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
            for k, v in cards) + "</div>")
        bp = " · ".join(f"{k} <b>{v['win_rate_pct']}%</b>({v['n']}只)"
                        for k, v in by_pool.items() if v.get('n'))
        if bp:
            parts.append(f"<div class='mod-sub'>分池命中率:{bp}</div>")
    else:
        parts.append("<div class='note'>本期暂无可验证标的(荐股日之后尚无足够交易日)。</div>")

    parts.append("<div class='rv'><table><thead><tr>"
                 "<th>日期</th><th>池</th><th>名称</th><th>代码</th><th>信号</th><th>评分</th>"
                 "<th>建仓价</th><th>次日%</th><th>持有期%</th><th>最大浮盈%</th><th>最大浮亏%</th><th>结果</th>"
                 "</tr></thead><tbody>")
    for r in rv.get("rows", []):
        if r.get("pending"):
            parts.append(
                f"<tr><td>{r.get('entry_date','')}</td><td>{_h.escape(str(r.get('pool','')))}</td>"
                f"<td class='nm'>{_h.escape(str(r.get('name','')))}</td><td>{r.get('code','')}</td>"
                f"<td>{_badge(r.get('signal',''))}</td><td>{r.get('score','')}</td>"
                f"<td>{r.get('entry','')}</td><td>—</td><td>—</td><td>—</td><td>—</td>"
                f"<td class='muted'>待验证</td></tr>")
        else:
            parts.append(
                f"<tr><td>{r.get('entry_date','')}</td><td>{_h.escape(str(r.get('pool','')))}</td>"
                f"<td class='nm'>{_h.escape(str(r.get('name','')))}</td><td>{r.get('code','')}</td>"
                f"<td>{_badge(r.get('signal',''))}</td><td>{r.get('score','')}</td>"
                f"<td>{r.get('entry','')}</td><td>{_pct(r.get('ret_1'))}</td><td>{_pct(r.get('ret_hold'))}</td>"
                f"<td>{_pct(r.get('max_gain'))}</td><td>{_pct(r.get('max_dd'))}</td>"
                f"<td>{_outcome_html(r.get('outcome',''))}</td></tr>")
    parts.append("</tbody></table></div>")
    return "\n".join(parts)


# ── 模块②:持仓维度诊断 ──────────────────────────────────────────────
def _diag_module(diag: dict) -> str:
    summ = diag.get("portfolio") or {}
    picks = diag.get("picks") or []
    parts = ["<div class='mod-h'>② 持仓维度诊断</div>",
             "<div class='mod-sub'>按你的<b>实际成本</b>做健康度体检 —— 这是你手上的钱,"
             "与上面的荐股对错是两回事。操作以次日盘前作战方案为准。</div>"]
    if summ:
        pos = summ.get("position_pct")
        cards = [("持仓", f"{summ.get('holdings','—')} 只"),
                 ("市值", f"{summ.get('total_mktval','—')}"),
                 ("浮盈亏", f"{summ.get('total_pnl','—')}({summ.get('total_pnl_pct')}%)"),
                 ("亏损", f"{summ.get('losers','—')} 只"),
                 ("仓位", f"{pos}%" if pos is not None else "—"),
                 ("主力净流出", f"{summ.get('outflow_count','—')}/{summ.get('holdings','—')} 只")]
        parts.append("<div class='cards'>" + "".join(
            f"<div class='mc'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
            for k, v in cards) + "</div>")
        if summ.get("heaviest"):
            parts.append(f"<div class='mod-sub'>最大单票 <b>{_h.escape(str(summ['heaviest']))}</b> "
                         f"占 {summ.get('top_concentration_pct')}%</div>")

    parts.append("<div class='rv'><table><thead><tr>"
                 "<th>股票</th><th>代码</th><th>成本</th><th>现价</th><th>盈亏%</th>"
                 "<th>信号</th><th>评分</th><th>结构止损</th><th>诊断</th>"
                 "</tr></thead><tbody>")
    for p in picks:
        i = p.get("indicators") or {}
        stop = (((p.get("judge") or {}).get("structural_stop") or {}).get("stop"))
        tag = p.get("holding_tag", "")
        verdict = p.get("holding_verdict", "")
        parts.append(
            f"<tr><td class='nm'>{_h.escape(str(p.get('name','')))}</td><td>{p.get('code','')}</td>"
            f"<td>{p.get('cost','')}</td><td>{i.get('close','')}</td><td>{_pct(p.get('pnl_pct'))}</td>"
            f"<td>{_badge(p.get('signal',''))}</td><td>{p.get('score','')}</td><td>{stop if stop is not None else '—'}</td>"
            f"<td><b>[{_h.escape(str(tag))}]</b> {_h.escape(str(verdict))}</td></tr>")
    parts.append("</tbody></table></div>")
    return "\n".join(parts)


def render(rv: dict, diag: dict | None = None) -> str:
    date = rv.get("date", "")
    body = [f"<h1>盘后复盘 · {date}</h1>",
            "<div class='note'>⚠️ 个人研究工具自动生成,<b>不构成投资建议</b>。"
            "荐股维度检验系统荐股决策对错;持仓维度按你的成本诊断健康度。两维度分开看。</div>",
            _review_module(rv)]
    if diag:
        body.append(_diag_module(diag))
    else:
        body.append("<div class='mod-h'>② 持仓维度诊断</div>"
                    "<div class='note'>未提供持仓诊断数据(把 portfolio/ 持仓文件备好,"
                    "复盘会自动并入此模块)。</div>")
    return (f"<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>盘后复盘 {date}</title><style>{CSS}</style>{EXTRA}</head>"
            f"<body><div class=wrap>{''.join(body)}"
            f"<div class=foot>A股投资分析助手 · 盘后复盘(荐股维度+持仓维度)· 仅供研究参考,不构成投资建议</div>"
            f"</div></body></html>")


def main() -> None:
    ap = argparse.ArgumentParser(description="复盘 → 图文 HTML(荐股维度 + 持仓维度)")
    ap.add_argument("input", help="复盘侧车 JSON(reviews/复盘-YYYY-MM-DD.json)")
    ap.add_argument("--diag", help="持仓诊断侧车 JSON(可选,并入持仓维度模块)")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"找不到复盘侧车:{src}")
    rv = json.loads(src.read_text(encoding="utf-8"))
    diag = None
    if args.diag and Path(args.diag).exists():
        diag = json.loads(Path(args.diag).read_text(encoding="utf-8"))
    out = Path(args.output) if args.output else src.with_suffix(".html")
    out.write_text(render(rv, diag), encoding="utf-8")
    print(f"完成 → {out}")


if __name__ == "__main__":
    main()
