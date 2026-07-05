# -*- coding: utf-8 -*-
"""每日报告生成器 — 一期 MVP 核心入口。
用法:  python scripts/daily_report.py
输出:  reports/日报-YYYY-MM-DD.md
消息面章节留占位符,由 Claude 在「今日热点」流程中联网搜索后补写。
"""
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402
import chips  # noqa: E402
import emotion  # noqa: E402
from indicators import compute_indicators  # noqa: E402
from scoring import score_stock  # noqa: E402

ROOT = Path(__file__).parent.parent

# 同花顺自查指引知识库
THS_GUIDE = {
    "均线": "个股K线页,主图默认显示 MA5/10/20/60;数字键可切换周期",
    "MACD": "个股页副图默认第一个指标;或输入 MACD 回车切换",
    "RSI": "个股页副图输入 RSI 回车;看 RSI6 与 80/20 的位置",
    "量比": "个股页行情栏即有『量比』字段;>1.5 为放量",
    "换手率": "行情栏『换手』字段;题材股 5%~15% 为活跃区间",
    "乖离率": "副图输入 BIAS 回车;BIAS1(6日)>5 视为短期过热",
    "板块资金": "同花顺首页→行情→板块→行业,按『净流入』排序",
    "F10股东": "个股页按 F10 →『股东研究』,看股东人数增减(减少=筹码集中)",
    "F10商誉": "F10 →『财务分析』→ 资产负债表,搜『商誉』,商誉/净资产>30%需警惕",
    "F10解禁": "F10 →『股本结构』→ 限售解禁,近期大额解禁是利空",
}


def is_excluded(name: str, code: str, filters: dict) -> bool:
    if filters.get("exclude_st") and "ST" in name.upper():
        return True
    if filters.get("exclude_bse") and code[:2] in ("92", "83", "87", "88", "43"):
        return True
    if filters.get("exclude_chinext") and code[:3] in ("300", "301"):  # 创业板
        return True
    if filters.get("exclude_star") and code[:3] in ("688", "689"):  # 科创板
        return True
    return False


def analyze_sector_strength(sec, cons) -> dict:
    """板块强弱量化(先看板块):不只看涨跌幅,而是综合
    ①动量(涨跌幅)②广度(上涨家数占比 —— 普涨才是真强,只领涨股涨是假强)
    ③中位涨幅(抗领涨股拉偏)。强弱分越高越强,给出 强/中/弱 评级。"""
    import pandas as pd
    pct = float(sec.get("涨跌幅", 0) or 0)
    adv_ratio, median_chg, n_up, n_total = None, None, 0, 0
    if cons is not None and "涨跌幅" in cons and len(cons):
        chg = pd.to_numeric(cons["涨跌幅"], errors="coerce").dropna()
        if len(chg):
            n_total = len(chg)
            n_up = int((chg > 0).sum())
            adv_ratio = round(n_up / n_total, 3)
            median_chg = round(float(chg.median()), 2)
    strength = pct * 0.4
    if adv_ratio is not None:
        strength += (adv_ratio * 100 - 50) * 0.3 + (median_chg or 0) * 0.3
    strength = round(strength, 1)
    grade = "强" if strength >= 5 else ("中" if strength >= 1 else "弱")
    return {"name": sec["板块名称"], "pct": pct, "key": sec.get("_key", sec["板块名称"]),
            "src": sec.get("_源", "东财"), "adv_ratio": adv_ratio, "median_chg": median_chg,
            "n_up": n_up, "n_total": n_total, "strength": strength, "grade": grade,
            "leader": sec.get("领涨股票", "—")}


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rp, fl, hr = cfg["report"], cfg["filters"], cfg["hard_rules"]
    today = dt.date.today()

    print("[1/4] 拉取大盘指数...")
    indexes = fetcher.get_index_snapshot()
    regime = fetcher.get_index_regime()  # 大盘环境总开关:弱市自动收紧
    eff_min_score = rp["min_score"] + regime.get("score_delta", 0)
    eff_max_picks = max(1, round(rp["max_picks"] * regime.get("picks_factor", 1.0)))
    print(f"  [环境] 大盘 {regime.get('level')} → 入池门槛 {eff_min_score} 分、上限 {eff_max_picks} 只")
    # 情绪周期温度计(影子运行:只展示/落台账,暂不 gate 选股;胜率验证后转正)
    emo = emotion.latest_snapshot()
    if emo:
        print(f"  [情绪] {emotion.brief(emo)}")

    print("[2/4] 拉取板块行情 + 强弱分析...")
    sectors = fetcher.get_sector_rank()
    src = sectors["_源"].iloc[0] if "_源" in sectors else "东财"
    print(f"  板块数据源:{src}")
    # 先看板块:对涨幅靠前的板块做强弱分析(涨跌幅+广度+中位涨幅),再按强弱分排序
    cand_sectors = sectors.sort_values("涨跌幅", ascending=False).head(rp["top_sectors"] * 2)
    sector_rank, cons_cache = [], {}
    for _, sec in cand_sectors.iterrows():
        try:
            cons = fetcher.get_sector_cons(sec)
        except Exception as e:  # noqa: BLE001
            print(f"  跳过板块 {sec['板块名称']}: 成分股获取失败 —— {e}")
            continue
        cons_cache[sec["板块名称"]] = cons
        sector_rank.append(analyze_sector_strength(sec, cons))
    sector_rank.sort(key=lambda s: s["strength"], reverse=True)
    top_sectors = sector_rank[: rp["top_sectors"]]
    print("  板块强弱榜:" + " / ".join(
        f"{s['name']}[{s['grade']}{s['strength']} 涨{s['pct']}% 广度"
        f"{int(s['adv_ratio']*100) if s['adv_ratio'] is not None else '-'}%]" for s in top_sectors))

    print("[3/4] 扫描强板块内候选股,选高于板块平均结构者...")
    picks, scanned = [], 0
    for sec_info in top_sectors:
        cons = cons_cache.get(sec_info["name"])
        if cons is None:
            continue
        cons = cons.sort_values("成交额", ascending=False) if "成交额" in cons else cons
        graded, count = [], 0
        for _, row in cons.iterrows():
            if count >= rp["stocks_per_sector"]:
                break
            code, name = str(row["代码"]).zfill(6), row["名称"]
            if is_excluded(name, code, fl):
                continue
            count += 1
            scanned += 1
            try:
                k = fetcher.get_kline(code, rp["kline_days"])
                if fl.get("exclude_sub_new") and len(k) < 60:
                    continue
                fresh, fresh_msg = fetcher.check_freshness(k, cfg["data"]["freshness_max_age_days"])
                if not fresh:
                    continue
                ind = compute_indicators(k)
                sc = score_stock(ind, hr)
                graded.append({"sector": sec_info["name"], "sector_pct": sec_info["pct"],
                               "code": code, "name": name, "ind": ind, "sc": sc,
                               "fresh_msg": fresh_msg, "kline": k})
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {name}: {e}")
        if not graded:
            continue
        avg = sum(g["sc"]["total"] for g in graded) / len(graded)  # 板块平均结构分
        for g in graded:
            g["sector_avg"] = round(avg, 1)
            g["above_sector"] = g["sc"]["total"] > avg  # 高于板块平均=板块内相对强
            picks.append(g)

    # 去重(同股可能出现在多个板块)→ 评分门槛 → 硬规则否决 → 相对强度优先 → 数量上限
    seen, deduped = set(), []
    for p in picks:
        if p["code"] not in seen:
            seen.add(p["code"])
            deduped.append(p)
    picks = [p for p in deduped
             if p["sc"]["total"] >= eff_min_score and not p["sc"]["vetoes"]]
    # 板块内相对强(高于板块平均结构)优先,再按评分 —— "强板块里的强结构股"
    picks.sort(key=lambda p: (p.get("above_sector", False), p["sc"]["total"]), reverse=True)
    picks = picks[: eff_max_picks]

    print("[4/4] 生成报告...")
    md = render(today, indexes, top_sectors, picks, scanned, cfg, source=src, emo=emo)
    day_dir = ROOT / "reports" / str(today).replace("-", "")  # 按日期归档
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"日报-{today}.md"
    out.write_text(md, encoding="utf-8")
    # 结构化侧车:供 富HTML图表渲染 与 盘后复盘验证 消费(不必再解析 Markdown)
    sidecar = build_sidecar(today, indexes, top_sectors, picks, scanned, cfg, src, regime, emo)
    sc_out = day_dir / f"日报-{today}.json"
    sc_out.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 → {out}")
    print(f"数据侧车 → {sc_out}")


def _plan_prices(ind: dict, dd: float) -> tuple[float, float]:
    """止损 = MA20 与 -dd% 取高者;目标 = 20日压力位。与报告正文口径一致。"""
    stop = max(ind["ma20"], round(ind["close"] * (1 - dd / 100), 2))
    return round(stop, 2), round(ind["pressure"], 2)


def build_sidecar(today, indexes, top_sectors, picks, scanned, cfg, source, regime=None,
                  emo=None) -> dict:
    """把当日报告的全部结构化数据(K线/筹码/资金流)导出为机器可读 JSON。"""
    dd = cfg["style"]["max_drawdown_pct"]
    chart_bars = int(cfg.get("report", {}).get("chart_bars", 120))
    out_picks = []
    for p in picks:
        ind = p["ind"]
        stop, target = _plan_prices(ind, dd)
        k = p["kline"].tail(chart_bars)
        bars = [{"d": str(r["日期"]), "o": float(r["开盘"]), "c": float(r["收盘"]),
                 "l": float(r["最低"]), "h": float(r["最高"]), "v": float(r["成交量"]),
                 "换手": round(float(r.get("换手", 0) or 0), 5)}
                for _, r in k.iterrows()]
        chip = chips.compute_chips(bars)
        flow = fetcher.get_fund_flow(p["code"])
        chan_res = chan.analyze(bars)
        turn_pct = round((bars[-1].get("换手", 0) or 0) * 100, 2)
        jd = judge.synthesize(ind, chip, judge.flow_sum(flow), chan_res, target, dd, turn_pct)
        out_picks.append({
            "code": p["code"], "name": p["name"],
            "sector": p["sector"], "sector_pct": round(float(p["sector_pct"]), 2),
            "signal": p["sc"]["signal"], "score": p["sc"]["total"],
            "breakdown": p["sc"]["breakdown"],
            "entry_date": ind["date"], "entry_close": ind["close"],
            "plan_stop": jd["structural_stop"]["stop"], "plan_target": target,
            "indicators": ind, "bars": bars,
            "chips": chip, "chip_comment": chips.control_comment(chip),
            "fund_flow": flow, "judge": jd,
            "sector_avg": p.get("sector_avg"), "above_sector": p.get("above_sector", False),
        })
    return {
        "date": str(today), "source": source, "scanned": scanned,
        "indexes": indexes, "regime": regime, "emotion": emo,
        "sectors": [{"name": s["name"], "pct": s["pct"], "strength": s["strength"],
                     "grade": s["grade"], "adv_ratio": s["adv_ratio"],
                     "median_chg": s["median_chg"], "n_up": s["n_up"], "n_total": s["n_total"],
                     "leader": s.get("leader", "—")} for s in top_sectors],
        "picks": out_picks,
    }


def render(today, indexes, top_sectors, picks, scanned, cfg, source="东财", emo=None) -> str:
    dd = cfg["style"]["max_drawdown_pct"]
    L = [f"# A股每日盘前报告 — {today}",
         "",
         "> ⚠️ 本报告由个人分析工具自动生成,不构成投资建议。数据来自免费接口,可能有延迟。",
         "",
         "## 一、大盘",
         "",
         "| 指数 | 收盘 | 涨跌幅 | 数据日期 |",
         "|---|---|---|---|"]
    for ix in indexes:
        L.append(f"| {ix['name']} | {ix['close']} | {ix['pct']:+.2f}% | {ix['date']} |")

    if emo:
        ladder = " ".join(f"{k}板×{v}" for k, v in emo.get("ladder", {}).items())
        L += ["", f"**短线情绪温度计(影子指标)**:{emotion.brief(emo)}",
              f"> 连板梯队:{ladder or '无'} · 周期阶段建议:{emo['note']}(暂不影响选股门槛,验证期)"]

    L += ["", "## 二、板块强弱榜", "",
          "> 强弱分 = 动量(涨跌幅) + 广度(上涨家数占比) + 中位涨幅;广度高=普涨真强,只领涨股涨=假强。", "",
          "| 板块 | 强弱 | 强弱分 | 涨跌幅 | 上涨广度 | 中位涨幅 |", "|---|---|---|---|---|---|"]
    for s in top_sectors:
        adv = f"{int(s['adv_ratio']*100)}%({s['n_up']}/{s['n_total']})" if s.get("adv_ratio") is not None else "—"
        med = f"{s['median_chg']:+.2f}%" if s.get("median_chg") is not None else "—"
        L.append(f"| {s['name']} | {s['grade']} | {s['strength']} | {s['pct']:+.2f}% | {adv} | {med} |")

    L += ["", "## 三、消息面(待 Claude 联网补充)", "",
          "<!-- CLAUDE_NEWS_PLACEHOLDER: 对『今日热点』说一声,我会联网搜索当日政策/热点并按",
          "     板块→催化剂→短期情绪还是中期逻辑 的格式补写本节 -->", ""]

    L += [f"## 四、技术面荐股池(扫描 {scanned} 只,入池 {len(picks)} 只,数据源:{source})", ""]
    if not picks:
        L.append("今日无标的通过评分+硬规则筛选,空仓观望也是操作。")
    for p in picks:
        i, s = p["ind"], p["sc"]
        stop = max(i["ma20"], round(i["close"] * (1 - dd / 100), 2))
        bd = " / ".join(f"{k}{v}" for k, v in s["breakdown"].items())
        reasons = "; ".join(
            ["板块若高开低走则热点属一日游,信号失效",
             f"若收盘跌破 MA10({i['ma10']})则短线趋势转弱"])
        rel = ""
        if p.get("sector_avg") is not None:
            rel = (f",**高于板块平均结构**(板块均{p['sector_avg']})" if p.get("above_sector")
                   else f"(板块均{p['sector_avg']})")
        L += [f"### {p['name']}({p['code']}) — {s['signal']} 评分 {s['total']}/100",
              "",
              f"- **所属板块**:{p['sector']}({p['sector_pct']:+.2f}%){rel}",
              f"- **现价**:{i['close']}({i['pct_chg']:+.2f}%)|{p['fresh_msg']}",
              f"- **技术面**:{i['alignment']};MACD {i['macd_cross']}"
              f"{'(零轴上)' if i['macd_above_zero'] else '(零轴下)'};"
              f"RSI6={i['rsi6']};量比{i['vol_ratio']}({i['vol_pattern']});"
              f"MA5乖离{i['bias5']}%",
              f"- **评分明细**:{bd}",
              f"- **操作参考**:支撑 {i['support']} / 压力 {i['pressure']};"
              f"止损 {stop}(MA20 与 -{dd}% 取高者)",
              f"- **反方理由(必读)**:{reasons}",
              f"- **同花顺自查**:{THS_GUIDE['均线']};{THS_GUIDE['MACD']};"
              f"F10 检查:{THS_GUIDE['F10解禁']}",
              ""]

    L += ["## 五、同花顺指标速查表", "",
          "| 项目 | 查看方法 |", "|---|---|"]
    for k, v in THS_GUIDE.items():
        L.append(f"| {k} | {v} |")
    L += ["", "---", "*技术指标由代码计算(AI零计算);评分规则见 scripts/scoring.py;"
          "风格配置见 config.yaml。*"]
    return "\n".join(L)


if __name__ == "__main__":
    main()
