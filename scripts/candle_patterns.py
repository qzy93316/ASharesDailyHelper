# -*- coding: utf-8 -*-
"""K线组合形态检测器 —— 判定逻辑据知识库 knowledge/kb/candlestick-patterns.json
(源:70种经典K线组合PPT)实现。元数据(中文名/多空/类别/可靠度)从 JSON 读取,
判定用 OHLC 编码;扫描近端K线,输出命中的形态,供图表标注与研判。

只实现"用日K OHLC 可稳定判定"的一批(单根/双根/三根经典形),持续可扩充。
"""
import json
from pathlib import Path

_KB_PATH = Path(__file__).parent.parent / "knowledge" / "kb" / "candlestick-patterns.json"
try:
    _KB = {e["id"]: e for e in json.loads(_KB_PATH.read_text(encoding="utf-8"))}
except Exception:  # noqa: BLE001
    _KB = {}


def _b(k):  # 实体
    return abs(k["c"] - k["o"])
def _rng(k):
    return max(k["h"] - k["l"], 1e-9)
def _up(k):  # 上影
    return k["h"] - max(k["o"], k["c"])
def _lo(k):  # 下影
    return min(k["o"], k["c"]) - k["l"]
def _yang(k):
    return k["c"] >= k["o"]
def _mid(k):
    return (k["o"] + k["c"]) / 2


def _downtrend(bars, i, n=4):
    return i >= n and bars[i - n]["c"] > bars[i - 1]["c"]
def _uptrend(bars, i, n=4):
    return i >= n and bars[i - n]["c"] < bars[i - 1]["c"]


# 每个检测器返回 True/False;签名 (bars, i) 判定"以 i 为最后一根"的形态
def _hammer(b, i):
    k = b[i]; return _downtrend(b, i) and _b(k) < _rng(k) * 0.4 and _lo(k) >= 2 * _b(k) and _up(k) <= _b(k) * 0.5
def _inverted_hammer(b, i):
    k = b[i]; return _downtrend(b, i) and _b(k) < _rng(k) * 0.4 and _up(k) >= 2 * _b(k) and _lo(k) <= _b(k) * 0.5
def _shooting_star(b, i):
    k = b[i]; return _uptrend(b, i) and _b(k) < _rng(k) * 0.4 and _up(k) >= 2 * _b(k) and _lo(k) <= _b(k) * 0.5
def _hanging_man(b, i):
    k = b[i]; return _uptrend(b, i) and _b(k) < _rng(k) * 0.4 and _lo(k) >= 2 * _b(k) and _up(k) <= _b(k) * 0.5
def _doji(b, i):
    k = b[i]; return _b(k) <= _rng(k) * 0.1
def _dragonfly_doji(b, i):
    k = b[i]; return _doji(b, i) and _lo(k) >= _rng(k) * 0.6 and _up(k) <= _rng(k) * 0.1
def _gravestone_doji(b, i):
    k = b[i]; return _doji(b, i) and _up(k) >= _rng(k) * 0.6 and _lo(k) <= _rng(k) * 0.1
def _one_word(b, i):
    k = b[i]; return _rng(k) <= max(k["c"], 1) * 0.005
def _bullish_engulfing(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return not _yang(p) and _yang(k) and k["o"] < p["c"] and k["c"] > p["o"]
def _bearish_engulfing(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _yang(p) and not _yang(k) and k["o"] > p["c"] and k["c"] < p["o"]
def _harami(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _b(p) > _b(k) * 2 and max(k["o"], k["c"]) < max(p["o"], p["c"]) and min(k["o"], k["c"]) > min(p["o"], p["c"])
def _dark_cloud_cover(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _uptrend(b, i) and _yang(p) and not _yang(k) and k["o"] > p["h"] and _mid(p) > k["c"] > p["o"]
def _piercing_line(b, i):
    if i < 1: return False
    p, k = b[i - 1], b[i]
    return _downtrend(b, i) and not _yang(p) and _yang(k) and k["o"] < p["l"] and p["o"] > k["c"] > _mid(p)
def _three_white_soldiers(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return all(_yang(t) for t in (x, y, z)) and x["c"] < y["c"] < z["c"] and y["o"] > x["o"] and z["o"] > y["o"]
def _three_black_crows(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return all(not _yang(t) for t in (x, y, z)) and x["c"] > y["c"] > z["c"] and y["o"] < x["o"] and z["o"] < y["o"]
def _morning_star(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _downtrend(b, i - 1) and not _yang(x) and _b(y) < _b(x) * 0.5 and max(y["o"], y["c"]) < x["c"] and _yang(z) and z["c"] > _mid(x)
def _evening_star(b, i):
    if i < 2: return False
    x, y, z = b[i - 2], b[i - 1], b[i]
    return _uptrend(b, i - 1) and _yang(x) and _b(y) < _b(x) * 0.5 and min(y["o"], y["c"]) > x["c"] and not _yang(z) and z["c"] < _mid(x)


_DETECTORS = {
    "hammer": _hammer, "inverted_hammer": _inverted_hammer, "shooting_star": _shooting_star,
    "hanging_man": _hanging_man, "doji": _doji, "dragonfly_doji": _dragonfly_doji,
    "gravestone_doji": _gravestone_doji, "one_word_line": _one_word,
    "bullish_engulfing": _bullish_engulfing, "bearish_engulfing": _bearish_engulfing,
    "harami": _harami, "dark_cloud_cover": _dark_cloud_cover, "piercing_line": _piercing_line,
    "three_white_soldiers": _three_white_soldiers, "three_black_crows": _three_black_crows,
    "morning_star": _morning_star, "evening_star": _evening_star,
}

# JSON 里的 id 命名可能不同,做别名兜底(检测器名 → KB 里的候选 id)
_KB_ALIAS = {
    "bullish_engulfing": ["bullish_engulfing", "engulfing", "bullish_engulfing_rising_sun"],
    "bearish_engulfing": ["bearish_engulfing", "engulfing"],
    "harami": ["harami", "pregnant_harami"],
    "piercing_line": ["piercing_line", "piercing"],
    "morning_star": ["morning_star", "morning_doji_star"],
}

_FALLBACK_NAME = {
    "hammer": ("锤头线", "bull"), "inverted_hammer": ("倒锤头线", "bull"),
    "shooting_star": ("射击之星", "bear"), "hanging_man": ("吊颈线", "bear"),
    "doji": ("十字线", "neutral"), "dragonfly_doji": ("T字线", "bull"),
    "gravestone_doji": ("倒T字线", "bear"), "one_word_line": ("一字线", "neutral"),
    "bullish_engulfing": ("看涨穿头破脚", "bull"), "bearish_engulfing": ("看跌穿头破脚", "bear"),
    "harami": ("身怀六甲", "neutral"), "dark_cloud_cover": ("乌云盖顶", "bear"),
    "piercing_line": ("曙光初现", "bull"), "three_white_soldiers": ("红三兵", "bull"),
    "three_black_crows": ("三只乌鸦", "bear"), "morning_star": ("早晨之星", "bull"),
    "evening_star": ("黄昏之星", "bear"),
}


def _meta(det_id: str) -> dict:
    # 方向以检测器自带口径为准(KB 的通用 engulfing 等标记为中性,方向性形态需覆盖)
    nm_fb, bias = _FALLBACK_NAME.get(det_id, (det_id, "neutral"))
    for kid in _KB_ALIAS.get(det_id, [det_id]):
        if kid in _KB:
            e = _KB[kid]
            return {"name_cn": e.get("name_cn") or nm_fb, "bias": bias,
                    "category": e.get("category", ""), "reliability": e.get("reliability", "中")}
    return {"name_cn": nm_fb, "bias": bias, "category": "", "reliability": "中"}


def detect(bars: list[dict], lookback: int = 20) -> list[dict]:
    """扫描最近 lookback 根K线,返回命中的形态(按日期)。"""
    out = []
    n = len(bars)
    if n < 5:
        return out
    for i in range(max(4, n - lookback), n):
        for det_id, fn in _DETECTORS.items():
            try:
                if fn(bars, i):
                    m = _meta(det_id)
                    out.append({"id": det_id, "name_cn": m["name_cn"], "bias": m["bias"],
                                "reliability": m["reliability"], "d": bars[i]["d"],
                                "price": bars[i]["h"] if m["bias"] == "bear" else bars[i]["l"]})
            except Exception:  # noqa: BLE001
                continue
    return out


def latest_comment(hits: list[dict]) -> str:
    """最近命中的形态转研判文字(取最后3个)。"""
    if not hits:
        return "近端未识别到明显的经典K线组合形态。"
    tag = {"bull": "看涨", "bear": "看跌", "neutral": "中性"}
    recent = hits[-3:]
    return "近端K线形态:" + ";".join(
        f"{h['d']} {h['name_cn']}({tag.get(h['bias'],'')},可靠度{h['reliability']})" for h in recent)
