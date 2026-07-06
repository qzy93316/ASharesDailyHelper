# -*- coding: utf-8 -*-
"""盘中机会池 —— 超短线试错入口(午间 11:30 跑一次)。

思路(与 daily_report 不同,专为超短线):
  ① 情绪闸门:先看涨停梯队(emotion 层)。最高连板 < 门槛 / 情绪冰点退潮 → 直接判"空仓"。
  ② 最强集群:从当日涨停池按所属行业聚合,找涨停家数最多的行业 = 当天最强题材集群。
  ③ 候选过滤(超短线口径):在最强集群板块内扫成分股,要求
       - 量比在 [volratio_min, volratio_max](温和放量,排除异动脉冲)
       - 换手率在 [turnover_min_pct, turnover_max_pct]
       - 不是已封死涨停(封死了买不进)、不是 3 板以上高位股
       - 排除 ST/次新(复用 filters)
       - 结构分达门槛(复用 scoring)
  ④ 输出:板块 → 逻辑 → 代表个股(含2:30入场纪律) → 风险提示。
     md + json 侧车 + 轻量 html(复用盘前报告 CSS 主题与环境卡片,无 ECharts)。

⚠️ 本工具仅供个人研究,不构成投资建议。数据来自免费接口,可能延迟。
   实盘的量比/换手/分时以你本机实时数据为准。

用法:  python scripts/intraday_pool.py            # 最近交易日
        python scripts/intraday_pool.py --date 20260706
        python scripts/intraday_pool.py --dry     # 只打印判定,不写文件
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402
import emotion  # noqa: E402
from indicators import compute_indicators  # noqa: E402
from scoring import score_stock  # noqa: E402
from daily_report import is_excluded, sector_size, analyze_sector_strength  # noqa: E402

ROOT = Path(__file__).parent.parent
NEWS_FILE = "盘中消息面.md"  # 午间联网写入的盘中催化剂/消息面(由 Claude 的 news-analysis 盘中版产出)


def _load_news(day_dir: Path) -> str | None:
    """读当日盘中消息面(超短线核心:题材催化剂/异动/公告)。无则 None。"""
    f = day_dir / NEWS_FILE
    if f.exists():
        t = f.read_text(encoding="utf-8").strip()
        return t or None
    return None


def _turn_pct(v) -> float:
    """换手率归一到百分数:kline 若给分数(≤1)则×100,已是百分数则原样。"""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(v * 100 if v <= 1 else v, 2)


def _limit_reached(code: str, pct_chg: float) -> bool:
    """现价是否已到/接近涨停(封死则买不进,排除)。复用 emotion 的按前缀阈值。"""
    return pct_chg >= emotion._limit_pct(str(code).zfill(6)) - 0.3


def leading_clusters(zt_df, top_n: int = 3) -> list[dict]:
    """当日涨停池按『所属行业』聚合 → 涨停家数最多的题材集群(当天最强方向)。"""
    if zt_df is None or not len(zt_df) or "所属行业" not in zt_df:
        return []
    counts: dict[str, int] = {}
    heights: dict[str, int] = {}
    for _, r in zt_df.iterrows():
        ind = str(r.get("所属行业") or "").strip()
        if not ind:
            continue
        counts[ind] = counts.get(ind, 0) + 1
        try:
            lb = int(r.get("连板数") or 1)
        except (TypeError, ValueError):
            lb = 1
        heights[ind] = max(heights.get(ind, 0), lb)
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"industry": k, "zt_count": v, "max_height": heights.get(k, 0)}
            for k, v in ranked[:top_n]]


def zt_index(zt_df) -> dict:
    """涨停池 → {代码: 连板数},用于高位板排除与『已涨停』判定。"""
    idx = {}
    if zt_df is None or not len(zt_df):
        return idx
    for _, r in zt_df.iterrows():
        code = str(r.get("代码") or "").zfill(6)
        try:
            idx[code] = int(r.get("连板数") or 1)
        except (TypeError, ValueError):
            idx[code] = 1
    return idx


def scan_candidates(cfg, clusters, zt_codes, regime):
    """在最强集群对应的板块里扫成分股,按超短线门槛过滤 → 候选池。
    集群名(涨停池的所属行业)与板块榜名称可能不完全一致,故用『子串匹配』兜底关联。"""
    ip, fl, hr = cfg["intraday_pool"], cfg["filters"], cfg["hard_rules"]
    rp = cfg["report"]
    try:
        sectors = fetcher.get_sector_rank()
    except Exception as e:  # noqa: BLE001
        print(f"  [板块榜获取失败] {e}")
        return [], "东财"
    src = sectors["_源"].iloc[0] if "_源" in sectors else "东财"
    cluster_names = [c["industry"] for c in clusters]

    def _match(sec_name: str) -> bool:
        s = str(sec_name)
        return any(cn and (cn in s or s in cn) for cn in cluster_names)

    ranked = sectors.sort_values("涨跌幅", ascending=False)
    # 优先取与最强集群匹配的板块;匹配不到就退回涨幅榜前列(仍按最强题材优先)
    matched = ranked[ranked["板块名称"].apply(_match)]
    use = matched if len(matched) else ranked.head(int(ip.get("max_concurrent", 1)) + 2)

    picks = []
    vr_min, vr_max = float(ip["volratio_min"]), float(ip["volratio_max"])
    to_min, to_max = float(ip["turnover_min_pct"]), float(ip["turnover_max_pct"])
    min_score = int(rp.get("min_score", 60)) + int(regime.get("score_delta", 0))
    max_lb = int(ip.get("min_ladder_height", 2))  # 高位板阈值参照:>2 板视为高位
    for _, sec in use.head(4).iterrows():
        try:
            cons = fetcher.get_sector_cons(sec)
        except Exception as e:  # noqa: BLE001
            print(f"  跳过板块 {sec['板块名称']}: 成分获取失败 —— {e}")
            continue
        cons = cons.sort_values("成交额", ascending=False) if "成交额" in cons else cons
        count = 0
        for _, row in cons.iterrows():
            if count >= int(rp.get("stocks_per_sector", 5)):
                break
            code, name = str(row["代码"]).zfill(6), row["名称"]
            if is_excluded(name, code, fl):
                continue
            count += 1
            lb = zt_codes.get(code, 0)
            if ip.get("exclude_high_board", True) and lb > max_lb:
                continue  # 3板以上高位股,试错期不碰
            try:
                # 盘中机会池须拿"当前时段"实时K(现价/量比/换手),强制不复用当日盘前缓存
                k = fetcher.get_kline(code, rp.get("kline_days", 180), force_fresh=True)
                if fl.get("exclude_sub_new") and len(k) < 60:
                    continue
                fresh, fresh_msg = fetcher.check_freshness(k, cfg["data"]["freshness_max_age_days"])
                if not fresh:
                    continue
                ind = compute_indicators(k)
                if _limit_reached(code, ind["pct_chg"]):
                    continue  # 已封死涨停,买不进,排除
                # ── 超短线信号闸门 ──
                if not (vr_min <= ind["vol_ratio"] <= vr_max):
                    continue
                turn = _turn_pct(k["换手"].iloc[-1] if "换手" in k else 0)
                if not (to_min <= turn <= to_max):
                    continue
                sc = score_stock(ind, hr)
                if sc["total"] < min_score or sc["vetoes"]:
                    continue
                heat = emotion.stock_heat(code, [{"c": float(x)} for x in k["收盘"].tail(61)],
                                          industry=str(sec["板块名称"]))
                picks.append({"sector": sec["板块名称"], "sector_pct": round(float(sec.get("涨跌幅", 0) or 0), 2),
                              "code": code, "name": name, "ind": ind, "sc": sc,
                              "turn": turn, "lb": lb, "fresh_msg": fresh_msg,
                              "heat": heat, "kline": k})
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {name}: {e}")
    # 结构强 + 温和放量优先
    picks.sort(key=lambda p: (p["sc"]["total"], p["ind"]["vol_ratio"]), reverse=True)
    return picks, src


def gate(cfg, emo, regime) -> tuple[bool, str]:
    """情绪/环境闸门:返回 (是否值得出手, 判定说明)。"""
    ip = cfg["intraday_pool"]
    need_height = int(ip.get("min_ladder_height", 2))
    if not emo:
        return False, "拿不到情绪数据(非交易日或接口失败),建议空仓等明确信号。"
    height = emo.get("max_height", 0)
    phase = emo.get("phase", "")
    reasons = []
    ok = True
    if height < need_height:
        ok = False
        reasons.append(f"最高连板仅 {height}(<{need_height}),涨停梯队不成型,情绪弱")
    if phase in ("冰点", "退潮"):
        ok = False
        reasons.append(f"情绪周期处于『{phase}』,亏钱效应主导")
    if regime.get("level") == "防守":
        reasons.append("大盘跌破 MA60(防守档),环境不利超短线,即便出手也要更轻")
    if ok and not reasons:
        reasons.append(f"梯队成型(最高{height}连板)、情绪『{phase}』,可小仓试错")
    return ok, ";".join(reasons)


def render(today, emo, regime, clusters, picks, worth, gate_msg, cfg, src, news=None) -> str:
    ip = cfg["intraday_pool"]
    dd = cfg["style"]["max_drawdown_pct"]
    L = [f"# 盘中机会池 — {today}(超短线试错)",
         "",
         "> ⚠️ 个人研究工具自动生成,**不构成投资建议**。实盘量比/换手/分时以你本机实时数据为准。",
         f"> 定位:小仓试错(≤{ip.get('position_cap_pct',20)}%),{ip.get('entry_window','14:30-14:57')} 择机手动入场,快进快出。",
         "",
         "## 一、今日环境判定"]
    if emo:
        ladder = " ".join(f"{k}板×{v}" for k, v in emo.get("ladder", {}).items())
        L += ["", f"- **情绪温度**:{emotion.brief(emo)}",
              f"- **涨停梯队**:{ladder or '无'}",
              f"- **大盘环境**:{regime.get('level','未知')} — {regime.get('note','')}"]
    verdict = "✅ 梯队成型,可小仓试错" if worth else "🛑 情绪不支持,建议今日空仓"
    L += ["", f"### 结论:{verdict}", "", f"> {gate_msg}", ""]
    if not worth:
        L += ["**今日不为做而做。** 空仓等下一个有清晰梯队的日子,是超短线最重要的纪律。",
              "", "---", "*信号由代码计算(AI零计算);参数见 config.yaml 的 intraday_pool。*"]
        return "\n".join(L)

    # 消息面 · 催化剂:超短线核心,必须与候选交叉验证(采纳用户强调)
    L += ["## 二、消息面 · 催化剂(超短线核心)", ""]
    if news:
        L += [news.strip(), ""]
    else:
        L += ["> ⏳ 待联网补充:对我说「盘中池消息面」,我搜当日最强集群/候选的**催化剂、异动、公告**后注入"
              "(`python scripts/intraday_pool.py --inject-news` 免重拉写入)。",
              "> **超短线吃的就是题材催化——没有催化剂 = 盲选强势票。务必先看这一节再决定是否出手。**", ""]

    L += ["## 三、最强题材集群(涨停家数聚合)", "",
          "| 集群(行业) | 涨停家数 | 集群内最高连板 |", "|---|---|---|"]
    for c in clusters:
        L.append(f"| {c['industry']} | {c['zt_count']} | {c['max_height']} |")

    L += ["", f"## 四、候选个股(共 {len(picks)} 只,数据源:{src})", "",
          "> 口径:最强集群板块内、温和放量、换手活跃、非封死涨停、非3板以上高位、结构达标。", ""]
    if not picks:
        L.append("集群内暂无满足超短线门槛的可买候选(可能强票都已封板)。宁可空手,不追高。")
    for p in picks:
        i, s = p["ind"], p["sc"]
        stop_soft = round(i["close"] * (1 - dd / 100), 2)
        heat_tag = ""
        if p.get("heat") and p["heat"].get("tags"):
            heat_tag = " · " + "; ".join(p["heat"]["tags"])
        L += [f"### {p['name']}({p['code']}) — {s['signal']} 评分 {s['total']}/100",
              "",
              f"- **所属集群**:{p['sector']}({p['sector_pct']:+.2f}%)"
              + (f" · 情绪画像:{p['heat']['grade']}{heat_tag}" if p.get("heat") else ""),
              f"- **现价**:{i['close']}({i['pct_chg']:+.2f}%)|{p['fresh_msg']}",
              f"- **超短线信号**:量比{i['vol_ratio']}({i['vol_pattern']})|换手{p['turn']}%|"
              f"{'当前' + str(p['lb']) + '连板' if p['lb'] else '未涨停'};"
              f"{i['alignment']};MACD {i['macd_cross']};RSI6={i['rsi6']};MA5乖离{i['bias5']}%",
              f"- **逻辑**:处当天最强集群、放量活跃且未封死;**须与上节『消息面·催化剂』交叉**"
              f"——所属集群/个股有真实催化才做,查无催化的孤立强势不追。尾盘分时仍强再小仓试错。",
              f"- **入场纪律**:{ip.get('entry_window','14:30-14:57')} 且分时走强才下手;单笔≤{ip.get('per_trade_pct',10)}%;"
              f"硬止损 -{dd}%(≈{stop_soft});软止损=次日不接力/低开走弱早盘即走,不等-{dd}%;"
              f"达+{ip.get('target_gain_pct',5)}%或情绪转弱先兑现。",
              f"- **反方理由(必读)**:集群若午后退潮/该股炸板,信号立即失效,不补仓、不扛单。",
              ""]

    L += ["---",
          "**离场提醒**:超短线是吃情绪溢价,不恋战。达标或转弱立刻走;连续负和请把 "
          "config.yaml 的 intraday_pool.enabled 设为 false 暂停。",
          "", "*信号由代码计算(AI零计算);参数见 config.yaml 的 intraday_pool。*"]
    return "\n".join(L)


def build_sidecar(today, emo, regime, clusters, picks, worth, gate_msg, cfg, src, news=None) -> dict:
    dd = cfg["style"]["max_drawdown_pct"]
    ip = cfg.get("intraday_pool", {})
    out = []
    for p in picks:
        i = p["ind"]
        out.append({"code": p["code"], "name": p["name"], "sector": p["sector"],
                    "sector_pct": p["sector_pct"], "signal": p["sc"]["signal"],
                    "score": p["sc"]["total"], "close": i["close"], "pct_chg": i["pct_chg"],
                    "vol_ratio": i["vol_ratio"], "turnover": p["turn"], "lb": p["lb"],
                    "soft_stop": round(i["close"] * (1 - dd / 100), 2),
                    "heat": p.get("heat"), "indicators": i})
    return {"date": str(today), "type": "intraday_pool", "source": src,
            "worth_trading": worth, "gate": gate_msg, "regime": regime, "emotion": emo,
            "clusters": clusters, "picks": out, "news": news,
            "params": {"entry_window": ip.get("entry_window", "14:30-14:57"),
                       "position_cap_pct": ip.get("position_cap_pct", 20),
                       "per_trade_pct": ip.get("per_trade_pct", 10),
                       "target_gain_pct": ip.get("target_gain_pct", 5),
                       "max_drawdown_pct": dd}}


def _render_html(sidecar, day_dir, today) -> None:
    """侧车 → 轻量 HTML(复用盘前报告 CSS 主题与环境卡片,无 ECharts)。"""
    try:
        sys.path.insert(0, str(ROOT / "skills" / "render-html" / "scripts"))
        import intraday_to_html  # noqa: E402
        html_out = day_dir / f"盘中机会池-{today}.html"
        html_out.write_text(intraday_to_html.render(sidecar), encoding="utf-8")
        print(f"网页 → {html_out}")
    except Exception as e:  # noqa: BLE001
        print(f"  [HTML 渲染跳过] {e}")


def _inject_md_news(md_path: Path, news: str) -> None:
    """把消息面注入已生成的 MD:定位 '## …消息面…' 标题,替换其正文到下一个 '## ' 前。"""
    if not md_path.exists():
        return
    lines = md_path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## ") and "消息面" in l), None)
    if start is None:
        return
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    lines[start:end] = [lines[start], "", news.strip(), ""]
    md_path.write_text("\n".join(lines), encoding="utf-8")


def cmd_inject_news(today) -> None:
    """免重拉注入:读已生成侧车 + 盘中消息面.md → 回写 json/md/html(不重新选股/拉K)。"""
    day_dir = ROOT / "reports" / str(today).replace("-", "")
    sc_path = day_dir / f"盘中机会池-{today}.json"
    if not sc_path.exists():
        print(f"找不到侧车 {sc_path},请先跑一次盘中机会池。"); return
    news = _load_news(day_dir)
    if not news:
        print(f"未找到 {day_dir / NEWS_FILE};请先把盘中消息面写入该文件再注入。"); return
    sidecar = json.loads(sc_path.read_text(encoding="utf-8"))
    sidecar["news"] = news
    sc_path.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    _inject_md_news(day_dir / f"盘中机会池-{today}.md", news)
    _render_html(sidecar, day_dir, today)
    print(f"✅ 消息面已注入(免重拉)→ {day_dir / f'盘中机会池-{today}.md'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="盘中机会池(超短线)")
    ap.add_argument("--date", help="YYYYMMDD,默认最近交易日")
    ap.add_argument("--dry", action="store_true", help="只打印判定,不写文件")
    ap.add_argument("--inject-news", action="store_true",
                    help="免重拉:把当日 盘中消息面.md 注入已生成的报告(json/md/html),不重新选股")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    ip = cfg.get("intraday_pool", {})
    if not ip.get("enabled", False):
        print("盘中机会池已暂停(config.yaml intraday_pool.enabled=false)。"); return

    today = dt.date.today() if not args.date else \
        dt.datetime.strptime(args.date, "%Y%m%d").date()
    day_dir = ROOT / "reports" / str(today).replace("-", "")
    if args.inject_news:
        cmd_inject_news(today); return
    news = _load_news(day_dir)  # 若午间消息面已写好则并入;否则报告出占位符提示补
    print("[1/4] 情绪梯队 + 大盘环境...")
    emo = emotion.snapshot(args.date) if args.date else emotion.latest_snapshot()
    regime = fetcher.get_index_regime()
    worth, gate_msg = gate(cfg, emo, regime)
    print(f"  判定:{'可试错' if worth else '空仓'} —— {gate_msg}")

    clusters, picks, src = [], [], "东财"
    if worth:
        print("[2/4] 涨停池 → 最强题材集群...")
        try:
            zt_df = fetcher.get_zt_pool(args.date)
        except Exception as e:  # noqa: BLE001
            print(f"  涨停池获取失败:{e}"); zt_df = None
        clusters = leading_clusters(zt_df, top_n=int(cfg["report"].get("top_concepts", 3)))
        zt_codes = zt_index(zt_df)
        print("  最强集群:" + (" / ".join(f"{c['industry']}({c['zt_count']}家)" for c in clusters) or "无"))
        print("[3/4] 集群内扫超短线候选...")
        picks, src = scan_candidates(cfg, clusters, zt_codes, regime)
        picks = picks[: max(1, int(ip.get("max_concurrent", 1))) + 2]  # 池子略多于持仓上限,留挑选空间
        print(f"  候选 {len(picks)} 只:" + ", ".join(p["name"] for p in picks))
    else:
        print("[2-3/4] 情绪不支持,跳过选股。")

    print("[4/4] 生成报告...")
    md = render(today, emo, regime, clusters, picks, worth, gate_msg, cfg, src, news=news)
    if args.dry:
        print("\n" + md); return
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"盘中机会池-{today}.md"
    out.write_text(md, encoding="utf-8")
    sidecar = build_sidecar(today, emo, regime, clusters, picks, worth, gate_msg, cfg, src, news=news)
    sc_out = day_dir / f"盘中机会池-{today}.json"
    sc_out.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成 → {out}")
    print(f"侧车 → {sc_out}")
    _render_html(sidecar, day_dir, today)
    if worth and not news:
        print(f"  ⏳ 尚无盘中消息面:搜候选/集群催化剂后写入 {day_dir / NEWS_FILE},"
              f"再跑 `python scripts/intraday_pool.py --inject-news` 注入(免重拉)。")


if __name__ == "__main__":
    main()
