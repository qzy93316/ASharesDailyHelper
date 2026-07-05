# -*- coding: utf-8 -*-
"""情绪周期温度计 —— 短线生态的环境仪表(涨停/炸板/跌停/昨涨停四池 → 温度0~100 + 周期阶段)。

指标(公式为业界通行口径,端点配方参考 third_party/a-stock-data Layer 8):
  炸板率   = 炸板数 / (涨停数 + 炸板数)            —— 承接力,>40% 视为退潮信号
  连板高度 = 涨停池最高连板数                       —— 情绪空间上限
  连板梯队 = {板数: 家数}                           —— 梯队断层 = 接力风险
  晋级率   = 昨涨停今仍涨停家数 / 昨涨停总数        —— 接力赚钱效应
  昨停溢价 = 昨日涨停池今日平均涨幅                 —— 打板隔日赚钱效应(最敏感)
温度合成后映射周期阶段:冰点 → 退潮 → 分歧 → 发酵 → 高潮(对齐 tuige market-regime 标签)。

设计原则:AI 零计算(全部代码算);影子运行(先不 gate 选股,写侧车+台账,由
backtest-review 的 by_phase 分组胜率验证有效性后再转正)。

用法:
  python emotion.py                    # 最近交易日快照(缓存优先)
  python emotion.py --date 20260703    # 指定日
  python emotion.py --backfill 90      # 回填近90个自然日(校准阈值/给复盘配历史)
  python emotion.py --history 10       # 打印最近10条已落库快照
数据:东财 push2ex(与 push2his 同源,curl_cffi Chrome 指纹直连);逐日快照落库
data/market.db(key=emotion:YYYYMMDD),历史快照不可变、天然可累积。
"""
import argparse
import datetime as dt
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cache  # noqa: E402
import fetcher  # noqa: E402

ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"

# 温度分档 → 周期阶段/操作标签(对齐 third_party tuige market-regime 五档)
PHASES = [
    (70, "高潮", "rotation",      "情绪过热,防分歧转折,只做有辨识度轮动,不追最高标"),
    (55, "发酵", "aggressive",    "赚钱效应扩张,短线最佳窗口,可考虑接力/趋势"),
    (40, "分歧", "pullback_only", "多空拉锯,只做回调确认/洗盘末端,不追高"),
    (25, "退潮", "defensive",     "亏钱效应主导,防守为主,轻仓或观望"),
    (0,  "冰点", "no_trade",      "情绪冰点,默认空仓;留意冰点转折的低吸试错点"),
]


def _limit_pct(code: str) -> float:
    """按代码前缀给涨停幅度判定阈值(留 0.2% 容差):主板10% / 创业科创20% / 北交30%。"""
    if code.startswith(("30", "68")):
        return 19.8
    if code.startswith(("8", "4", "92")):
        return 29.8
    return 9.8


def _pool(endpoint: str, sort: str, date: str) -> tuple[list[dict], str | None]:
    """东财涨停板行情中心四池通用请求(push2ex)。返回 (pool, qdate实际数据日)。
    注意:请求未来/非交易日时东财会回退返回最近交易日数据(qdate 标注真实日期),
    必须校验 qdate == 请求日,否则周末跑会把最近交易日数据错误落到当天。"""
    r = fetcher._cf.get(f"https://push2ex.eastmoney.com/{endpoint}",
                        params={"ut": ZTB_UT, "dpt": "wz.ztzt", "Pageindex": 0,
                                "pagesize": 10000, "sort": sort, "date": date},
                        headers={"Referer": "https://quote.eastmoney.com/"},
                        timeout=15, impersonate="chrome", proxies=fetcher._NO_PROXY)
    r.raise_for_status()
    data = r.json().get("data") or {}
    qdate = str(data.get("qdate") or "") or None
    return (data.get("pool") or []), qdate


def _temperature(zt_n: int, dt_n: int, break_rate: float, max_height: int,
                 promotion: float | None, premium: float | None) -> int:
    """六因子加权合成 0~100。阈值取通行经验值,可用 --backfill 的历史分布校准。"""
    def clamp(x):
        return max(0.0, min(1.0, x))
    s_zt = clamp((zt_n - 20) / (120 - 20))              # 涨停家数:20冰点 ~ 120高潮
    s_break = clamp(1 - (break_rate - 15) / (45 - 15))  # 炸板率:15%满分 ~ 45%零分
    s_height = clamp(max_height / 7)                    # 连板高度:7板见顶
    s_prom = clamp(((promotion or 0) - 10) / (40 - 10)) # 晋级率:10%冰 ~ 40%沸
    s_prem = clamp(((premium if premium is not None else 0) + 3) / (5 + 3))  # 溢价:-3%~+5%
    s_dt = clamp(1 - dt_n / 30)                         # 跌停家数:30只清零
    w = (0.20 * s_zt + 0.20 * s_break + 0.15 * s_height
         + 0.15 * s_prom + 0.20 * s_prem + 0.10 * s_dt)
    return round(w * 100)


def _phase(temp: int) -> tuple[str, str, str]:
    for th, name, tag, note in PHASES:
        if temp >= th:
            return name, tag, note
    return PHASES[-1][1:]


def snapshot(date: str, refresh: bool = False) -> dict | None:
    """单交易日情绪快照(缓存优先,历史快照不可变)。非交易日返回 None。"""
    key = f"emotion:{date}"
    if not refresh:
        data, _ = cache.load(key)
        if data:
            return data
    zt, q1 = _pool("getTopicZTPool", "fbt:asc", date)
    if q1 and q1 != date:
        return None  # 东财回退返回了别的交易日(请求日为非交易日),拒收防污染
    zb, _ = _pool("getTopicZBPool", "fbt:asc", date)
    dtp, _ = _pool("getTopicDTPool", "fund:asc", date)
    yzt, _ = _pool("getYesterdayZTPool", "zs:desc", date)
    if not zt and not zb and not yzt:
        return None  # 非交易日/东财尚未生成当日数据
    ladder: dict[int, int] = {}
    for s in zt:
        ladder[s["lbc"]] = ladder.get(s["lbc"], 0) + 1
    zt_n, zb_n, dt_n = len(zt), len(zb), len(dtp)
    break_rate = round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0.0
    max_height = max((s["lbc"] for s in zt), default=0)
    promotion = premium = None
    if yzt:
        again = sum(1 for s in yzt if s["zdp"] >= _limit_pct(str(s["c"]).zfill(6)))
        promotion = round(again / len(yzt) * 100, 1)
        premium = round(sum(s["zdp"] for s in yzt) / len(yzt), 2)
    temp = _temperature(zt_n, dt_n, break_rate, max_height, promotion, premium)
    name, tag, note = _phase(temp)
    snap = {"date": date, "zt_count": zt_n, "zb_count": zb_n, "dt_count": dt_n,
            "break_rate": break_rate, "max_height": max_height,
            "ladder": {str(k): v for k, v in sorted(ladder.items())},
            "promotion_rate": promotion, "yzt_premium": premium, "yzt_count": len(yzt),
            "temperature": temp, "phase": name, "regime_tag": tag, "note": note}
    prev = latest_before(date)
    if prev:
        snap["temp_delta"] = temp - prev["temperature"]
        snap["prev_phase"] = prev["phase"]
        if snap["temp_delta"] <= -15:
            snap["turning"] = "转冷(温度骤降,警惕退潮首日)"
        elif snap["temp_delta"] >= 15:
            snap["turning"] = "转暖(温度跃升,关注启动确认)"
    cache.save(key, snap)
    return snap


def latest_before(date: str, lookback: int = 10) -> dict | None:
    """取 date 之前最近一条已落库快照(算温度变化/阶段切换)。"""
    d = dt.datetime.strptime(date, "%Y%m%d").date()
    for i in range(1, lookback + 1):
        data, _ = cache.load(f"emotion:{(d - dt.timedelta(days=i)):%Y%m%d}")
        if data:
            return data
    return None


def latest_snapshot(refresh: bool = False) -> dict | None:
    """最近交易日快照:从今天起回退找(周末/节假日自动跳过),供日报接入。"""
    d = dt.date.today()
    for _ in range(8):
        try:
            snap = snapshot(f"{d:%Y%m%d}", refresh=refresh)
        except Exception as e:  # noqa: BLE001
            print(f"    [情绪层缺失] {d} —— {e}")
            return None
        if snap:
            return snap
        d -= dt.timedelta(days=1)
    return None


def history(n: int = 20) -> list[dict]:
    """最近 n 个自然日内已落库的快照(升序)。"""
    out = []
    d = dt.date.today()
    for i in range(n):
        data, _ = cache.load(f"emotion:{(d - dt.timedelta(days=i)):%Y%m%d}")
        if data:
            out.append(data)
    return list(reversed(out))


def backfill(days: int) -> int:
    """回填近 N 个自然日(跳过已落库/非交易日),限速防封。返回新增条数。"""
    added = 0
    today = dt.date.today()
    # 从最早到最晚回填,保证 temp_delta/prev_phase 逐日可算
    for i in range(days, -1, -1):
        d = today - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        key = f"emotion:{d:%Y%m%d}"
        if cache.load(key)[0]:
            continue
        try:
            snap = snapshot(f"{d:%Y%m%d}")
        except Exception as e:  # noqa: BLE001
            print(f"  {d} 失败:{e}")
            continue
        if snap:
            added += 1
            print(f"  {d} 温度{snap['temperature']} {snap['phase']} "
                  f"涨停{snap['zt_count']} 炸板率{snap['break_rate']}% "
                  f"高度{snap['max_height']} 溢价{snap['yzt_premium']}%")
        time.sleep(1.0 + random.random() * 0.5)
    return added


def _pools_for_heat(date: str) -> dict:
    """个股情绪画像用的三池索引(涨停/炸板/昨涨停,按代码),当日缓存。"""
    key = f"emotion_pools:{date}"
    data, _ = cache.load(key)
    if data:
        return data
    zt, q1 = _pool("getTopicZTPool", "fbt:asc", date)
    if q1 and q1 != date:
        return {}
    zb, _ = _pool("getTopicZBPool", "fbt:asc", date)
    yzt, _ = _pool("getYesterdayZTPool", "zs:desc", date)
    ind_zt: dict[str, int] = {}
    for s in zt:
        hy = s.get("hybk") or ""
        if hy:
            ind_zt[hy] = ind_zt.get(hy, 0) + 1
    idx = {"date": date,
           "zt": {str(s["c"]).zfill(6): {"lbc": s["lbc"],
                  "stat": f'{(s.get("zttj") or {}).get("days","?")}天{(s.get("zttj") or {}).get("ct","?")}板',
                  "industry": s.get("hybk", "")} for s in zt},
           "zb": {str(s["c"]).zfill(6): {"break_times": s.get("zbc", 0)} for s in zb},
           "yzt": {str(s["c"]).zfill(6): {"pct": round(s["zdp"], 2)} for s in yzt},
           "industry_zt": ind_zt}
    cache.save(key, idx)
    return idx


def stock_heat(code: str, bars: list[dict] | None = None, industry: str = "") -> dict | None:
    """单只个股的情绪画像(影子指标,与大盘温度计同源):
    今日涨停/炸板/昨日涨停状态、连板数、近60日涨停基因、所属行业今日涨停家数。
    bars 传日K([{c,...}]含涨跌可算涨停基因);industry 缺省时从涨停池回填。"""
    d = dt.date.today()
    pools = {}
    for _ in range(8):  # 回退找最近交易日
        pools = _pools_for_heat(f"{d:%Y%m%d}")
        if pools:
            break
        d -= dt.timedelta(days=1)
    if not pools:
        return None
    code = str(code).zfill(6)
    zt_hit = pools["zt"].get(code)
    zb_hit = pools["zb"].get(code)
    yzt_hit = pools["yzt"].get(code)
    lim = _limit_pct(code)
    zt60 = 0
    if bars:
        closes = [b["c"] for b in bars[-61:]]
        for i in range(1, len(closes)):
            if closes[i-1] > 0 and (closes[i] / closes[i-1] - 1) * 100 >= lim:
                zt60 += 1
    industry = industry or (zt_hit or {}).get("industry", "")
    ind_zt_n = pools["industry_zt"].get(industry, 0) if industry else None
    tags = []
    if zt_hit:
        tags.append(f"今日涨停({zt_hit['stat']})" if zt_hit["lbc"] <= 1
                    else f"今日{zt_hit['lbc']}连板({zt_hit['stat']})")
    if zb_hit:
        tags.append(f"今日炸板×{zb_hit['break_times']}")
    if yzt_hit:
        tags.append(f"昨日涨停今日{yzt_hit['pct']:+}%")
    if zt60 >= 3:
        tags.append(f"涨停基因×{zt60}/60日")
    if ind_zt_n:
        tags.append(f"同行业今日涨停{ind_zt_n}家")
    # 情绪属性分级:龙头(连板/高涨停基因) > 活跃(涨停/题材共振) > 平静
    if zt_hit and zt_hit["lbc"] >= 2:
        grade = "情绪龙头"
    elif zt_hit or (zt60 >= 5) or (ind_zt_n or 0) >= 5:
        grade = "情绪活跃"
    elif zb_hit or yzt_hit or zt60 >= 3:
        grade = "有情绪属性"
    else:
        grade = "情绪平静"
    return {"date": pools["date"], "grade": grade, "tags": tags, "zt60": zt60,
            "limit_up_today": bool(zt_hit), "lbc": (zt_hit or {}).get("lbc", 0),
            "broke_today": bool(zb_hit), "yzt_pct": (yzt_hit or {}).get("pct"),
            "industry_zt_count": ind_zt_n}


def brief(snap: dict) -> str:
    """一行文字摘要(日报/侧车展示用)。"""
    parts = [f"情绪温度 {snap['temperature']}/100({snap['phase']})",
             f"涨停{snap['zt_count']}家·炸板率{snap['break_rate']}%",
             f"最高{snap['max_height']}连板"]
    if snap.get("promotion_rate") is not None:
        parts.append(f"晋级率{snap['promotion_rate']}%")
    if snap.get("yzt_premium") is not None:
        parts.append(f"昨停溢价{snap['yzt_premium']:+}%")
    if snap.get("turning"):
        parts.append(snap["turning"])
    return " | ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="情绪周期温度计")
    ap.add_argument("--date", help="YYYYMMDD,默认最近交易日")
    ap.add_argument("--backfill", type=int, metavar="N", help="回填近N个自然日")
    ap.add_argument("--history", type=int, metavar="N", help="打印最近N日已落库快照")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存强制重取")
    args = ap.parse_args()
    if args.backfill:
        n = backfill(args.backfill)
        print(f"回填完成,新增 {n} 条"); return
    if args.history:
        for s in history(args.history):
            print(f"{s['date']} 温度{s['temperature']:>3} {s['phase']} | "
                  f"涨停{s['zt_count']:>3} 炸板率{s['break_rate']:>4}% "
                  f"高度{s['max_height']} 晋级率{s.get('promotion_rate')}% "
                  f"溢价{s.get('yzt_premium')}%")
        return
    snap = snapshot(args.date, refresh=args.refresh) if args.date \
        else latest_snapshot(refresh=args.refresh)
    if not snap:
        print("无数据(非交易日或接口失败)"); return
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    print("\n" + brief(snap))
    print(f"操作标签:{snap['regime_tag']} —— {snap['note']}")


if __name__ == "__main__":
    main()
