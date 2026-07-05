# -*- coding: utf-8 -*-
"""DangInvest 数据源客户端 —— 东财 push2 clist 的完整备源(见调研结论)。

数据源 https://dang-invest.com(东财+同花顺数据再加工),免费无 key,与东财 push2
是完全不同的服务器,故不受东财 clist 限流影响。关键价值:板块排名(summary)与
板块成分股(detail)用同一套 groupKey 串联,内部命名自洽 —— 一举补上"成分股仅东财
一家、无法降级"的单点缺口。

返回值统一对齐东财 schema(板块名称/涨跌幅/领涨股票、代码/名称/成交额),下游零改动。
"""
import requests
import pandas as pd

_BASE = "https://dang-invest.com/api/market"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 板块维度:industry=申万大类(~110,与东财行业板块粒度相近,作默认)
#          ths_industry=同花顺细分(~588) / ths_concept=同花顺概念(~396)
MODE = {"industry": "industry", "sub": "ths_industry", "concept": "ths_concept"}


def _get(path: str, params: dict) -> dict:
    r = requests.get(f"{_BASE}/{path}", params=params, headers=_HEADERS,
                     timeout=20, proxies={"http": None, "https": None})
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"DangInvest 返回错误: {payload['error']}")
    return payload


def _norm_code(code: str) -> str:
    """'688017.SH' / '002747.SZ' → '688017'(对齐东财 6 位代码)。"""
    return str(code).split(".")[0].zfill(6)


def board_summary(mode: str = "industry", limit: int = 300) -> pd.DataFrame:
    """板块排名(按涨跌幅降序)。列对齐东财:板块名称/涨跌幅/板块代码/上涨家数...
    额外保留 _key(groupKey,供 board_detail 串联)。"""
    payload = _get("boards/summary", {"mode": MODE.get(mode, mode),
                                      "limit": limit, "sort": "change_desc"})
    items = (payload.get("data") or {}).get("items") or []
    rows = [{
        "板块名称": it.get("groupLabel", ""),
        "涨跌幅": round(float(it.get("changePct") or 0), 2),
        "板块代码": it.get("groupKey", ""),
        "成分数": it.get("count", 0),
        "总市值": float(it.get("totalMarketCapYuan") or 0),
        "领涨股票": "—",  # summary 不含领涨股,置空;需要时看东财主源
        "_key": it.get("groupKey", ""),
    } for it in items]
    return pd.DataFrame(rows)


def board_cons(group_key: str, mode: str = "industry", items_limit: int = 300) -> pd.DataFrame:
    """板块成分股(按成交额降序)。列对齐东财:代码/名称/最新价/涨跌幅/成交额。"""
    payload = _get("boards/detail", {"mode": MODE.get(mode, mode), "groupKey": group_key,
                                     "sort": "turnover_desc", "items_limit": items_limit})
    items = ((payload.get("data") or {}).get("items")) or []
    rows = [{
        "代码": _norm_code(it.get("code", "")),
        "名称": it.get("name", ""),
        "最新价": it.get("price"),
        "涨跌幅": it.get("changePct"),
        "成交额": float(it.get("turnoverYuan") or 0),
    } for it in items]
    return pd.DataFrame(rows)
