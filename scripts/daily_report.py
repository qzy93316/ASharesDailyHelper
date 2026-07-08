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
import chan  # noqa: E402
import judge  # noqa: E402
import emotion  # noqa: E402
import fundamental  # noqa: E402
import signals  # noqa: E402  逐日买卖信号(反哺研判)
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


def sector_size(sec, n_total: int = 0) -> int:
    """板块容量(成分股数):DangInvest 的『成分数』精确 → 东财的『上涨+下跌家数』近似
    → 成分股实际条数 n_total 兜底。数据源都没给时返回 0(上游据此『不误杀』)。"""
    s = int(sec.get("成分数") or 0)
    if not s:
        up, dn = sec.get("上涨家数"), sec.get("下跌家数")
        if up is not None or dn is not None:
            try:
                s = int(up or 0) + int(dn or 0)
            except (TypeError, ValueError):
                s = 0
    return s or n_total


def analyze_sector_strength(sec, cons, size_weight: float = 0.0) -> dict:
    """板块强弱量化(先看板块):不只看涨跌幅,而是综合
    ①动量(涨跌幅)②广度(上涨家数占比 —— 普涨才是真强,只领涨股涨是假强)
    ③中位涨幅(抗领涨股拉偏)。强弱分越高越强,给出 强/中/弱 评级。
    容量(方案B):强弱分保持纯净(只驱动评级),另算 size_score=log10(成分数)×权重,
    合成 rank_score=强弱分+size_score 供选板块排序 —— 大容量同等强度优先,评级口径不变。"""
    import math
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
    size = sector_size(sec, n_total)
    mcap_yi = round(float(sec.get("总市值") or 0) / 1e8)  # 元 → 亿
    size_score = round(math.log10(max(size, 1)) * size_weight, 1)
    rank_score = round(strength + size_score, 1)
    return {"name": sec["板块名称"], "pct": pct, "key": sec.get("_key", sec["板块名称"]),
            "src": sec.get("_源", "东财"), "adv_ratio": adv_ratio, "median_chg": median_chg,
            "n_up": n_up, "n_total": n_total, "strength": strength, "grade": grade,
            "size": size, "mcap_yi": mcap_yi, "size_score": size_score,
            "rank_score": rank_score, "leader": sec.get("领涨股票", "—")}


def scan_concepts(cfg, eff_min_score, exclude_codes):
    """方案C 概念板块并轨:同花顺概念 → 三重过滤(黑名单+容量上下限)→ 涨幅排序
    → 热榜(前8) + 概念内个股扫描(top_concepts×stocks_per_sector,评分门槛+否决)。
    概念不套用容量权重(已卡进区间,概念非越大越好);与行业池按代码去重。
    返回 (concept_hotlist, concept_picks)。"""
    rp, fl, hr = cfg["report"], cfg["filters"], cfg["hard_rules"]
    if not rp.get("scan_concepts"):
        return [], []
    try:
        cr = fetcher.get_concept_rank()
    except Exception as e:  # noqa: BLE001
        print(f"  [概念并轨] 概念榜获取失败,跳过 —— {e}")
        return [], []
    black = rp.get("concept_blacklist") or []
    max_size, min_size = int(rp.get("concept_max_size", 400)), int(rp.get("min_sector_size", 15))

    def _keep(row) -> bool:
        name = str(row["板块名称"])
        if any(b in name for b in black):
            return False
        sz = int(row.get("成分数") or 0)
        return min_size <= sz <= max_size

    cand = cr[cr.apply(_keep, axis=1)].sort_values("涨跌幅", ascending=False)
    if not len(cand):
        return [], []
    hotlist = [{"name": r["板块名称"], "pct": round(float(r["涨跌幅"]), 2),
                "size": int(r.get("成分数") or 0)} for _, r in cand.head(8).iterrows()]
    picks = []
    for _, csec in cand.head(int(rp.get("top_concepts", 3))).iterrows():
        cname, cpct = csec["板块名称"], round(float(csec["涨跌幅"]), 2)
        try:
            cons = fetcher.get_concept_cons(csec["_key"])
        except Exception as e:  # noqa: BLE001
            print(f"  跳过概念 {cname}: 成分股获取失败 —— {e}")
            continue
        cons = cons.sort_values("成交额", ascending=False) if "成交额" in cons else cons
        count = 0
        for _, row in cons.iterrows():
            if count >= rp["stocks_per_sector"]:
                break
            code, name = str(row["代码"]).zfill(6), row["名称"]
            if is_excluded(name, code, fl) or code in exclude_codes:
                continue
            count += 1
            try:
                k = fetcher.get_kline(code, rp["kline_days"])
                if fl.get("exclude_sub_new") and len(k) < 60:
                    continue
                fresh, fresh_msg = fetcher.check_freshness(k, cfg["data"]["freshness_max_age_days"])
                if not fresh:
                    continue
                ind = compute_indicators(k)
                sc = score_stock(ind, hr)
                if sc["total"] < eff_min_score or sc["vetoes"]:
                    continue
                exclude_codes.add(code)  # 防同一股在多个热门概念里重复入池
                picks.append({"sector": f"概念·{cname}", "sector_pct": cpct,
                              "code": code, "name": name, "ind": ind, "sc": sc,
                              "fresh_msg": fresh_msg, "kline": k, "pool": "概念"})
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {name}: {e}")
    return hotlist, picks


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
    # 先看板块:涨幅排序 → 容量门槛(A)滤掉伪板块 → 强弱+容量分(B)重排 → 取 top_sectors
    size_weight = float(rp.get("sector_size_weight", 0) or 0)
    min_size = int(rp.get("min_sector_size", 0) or 0)
    ranked = sectors.sort_values("涨跌幅", ascending=False)
    if min_size > 0:
        # 成分数为0(数据源未给容量)时保留不误杀;<门槛的小容量板块滤除
        sizes = ranked.apply(lambda r: sector_size(r), axis=1)
        killed = ranked[(sizes > 0) & (sizes < min_size)]
        ranked = ranked[~((sizes > 0) & (sizes < min_size))]
        if len(killed):
            dropped = [(r["板块名称"], sector_size(r)) for _, r in killed.head(6).iterrows()]
            print(f"  [容量门槛<{min_size}只] 滤除小容量板块:"
                  + ", ".join(f"{n}({s}只)" for n, s in dropped)
                  + (" …" if len(killed) > 6 else ""))
    cand_sectors = ranked.head(rp["top_sectors"] * 2)
    sector_rank, cons_cache = [], {}
    for _, sec in cand_sectors.iterrows():
        try:
            cons = fetcher.get_sector_cons(sec)
        except Exception as e:  # noqa: BLE001
            print(f"  跳过板块 {sec['板块名称']}: 成分股获取失败 —— {e}")
            continue
        cons_cache[sec["板块名称"]] = cons
        sector_rank.append(analyze_sector_strength(sec, cons, size_weight))
    sector_rank.sort(key=lambda s: s["rank_score"], reverse=True)  # 容量加权后的排序分
    top_sectors = sector_rank[: rp["top_sectors"]]
    print("  板块强弱榜:" + " / ".join(
        f"{s['name']}[{s['grade']}{s['strength']}+容量{s['size_score']}={s['rank_score']} "
        f"{s['size']}只 涨{s['pct']}%]" for s in top_sectors))

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

    # 方案C:概念板块并轨 —— 过滤垃圾概念 → 热榜 + 概念内个股(独立配额,与行业池去重)
    concept_hotlist, concept_picks = scan_concepts(cfg, eff_min_score, {p["code"] for p in picks})
    concept_picks = concept_picks[: eff_max_picks]
    if concept_picks:
        print(f"  [概念并轨] 概念池入选 {len(concept_picks)} 只:"
              + ", ".join(f"{p['name']}({p['sector']})" for p in concept_picks))
    all_picks = picks + concept_picks

    print("[4/4] 生成报告...")
    md = render(today, indexes, top_sectors, all_picks, scanned, cfg, source=src, emo=emo,
                concepts=concept_hotlist)
    day_dir = ROOT / "reports" / str(today).replace("-", "")  # 按日期归档
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"日报-{today}.md"
    out.write_text(md, encoding="utf-8")
    # 结构化侧车:供 富HTML图表渲染 与 盘后复盘验证 消费(不必再解析 Markdown)
    sidecar = build_sidecar(today, indexes, top_sectors, all_picks, scanned, cfg, src, regime, emo,
                            concepts=concept_hotlist)
    sc_out = day_dir / f"日报-{today}.json"
    sc_out.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 → {out}")
    print(f"数据侧车 → {sc_out}")


def _plan_prices(ind: dict, dd: float) -> tuple[float, float]:
    """止损 = MA20 与 -dd% 取高者;目标 = 20日压力位。与报告正文口径一致。"""
    stop = max(ind["ma20"], round(ind["close"] * (1 - dd / 100), 2))
    return round(stop, 2), round(ind["pressure"], 2)


def build_sidecar(today, indexes, top_sectors, picks, scanned, cfg, source, regime=None,
                  emo=None, concepts=None) -> dict:
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
        sig = signals.compute(signals.series_from_bars(bars))       # 逐日信号(图上箭头 + 反哺研判)
        sig_sum = signals.latest_summary(sig, [b["d"] for b in bars])
        jd = judge.synthesize(ind, chip, judge.flow_sum(flow), chan_res, target, dd, turn_pct, sig=sig_sum)
        heat = emotion.stock_heat(p["code"], bars, industry=p.get("sector", ""))
        fnd = fundamental.summarize(p["code"])
        out_picks.append({
            "code": p["code"], "name": p["name"], "stock_emotion": heat, "fundamental": fnd,
            "sector": p["sector"], "sector_pct": round(float(p["sector_pct"]), 2),
            "signal": p["sc"]["signal"], "score": p["sc"]["total"],
            "breakdown": p["sc"]["breakdown"],
            "entry_date": ind["date"], "entry_close": ind["close"],
            "plan_stop": jd["structural_stop"]["stop"], "plan_target": target,
            "indicators": ind, "bars": bars, "signals": sig, "signal_summary": sig_sum,
            "chips": chip, "chip_comment": chips.control_comment(chip),
            "fund_flow": flow, "judge": jd,
            "sector_avg": p.get("sector_avg"), "above_sector": p.get("above_sector", False),
            "pool": p.get("pool", ""),
        })
    return {
        "date": str(today), "source": source, "scanned": scanned,
        "indexes": indexes, "regime": regime, "emotion": emo,
        "sectors": [{"name": s["name"], "pct": s["pct"], "strength": s["strength"],
                     "grade": s["grade"], "adv_ratio": s["adv_ratio"],
                     "median_chg": s["median_chg"], "n_up": s["n_up"], "n_total": s["n_total"],
                     "size": s.get("size"), "mcap_yi": s.get("mcap_yi"),
                     "size_score": s.get("size_score"), "rank_score": s.get("rank_score"),
                     "leader": s.get("leader", "—")} for s in top_sectors],
        "concepts": concepts or [],
        "picks": out_picks,
    }


def render(today, indexes, top_sectors, picks, scanned, cfg, source="东财", emo=None,
           concepts=None) -> str:
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
          "> 强弱分 = 动量(涨跌幅) + 广度(上涨家数占比) + 中位涨幅;广度高=普涨真强,只领涨股涨=假强。",
          "> 排序分 = 强弱分 + 容量分(log10成分数×权重);大容量板块梯队深、资金承接强,同等强度优先(小容量板块已按门槛滤除)。", "",
          "| 板块 | 强弱 | 强弱分 | 容量分 | 排序分 | 成分数 | 总市值(亿) | 涨跌幅 | 上涨广度 | 中位涨幅 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for s in top_sectors:
        adv = f"{int(s['adv_ratio']*100)}%({s['n_up']}/{s['n_total']})" if s.get("adv_ratio") is not None else "—"
        med = f"{s['median_chg']:+.2f}%" if s.get("median_chg") is not None else "—"
        L.append(f"| {s['name']} | {s['grade']} | {s['strength']} | +{s.get('size_score',0)} | "
                 f"{s.get('rank_score',s['strength'])} | {s.get('size','—')} | {s.get('mcap_yi','—')} | "
                 f"{s['pct']:+.2f}% | {adv} | {med} |")

    # 大盘(一)、板块强弱榜(二)之后动态编号,概念热榜可选
    _CN = "一二三四五六七八"
    _n = [3]  # 下一个大节序号(从三开始)

    def _sec(title):
        h = f"## {_CN[_n[0]-1]}、{title}"
        _n[0] += 1
        return h

    if concepts:
        L += ["", _sec("概念热榜(题材维度,已滤除融资融券/沪股通类伪概念及指数化巨型概念)"), "",
              "> 概念是题材聚合,与行业分类互补;此处按涨幅取紧凑热题材(成分15~上限,过滤名单式伪概念)。", "",
              "| 概念 | 涨跌幅 | 成分数 |", "|---|---|---|"]
        for c in concepts:
            L.append(f"| {c['name']} | {c['pct']:+.2f}% | {c['size']} |")

    L += ["", _sec("消息面(待 Claude 联网补充)"), "",
          "<!-- CLAUDE_NEWS_PLACEHOLDER: 对『今日热点』说一声,我会联网搜索当日政策/热点并按",
          "     板块→催化剂→短期情绪还是中期逻辑 的格式补写本节。搜索由『板块强弱榜+概念热榜』共同驱动 -->", ""]

    L += [_sec(f"技术面荐股池(扫描 {scanned} 只,入池 {len(picks)} 只,数据源:{source})"), ""]
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
        heat = emotion.stock_heat(p["code"], [{"c": float(x)} for x in p["kline"]["收盘"].tail(61)],
                                  industry=p.get("sector", ""))
        heat_line = (f"- **个股情绪(影子)**:{heat['grade']}"
                     + (f";{'; '.join(heat['tags'])}" if heat["tags"] else "")) if heat else None
        fnd = fundamental.summarize(p["code"])
        fnd_line = (f"- **基本面速览**:{fnd['comment']}"
                    + (f"|{';'.join(t for t in fnd['tags'] if '分位' not in t)}"
                       if fnd.get("tags") else "")) if fnd else None
        L += [f"### {p['name']}({p['code']}) — {s['signal']} 评分 {s['total']}/100",
              "",
              f"- **所属板块**:{p['sector']}({p['sector_pct']:+.2f}%){rel}",
              *([heat_line] if heat_line else []),
              *([fnd_line] if fnd_line else []),
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

    L += [_sec("同花顺指标速查表"), "",
          "| 项目 | 查看方法 |", "|---|---|"]
    for k, v in THS_GUIDE.items():
        L.append(f"| {k} | {v} |")
    L += ["", "---", "*技术指标由代码计算(AI零计算);评分规则见 scripts/scoring.py;"
          "风格配置见 config.yaml。*"]
    return "\n".join(L)


if __name__ == "__main__":
    main()
