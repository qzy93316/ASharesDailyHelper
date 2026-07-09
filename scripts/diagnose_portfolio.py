# -*- coding: utf-8 -*-
"""持仓诊断 —— 读 portfolio/ 下导出的持仓文件,逐只复用点名分析引擎 + 成本感知诊断,
再出组合层面体检(集中度/主线暴露/资金流/风险),产出诊断报告(md)+ 侧车(可渲染HTML)。

安全边界:只读持仓数据做研究诊断,**不接触账户密码、不做任何实盘下单**。
持仓文件从券商/同花顺 APP 手动导出即可,无需任何凭证。

支持的持仓文件(放 portfolio/,自动取最新):
  · 同花顺导出的 .xls(实为 GBK Tab 分隔文本)—— 列:证券代码/证券名称/股票余额/成本价/市价/市值…
  · 通用 .csv(UTF-8/GBK)、真 .xlsx
识别列(中文名,容错匹配):代码、名称、数量(股票余额/持仓)、成本价、市价(现价)。

用法:
  python diagnose_portfolio.py                 # 诊断 portfolio/ 最新文件
  python diagnose_portfolio.py 持仓.xls        # 指定文件
输出:reports/YYYYMMDD/持仓诊断-YYYY-MM-DD.(md/json)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402
import analyze  # noqa: E402  复用 analyze_one(指标+筹码+资金+情绪+基本面+研判)

ROOT = Path(__file__).parent.parent
PORT_DIR = ROOT / "portfolio"

# 列名容错匹配(同花顺/通达信/银河等导出命名各异)
_COL = {
    "code": ["证券代码", "代码", "股票代码", "证券编码"],
    "name": ["证券名称", "名称", "股票名称", "证券简称"],
    "qty": ["股票余额", "持仓数量", "数量", "持股数量", "股份余额", "参考持股"],
    "cost": ["成本价", "成本", "买入均价", "持仓成本", "参考成本价"],
    "price": ["市价", "现价", "最新价", "参考市价"],
}


def _pick(cols, keys):
    for k in keys:
        for c in cols:
            if k == str(c).strip():
                return c
    for k in keys:  # 再做包含匹配
        for c in cols:
            if k in str(c):
                return c
    return None


def _read_any(path: Path) -> pd.DataFrame:
    """健壮读取:真xlsx → GBK制表符(同花顺xls) → UTF8/GBK csv。"""
    errs = []
    if path.suffix.lower() in (".xlsx", ".xls"):
        try:
            return pd.read_excel(path)  # 真 Excel
        except Exception as e:  # noqa: BLE001
            errs.append(f"excel:{e}")
    for enc in ("gbk", "utf-8-sig", "utf-8"):
        for sep in ("\t", ","):
            try:
                df = pd.read_csv(path, sep=sep, encoding=enc, dtype=str)
                if df.shape[1] >= 4:  # 列够多才算解析成功
                    return df
            except Exception as e:  # noqa: BLE001
                errs.append(f"{enc}/{sep!r}:{e}")
    raise RuntimeError(f"无法解析持仓文件 {path.name};尝试记录:{'; '.join(errs[:4])}")


def read_holdings(path: Path) -> list[dict]:
    df = _read_any(path)
    df.columns = [str(c).strip() for c in df.columns]
    cols = list(df.columns)
    m = {k: _pick(cols, ks) for k, ks in _COL.items()}
    if not m["code"] or not m["cost"]:
        raise RuntimeError(f"未识别到 代码/成本价 列;实际列名:{cols}")
    out = []
    for _, r in df.iterrows():
        raw = str(r[m["code"]]).strip().replace(".0", "")
        code = "".join(ch for ch in raw if ch.isdigit()).zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue

        def num(col):
            if not col or pd.isna(r.get(col)):
                return None
            try:
                return float(str(r[col]).replace(",", "").strip())
            except (ValueError, TypeError):
                return None
        name = str(r[m["name"]]).strip() if m["name"] else code
        qty = num(m["qty"])
        # 跳过已清仓的残留空行(数量明确为 0):0 股不是持仓,否则会被误计成一只、
        # 且成本常为 0 → 显示"成本0.0/None%"噪声。qty 为 None(无数量列)则不过滤。
        if qty is not None and qty == 0:
            continue
        out.append({"code": code, "name": name, "qty": qty,
                    "cost": num(m["cost"]), "price_file": num(m["price"])})
    return out


def diagnose_one(h: dict, cfg: dict) -> dict:
    """单只:点名分析 + 成本感知诊断。"""
    p = analyze.analyze_one(h["code"], h["name"], cfg)
    ind, jd = p["indicators"], p["judge"]
    price = ind["close"]
    cost = h["cost"]
    stop = jd["structural_stop"]["stop"]
    dd = cfg["style"]["max_drawdown_pct"]
    pnl_pct = round((price / cost - 1) * 100, 2) if cost else None
    dist_stop = round((price / stop - 1) * 100, 2) if stop else None  # 现价距止损%
    mktval = round(price * h["qty"], 1) if h.get("qty") else None
    fs = p.get("fund_flow") or []
    main5 = round(sum(x["main"] for x in fs[-5:]), 0) if fs else 0
    verdict, tag = _verdict(p, cost, price, stop, pnl_pct, dd, main5, dist_stop)
    p.update({"cost": cost, "qty": h.get("qty"), "mktval": mktval, "pnl_pct": pnl_pct,
              "dist_stop_pct": dist_stop, "main5": main5,
              "holding_verdict": verdict, "holding_tag": tag})
    return p


def _verdict(p, cost, price, stop, pnl_pct, dd, main5, dist_stop=None):
    """成本感知的规则化诊断(透明可核,非喊单)。返回 (文字, 标签)。"""
    ind, jd = p["indicators"], p["judge"]
    # 强弱判定单源:以均线排列为准(stance 本就由 alignment 派生,不再匹配 stance 文案,避免文案一改就误判)
    weak = ind.get("alignment") in ("空头排列", "弱空")
    outflow = main5 < 0
    below_stop = stop and price <= stop
    grey = jd.get("grey") or []  # 灰区预警(RSI偏热/乖离接近追高/换手过高)—— 落地到诊断,不再只在报告角落显示

    def _ret(text, tag):
        if grey and tag not in ("止损", "重亏警戒"):  # 已到重警级别就不再叠加灰区,免噪音
            text += ";灰区预警:" + "、".join(grey)
        return text, tag

    if below_stop:
        return _ret(f"⛔ 已跌破结构止损 {stop}({jd['structural_stop'].get('basis','')}),"
                    f"纪律上应减仓/离场,勿扛单", "止损")
    if pnl_pct is not None and pnl_pct <= -dd * 1.5:
        return _ret(f"⚠️ 浮亏 {pnl_pct}% 已超止损容忍({dd}%)近1.5倍,趋势未反转前不宜补仓摊低,"
                    f"反弹优先减亏;结构止损 {stop}", "重亏警戒")
    if weak and outflow:
        return _ret(f"🔻 趋势偏弱 + 主力近5日净流出{main5}万,反弹到压力/成本区减仓为主,不追不补;"
                    f"守 {stop}", "逢反弹减")
    if (not weak) and main5 > 0:
        return _ret(f"✅ 结构相对健康 + 资金未流出,可持有;跌破结构止损 {stop} 再离场", "持有")
    if weak and main5 > 0:
        return _ret(f"👀 趋势偏弱但主力逆势吸筹({main5}万),持有观察,严守 {stop}", "持有观察")
    return _ret(f"持有观察,以结构止损 {stop} 为纪律线(现价距止损 {dist_stop if dist_stop is not None else '-'}%)", "观察")


def portfolio_summary(picks: list[dict], cash: float | None = None) -> dict:
    tot_mkt = round(sum(p.get("mktval") or 0 for p in picks), 1)
    tot_cost = round(sum((p["cost"] * p["qty"]) for p in picks if p.get("cost") and p.get("qty")), 1)
    tot_pnl = round(tot_mkt - tot_cost, 1) if tot_mkt and tot_cost else None
    losers = sum(1 for p in picks if (p.get("pnl_pct") or 0) < 0)
    outflow_n = sum(1 for p in picks if (p.get("main5") or 0) < 0)
    heaviest = max(picks, key=lambda p: p.get("mktval") or 0, default=None)
    conc = (round((heaviest["mktval"] / tot_mkt) * 100, 1)
            if heaviest and tot_mkt else None)
    industries = {}
    for p in picks:
        # 用基本面/情绪里的行业信息粗聚合(无则用名称占位)
        ind_name = ((p.get("stock_emotion") or {}).get("industry")
                    or p.get("sector") or "—")
        industries[ind_name] = industries.get(ind_name, 0) + (p.get("mktval") or 0)
    return {"holdings": len(picks), "total_mktval": tot_mkt, "total_cost": tot_cost,
            "total_pnl": tot_pnl,
            "total_pnl_pct": round(tot_pnl / tot_cost * 100, 2) if tot_pnl and tot_cost else None,
            "losers": losers, "outflow_count": outflow_n,
            "heaviest": (heaviest["name"] if heaviest else None), "top_concentration_pct": conc,
            "cash": cash,
            "position_pct": (round(tot_mkt / (tot_mkt + cash) * 100, 1)
                             if cash is not None and tot_mkt else None)}


def render_md(today, picks, summ) -> str:
    L = [f"# 持仓诊断报告 — {today}", "",
         "> ⚠️ 基于历史/技术数据的研究性诊断,**不构成投资建议**;只读持仓、不涉账户密码、不做实盘下单。", "",
         "## 组合体检", ""]
    L.append(f"- 持仓 **{summ['holdings']}** 只 · 市值 **{summ['total_mktval']}** 元 · "
             f"浮盈亏 **{summ['total_pnl']}** 元({summ.get('total_pnl_pct')}%)· 亏损 {summ['losers']} 只")
    if summ.get("position_pct") is not None:
        L.append(f"- 仓位 **{summ['position_pct']}%**(现金 {summ['cash']} 元)—— "
                 + ("偏重,防守空间小" if summ['position_pct'] >= 85 else "尚有现金缓冲"))
    L.append(f"- 最大单票 **{summ['heaviest']}** 占 {summ['top_concentration_pct']}% · "
             f"主力净流出个股 **{summ['outflow_count']}/{summ['holdings']}** 只")
    L += ["", "## 逐只诊断(结合成本)", "",
          "| 股票 | 成本/现价 | 盈亏% | 信号 | 评分 | 结构止损 | 诊断 |",
          "|---|---|---|---|---|---|---|"]
    for p in picks:
        i = p["indicators"]
        L.append(f"| {p['name']}({p['code']}) | {p['cost']}/{i['close']} | {p.get('pnl_pct')}% | "
                 f"{p['signal']} | {p['score']} | {p['judge']['structural_stop']['stop']} | "
                 f"[{p['holding_tag']}] {p['holding_verdict']} |")
    L += ["", "## 逐只要点", ""]
    for p in picks:
        i, jd = p["indicators"], p["judge"]
        L += [f"### {p['name']}({p['code']}) — {p['signal']} 评分{p['score']} · [{p['holding_tag']}]",
              f"- **成本 {p['cost']} / 现价 {i['close']}({p.get('pnl_pct')}%)** · 市值 {p.get('mktval')} 元",
              f"- 技术:{i['alignment']};MACD {i['macd_cross']};RSI6 {i['rsi6']};乖离 {i['bias5']}%",
              f"- 立场:{jd.get('stance','')}",
              f"- 资金:主力近5日 {p.get('main5')} 万" + (f";基本面 {(p.get('fundamental') or {}).get('comment','')}" if p.get("fundamental") else ""),
              f"- **诊断**:{p['holding_verdict']}",
              (f"- 矛盾:{'; '.join(jd['tensions'])}" if jd.get("tensions") else ""),
              ""]
    L += ["---", "*诊断由代码计算(AI零计算),仅供研究复盘;止损为结构化支撑,请自行复核决策。*"]
    return "\n".join(x for x in L if x is not None)


def render_html(sidecar: dict, out_path: Path) -> None:
    """顺手出一份暗色交互图表版(无消息面);要逐股消息面走 report_to_html.py --port-news。"""
    sys.path.insert(0, str(ROOT / "skills" / "render-html" / "scripts"))
    import report_to_html  # noqa: E402
    out_path.write_text(report_to_html.render(sidecar), encoding="utf-8")


def main() -> None:
    # Windows GBK 控制台打印诊断标签里的 emoji(🔻✅⛔ 等)会 UnicodeEncodeError,兜底为 replace
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    want_html = "--html" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        path = Path(args[0])
        if not path.is_absolute():
            path = PORT_DIR / path
    else:
        files = sorted(PORT_DIR.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        files = [f for f in files if f.suffix.lower() in (".xls", ".xlsx", ".csv")]
        if not files:
            print(f"portfolio/ 下没有持仓文件(.xls/.xlsx/.csv)。请从券商/同花顺导出后放入 {PORT_DIR}")
            return
        path = files[0]
    print(f"读取持仓文件:{path.name}")
    holdings = read_holdings(path)
    if not holdings:
        print("未解析到有效持仓行。"); return
    print(f"共 {len(holdings)} 只,逐只诊断中(复用点名分析引擎)...")
    picks = []
    for h in holdings:
        print(f"  诊断 {h['name']}({h['code']}) 成本{h['cost']} ...")
        try:
            picks.append(diagnose_one(h, cfg))
        except Exception as e:  # noqa: BLE001
            print(f"    失败:{e}")
    if not picks:
        print("无有效诊断结果。"); return
    summ = portfolio_summary(picks)
    today = dt.date.today()
    day_dir = ROOT / "reports" / str(today).replace("-", "")
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"持仓诊断-{today}.md").write_text(render_md(today, picks, summ), encoding="utf-8")
    sidecar = {"date": str(today), "source": "持仓诊断", "scanned": len(picks),
               "indexes": fetcher.get_index_snapshot(), "sectors": [],
               "portfolio": summ, "picks": picks}
    json_path = day_dir / f"持仓诊断-{today}.json"
    json_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成 → {day_dir / f'持仓诊断-{today}.md'}")
    print(f"侧车 → {json_path}")
    if want_html:
        html_path = day_dir / f"持仓诊断-{today}-图表版.html"
        try:
            render_html(sidecar, html_path)
            print(f"图表版 → {html_path}(暗色交互:K线/缠论/形态/资金/基本面 + 组合体检)")
        except Exception as e:  # noqa: BLE001
            print(f"图表版渲染失败(可手动跑 report_to_html.py):{e}")
    print(f"\n组合:{summ['holdings']}只 市值{summ['total_mktval']} 浮亏盈{summ['total_pnl']}"
          f"({summ.get('total_pnl_pct')}%) 亏损{summ['losers']}只 主力流出{summ['outflow_count']}只")
    for p in picks:
        print(f"  [{p['holding_tag']}] {p['name']} 成本{p['cost']}/现{p['indicators']['close']}"
              f"({p.get('pnl_pct')}%) {p['signal']} — {p['holding_verdict']}")
    if not want_html:
        print("\n出富交互图表版(推荐):")
        print("  1) 本脚本加 --html 出快速版(无消息面)")
        print("  2) Claude 联网逐股搜消息面 → 写 reports/YYYYMMDD/持仓消息面-<日期>.md(每股 `## 名称(代码)` 一节)")
        print(f'  3) python skills/render-html/scripts/report_to_html.py "{json_path}" \\')
        print(f'       --port-news "reports/{str(today).replace("-","")}/持仓消息面-{today}.md" \\')
        print(f'       -o "{day_dir / f"持仓诊断-{today}-图表版.html"}"')


if __name__ == "__main__":
    main()
