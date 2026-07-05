# -*- coding: utf-8 -*-
"""缠论 + 经典形态 检测引擎(拿来主义 + 本地实现)。

流程(缠论标准):包含处理 → 分型 → 笔 → 中枢;并在笔的极值序列上识别
支撑/压力、双顶(M头)/双底(W底)/头肩顶/头肩底 及其颈线。

产出均为「可视化友好」结构(带日期与价格),供图表叠加。
注:形态/中枢为启发式估算,辅助研判,非精确定义。
"""
from __future__ import annotations


def _merge_inclusion(bars: list[dict]) -> list[dict]:
    """包含关系处理:合并互相包含的相邻K,输出去包含后的 (d,h,l) 序列。"""
    if len(bars) < 2:
        return [{"d": b["d"], "h": b["h"], "l": b["l"]} for b in bars]
    merged = [{"d": bars[0]["d"], "h": bars[0]["h"], "l": bars[0]["l"]}]
    # 初始方向由前两根高点比较决定(修复:原写死向上,开头下跌会误判首合并)
    direction = 1 if bars[1]["h"] >= bars[0]["h"] else -1
    for b in bars[1:]:
        prev = merged[-1]
        contain = (b["h"] >= prev["h"] and b["l"] <= prev["l"]) or \
                  (b["h"] <= prev["h"] and b["l"] >= prev["l"])
        if contain:
            if direction >= 0:  # 上升:高高、低取高
                prev["h"] = max(prev["h"], b["h"])
                prev["l"] = max(prev["l"], b["l"])
            else:               # 下降:低低、高取低
                prev["h"] = min(prev["h"], b["h"])
                prev["l"] = min(prev["l"], b["l"])
            prev["d"] = b["d"]
        else:
            direction = 1 if b["h"] > prev["h"] else -1
            merged.append({"d": b["d"], "h": b["h"], "l": b["l"]})
    return merged


def _fractals(m: list[dict]) -> list[dict]:
    """分型:去包含序列上的三根K高低点。返回 [{i,d,price,type}]。"""
    out = []
    for i in range(1, len(m) - 1):
        if m[i]["h"] > m[i - 1]["h"] and m[i]["h"] > m[i + 1]["h"]:
            out.append({"i": i, "d": m[i]["d"], "price": round(m[i]["h"], 2), "type": "top"})
        elif m[i]["l"] < m[i - 1]["l"] and m[i]["l"] < m[i + 1]["l"]:
            out.append({"i": i, "d": m[i]["d"], "price": round(m[i]["l"], 2), "type": "bottom"})
    return out


def _bi(fractals: list[dict], min_gap: int = 3) -> list[dict]:
    """笔:交替连接顶底分型,同类取更极端,间隔≥min_gap(去包含K数)。"""
    if not fractals:
        return []
    seq = [fractals[0]]
    for f in fractals[1:]:
        last = seq[-1]
        if f["type"] == last["type"]:
            # 同类分型,保留更极端者
            if (f["type"] == "top" and f["price"] > last["price"]) or \
               (f["type"] == "bottom" and f["price"] < last["price"]):
                seq[-1] = f
        else:
            # 异类分型:间隔足够才成新笔;间隔不足则跳过(不改变已确认端点)
            if f["i"] - last["i"] >= min_gap:
                seq.append(f)
    return seq


def _segments(bi: list[dict]) -> list[dict]:
    """线段(工程近似):在笔端点序列上取"高阶分型"——某顶高于相邻两个顶、
    某底低于相邻两个底(同类端点相隔2个)即为线段转折点,过滤细碎笔。
    严格缠论线段用特征序列+缺口判定;此近似更稳、够用于中枢重构(chan-rules §4)。
    返回 [{d_start,d_end,dir,high,low}](方向 up/down,high/low 为两端极值)。"""
    if len(bi) < 5:
        return []
    majors = [bi[0]]
    for k in range(1, len(bi) - 1):
        p = bi[k]
        prev_same = bi[k - 2]["price"] if k - 2 >= 0 else None
        next_same = bi[k + 2]["price"] if k + 2 < len(bi) else None
        if p["type"] == "top":
            if (prev_same is None or p["price"] >= prev_same) and (next_same is None or p["price"] >= next_same):
                majors.append(p)
        else:
            if (prev_same is None or p["price"] <= prev_same) and (next_same is None or p["price"] <= next_same):
                majors.append(p)
    if majors[-1] is not bi[-1]:
        majors.append(bi[-1])
    # 折叠连续同类转折点(取更极端者),保证顶底交替
    collapsed = [majors[0]]
    for p in majors[1:]:
        if p["type"] == collapsed[-1]["type"]:
            if (p["type"] == "top" and p["price"] >= collapsed[-1]["price"]) or \
               (p["type"] == "bottom" and p["price"] <= collapsed[-1]["price"]):
                collapsed[-1] = p
        else:
            collapsed.append(p)
    segs = []
    for k in range(1, len(collapsed)):
        a, b = collapsed[k - 1], collapsed[k]
        segs.append({"d_start": a["d"], "d_end": b["d"],
                     "dir": "up" if b["type"] == "top" else "down",
                     "high": round(max(a["price"], b["price"]), 2),
                     "low": round(min(a["price"], b["price"]), 2)})
    return segs


def _zhongshu_from_segments(segs: list[dict]) -> list[dict]:
    """基于线段建中枢(缠论标准口径,chan-rules §5):连续三段重叠区。
    ZG=三段 high 的最小值,ZD=三段 low 的最大值,ZG>ZD 成中枢;向后延伸并输出方向。"""
    if len(segs) < 3:
        return []
    out = []
    i = 0
    while i + 2 < len(segs):
        tri = segs[i:i + 3]
        zg = min(s["high"] for s in tri)
        zd = max(s["low"] for s in tri)
        band = zg - zd
        max_range = max(s["high"] - s["low"] for s in tri)
        # 护栏:中枢带宽须与线段规模相当(真震荡);带宽远小于趋势腿=退化假中枢,跳过
        if zg > zd and band >= 0.4 * max_range:
            z = {"d_start": tri[0]["d_start"], "d_end": tri[-1]["d_end"],
                 "zd": round(zd, 2), "zg": round(zg, 2),
                 "dir_in": segs[i]["dir"], "count": 3}
            j = i + 3  # 向后延伸:后续段仍与 [zd,zg] 重叠则并入
            while j < len(segs) and segs[j]["low"] <= zg and segs[j]["high"] >= zd:
                z["d_end"] = segs[j]["d_end"]
                z["count"] += 1
                j += 1
            z["dir_out"] = segs[j]["dir"] if j < len(segs) else z["dir_in"]
            out.append(z)
            i = j
        else:
            i += 1
    return out


def _zhongshu(bi: list[dict]) -> list[dict]:
    """中枢(笔的降级方案):线段不足时用连续≥3笔的重叠段。返回 [{d_start,d_end,zd,zg}]。"""
    if len(bi) < 4:
        return []
    out = []
    i = 0
    while i + 3 < len(bi):
        # 三段笔构成的区间:相邻极值对形成的高低
        seg = bi[i:i + 4]
        highs = [p["price"] for p in seg]
        lows = highs
        # 用相邻笔的重叠:zg=较低的高点,zd=较高的低点
        tops = [p["price"] for p in seg if p["type"] == "top"]
        bots = [p["price"] for p in seg if p["type"] == "bottom"]
        if tops and bots:
            zg = min(tops)
            zd = max(bots)
            # 有重叠且带宽≥1.5%(过滤仅一线重叠的退化中枢)才成中枢
            if zg > zd and (zg - zd) / zd >= 0.015:
                out.append({"d_start": seg[0]["d"], "d_end": seg[-1]["d"],
                            "zd": round(zd, 2), "zg": round(zg, 2)})
                i += 3
                continue
        i += 1
    # 合并相邻重叠中枢
    merged = []
    for z in out:
        if merged and z["zd"] <= merged[-1]["zg"] and z["zg"] >= merged[-1]["zd"]:
            merged[-1]["d_end"] = z["d_end"]
            merged[-1]["zd"] = max(merged[-1]["zd"], z["zd"])
            merged[-1]["zg"] = min(merged[-1]["zg"], z["zg"])
        else:
            merged.append(dict(z))
    return merged


def _patterns(bi: list[dict], last_close: float, tol: float = 0.04) -> list[dict]:
    """在笔极值序列上识别双顶/双底/头肩顶/头肩底,给出颈线与状态。"""
    pats = []
    tops = [p for p in bi if p["type"] == "top"]
    bots = [p for p in bi if p["type"] == "bottom"]

    def close_(a, b):
        return abs(a - b) / max(a, b) <= tol

    # 双顶 M头:最近两个相近高点,中间有低点(颈线)
    if len(tops) >= 2 and len(bots) >= 1:
        t1, t2 = tops[-2], tops[-1]
        mids = [b for b in bots if t1["d"] < b["d"] < t2["d"]]
        if close_(t1["price"], t2["price"]) and mids:
            neck = min(m["price"] for m in mids)
            pats.append({"type": "双顶(M头)", "kind": "bear", "neckline": round(neck, 2),
                         "points": [t1, mids[0], t2], "peak": round(max(t1["price"], t2["price"]), 2),
                         "status": "已跌破颈线(看跌)" if last_close < neck else "颈线未破,警惕"})
    # 双底 W底
    if len(bots) >= 2 and len(tops) >= 1:
        b1, b2 = bots[-2], bots[-1]
        mids = [t for t in tops if b1["d"] < t["d"] < b2["d"]]
        if close_(b1["price"], b2["price"]) and mids:
            neck = max(m["price"] for m in mids)
            pats.append({"type": "双底(W底)", "kind": "bull", "neckline": round(neck, 2),
                         "points": [b1, mids[0], b2], "trough": round(min(b1["price"], b2["price"]), 2),
                         "status": "已突破颈线(看涨)" if last_close > neck else "颈线未破,等待突破"})
    # 头肩顶:三高,中间最高,两肩相近
    if len(tops) >= 3 and len(bots) >= 2:
        ls, head, rs = tops[-3], tops[-2], tops[-1]
        if head["price"] > ls["price"] and head["price"] > rs["price"] and close_(ls["price"], rs["price"]):
            necks = [b["price"] for b in bots if ls["d"] < b["d"] < rs["d"]]
            if necks:
                neck = min(necks)
                pats.append({"type": "头肩顶", "kind": "bear", "neckline": round(neck, 2),
                             "points": [ls, head, rs], "peak": round(head["price"], 2),
                             "status": "已跌破颈线(看跌)" if last_close < neck else "颈线未破,警惕"})
    # 头肩底
    if len(bots) >= 3 and len(tops) >= 2:
        ls, head, rs = bots[-3], bots[-2], bots[-1]
        if head["price"] < ls["price"] and head["price"] < rs["price"] and close_(ls["price"], rs["price"]):
            necks = [t["price"] for t in tops if ls["d"] < t["d"] < rs["d"]]
            if necks:
                neck = max(necks)
                pats.append({"type": "头肩底", "kind": "bull", "neckline": round(neck, 2),
                             "points": [ls, head, rs], "trough": round(head["price"], 2),
                             "status": "已突破颈线(看涨)" if last_close > neck else "颈线未破,等待突破"})
    return pats


def _support_resistance(bi: list[dict], last_close: float) -> dict:
    """从笔极值取最近的下方支撑与上方压力(聚类相近价位)。"""
    tops = sorted({round(p["price"], 2) for p in bi if p["type"] == "top"})
    bots = sorted({round(p["price"], 2) for p in bi if p["type"] == "bottom"})
    supports = [x for x in bots + tops if x < last_close]
    resists = [x for x in tops + bots if x > last_close]
    return {"support": max(supports) if supports else None,
            "resistance": min(resists) if resists else None}


def _macd(bars: list[dict], fast=12, slow=26, sig=9) -> dict:
    """MACD(DIF/DEA/柱)。返回按日期索引的 dict,供背驰判断。"""
    close = [b["c"] for b in bars]
    ka, ks, kg = 2 / (fast + 1), 2 / (slow + 1), 2 / (sig + 1)
    ef = es = close[0]
    dif, dea = [], []
    d = 0.0
    for c in close:
        ef = c * ka + ef * (1 - ka)
        es = c * ks + es * (1 - ks)
        v = ef - es
        dif.append(v)
        d = v * kg + d * (1 - kg)
        dea.append(d)
    by_d = {}
    for i, b in enumerate(bars):
        by_d[b["d"]] = {"dif": round(dif[i], 3), "dea": round(dea[i], 3),
                        "bar": round(2 * (dif[i] - dea[i]), 3)}
    return by_d


def _third_bs(bars: list[dict], zhongshu: list[dict]) -> dict | None:
    """第三类买卖点:中枢确认后,向上离开中枢、回抽不跌回中枢上沿ZG → 3B(镜像 3S)。
    引擎已有中枢,只需看离开方向 + 回抽极值是否守住 ZG/ZD(chan-rules §6 最易落地项)。"""
    if not zhongshu:
        return None
    z = zhongshu[-1]
    zg, zd = z["zg"], z["zd"]
    after = [b for b in bars if b["d"] > z["d_end"]]
    if len(after) < 2:
        return None
    # 向上离开:出现高点破 ZG,其后回调最低点仍 > ZG
    up_break = next((i for i, b in enumerate(after) if b["h"] > zg), None)
    if up_break is not None and up_break < len(after) - 1:
        rest = after[up_break + 1:]
        pull_low = min(b["l"] for b in rest)
        if pull_low > zg:
            d = min(rest, key=lambda b: b["l"])["d"]
            return {"type": "3B", "price": round(pull_low, 2), "d": d, "zg": zg, "zd": zd}
    # 向下离开:低点破 ZD,其后反抽最高点仍 < ZD
    dn_break = next((i for i, b in enumerate(after) if b["l"] < zd), None)
    if dn_break is not None and dn_break < len(after) - 1:
        rest = after[dn_break + 1:]
        reb_high = max(b["h"] for b in rest)
        if reb_high < zd:
            d = max(rest, key=lambda b: b["h"])["d"]
            return {"type": "3S", "price": round(reb_high, 2), "d": d, "zg": zg, "zd": zd}
    return None


def _divergence(bi: list[dict], macd: dict) -> dict | None:
    """MACD 背驰(辅助):相邻同类分型,价创新低/高但 DIF 未创新低/高 → 底/顶背离。
    对应第一类买卖点候选(chan-rules §7,MACD 辅助口径)。"""
    bottoms = [p for p in bi if p["type"] == "bottom"]
    tops = [p for p in bi if p["type"] == "top"]
    if bi and bi[-1]["type"] == "bottom" and len(bottoms) >= 2:
        p0, p1 = bottoms[-2], bottoms[-1]
        m0, m1 = macd.get(p0["d"]), macd.get(p1["d"])
        if m0 and m1 and p1["price"] < p0["price"] and m1["dif"] > m0["dif"]:
            return {"type": "底背离", "bs": "1B", "d": p1["d"], "price": p1["price"],
                    "note": "价创新低但MACD-DIF未创新低,动能衰竭(第一类买点候选)"}
    if bi and bi[-1]["type"] == "top" and len(tops) >= 2:
        p0, p1 = tops[-2], tops[-1]
        m0, m1 = macd.get(p0["d"]), macd.get(p1["d"])
        if m0 and m1 and p1["price"] > p0["price"] and m1["dif"] < m0["dif"]:
            return {"type": "顶背离", "bs": "1S", "d": p1["d"], "price": p1["price"],
                    "note": "价创新高但MACD-DIF未创新高,动能衰竭(第一类卖点候选)"}
    return None


def analyze(bars: list[dict]) -> dict:
    """总入口。bars:[{d,o,c,l,h,v}]。返回缠论与形态结构(可视化友好)。"""
    if len(bars) < 8:
        return {"fractals": [], "bi": [], "zhongshu": [], "patterns": [], "sr": {},
                "third_bs": None, "divergence": None}
    merged = _merge_inclusion(bars)
    fr = _fractals(merged)
    bi = _bi(fr)
    segs = _segments(bi)
    # 中枢优先建立在线段上(更稳);线段中枢为空(纯趋势/无有效震荡)则降级用笔
    zs_seg = _zhongshu_from_segments(segs)
    zs = zs_seg if zs_seg else _zhongshu(bi)
    zs_basis = "线段" if zs_seg else "笔"
    last_close = bars[-1]["c"]
    pats = _patterns(bi, last_close)
    sr = _support_resistance(bi, last_close)
    macd = _macd(bars)
    return {
        "fractals": [{"d": f["d"], "price": f["price"], "type": f["type"]} for f in fr],
        "bi": [{"d": p["d"], "price": p["price"], "type": p["type"]} for p in bi],
        "segments": segs, "zhongshu": zs, "zs_basis": zs_basis,
        "patterns": pats, "sr": sr,
        "third_bs": _third_bs(bars, zs), "divergence": _divergence(bi, macd),
    }


def summary(res: dict) -> str:
    """把缠论/形态结果转成研判文字。"""
    parts = []
    segs = res.get("segments") or []
    if segs:
        parts.append(f"线段结构 {len(segs)} 段(当前{'向上' if segs[-1]['dir']=='up' else '向下'})")
    zs = res.get("zhongshu") or []
    if zs:
        z = zs[-1]
        parts.append(f"最近中枢 {z['zd']}~{z['zg']}(基于{res.get('zs_basis','笔')},突破/跌破定方向)")
    bi = res.get("bi") or []
    if bi:
        parts.append("当前笔向上,短线走强" if bi[-1]["type"] == "bottom" else "当前笔向下,短线走弱")
    tbs = res.get("third_bs")
    if tbs:
        act = "回抽不跌回中枢,趋势延续买点" if tbs["type"] == "3B" else "反抽不涨回中枢,趋势延续卖点"
        parts.append(f"⭐第三类{'买' if tbs['type']=='3B' else '卖'}点(3{tbs['type'][-1]}):{tbs['price']},{act}")
    dv = res.get("divergence")
    if dv:
        parts.append(f"⭐{dv['type']}({dv['bs']}候选):{dv['note']}")
    for p in res.get("patterns") or []:
        parts.append(f"{p['type']}:颈线 {p['neckline']},{p['status']}")
    sr = res.get("sr") or {}
    if sr.get("support") or sr.get("resistance"):
        parts.append(f"缠论关键位:支撑 {sr.get('support','—')} / 压力 {sr.get('resistance','—')}")
    return ";".join(parts) if parts else "结构数据不足,以趋势与均线为主。"
