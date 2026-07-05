# -*- coding: utf-8 -*-
"""数据获取层:AKShare 为主,统一加 重试/限速/双源降级/本地缓存/新鲜度校验。

韧性设计(见设计文档 §2 双源互备、§3.1、§8 数据风险):
  1. 当天已缓存 → 直接用缓存(省请求、避限流)
  2. 未缓存 → 实时拉取(东财主源 → 同花顺备源),成功后写缓存
  3. 实时失败 → 回退到最近一次缓存并标记陈旧,交给新鲜度门禁把关
"""
import os
import time
import random
import datetime as dt
from pathlib import Path

import yaml

# 数据源均为国内站点,直连即可;忽略系统/环境代理,避免本地代理故障拖垮接口
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# 东财 push2 行情集群会按 TLS 指纹拦截 python-requests/curl,
# 用 curl_cffi 伪装 Chrome 指纹替换 akshare 的分页请求函数
from curl_cffi import requests as _cf
import akshare.utils.request as _ak_request
import akshare.utils.func as _ak_func

_NO_PROXY = {"http": None, "https": None}


def _tweak_params(params):
    """针对东财 clist 接口的两处规避:
    1. fields 字段串按字段号重排 —— 绕开 WAF 对 akshare 固定串的签名拦截;
    2. pz(每页条数)拉大到 1000 —— 让分页塌缩成单请求。akshare 默认每页 100,
       行业板块/成分股会被放大成 5+ 个请求,而东财 clist 有很严的突发限制,
       请求数一多就被断连;单请求可大幅降低触发概率。"""
    if not params:
        return params
    out = dict(params)
    if "fields" in out:
        try:
            fields = sorted(out["fields"].split(","), key=lambda f: int(f.lstrip("f")))
            out["fields"] = ",".join(fields)
        except ValueError:
            pass
    if "pz" in out:
        try:
            if int(out["pz"]) < 1000:
                out["pz"] = "1000"
        except (ValueError, TypeError):
            pass
    return out


def _cf_request_with_retry(url, params=None, timeout=15, max_retries=2, **_):
    """伪装 Chrome 指纹的单请求重试。次数刻意压低——限流时靠上层缓存兜底,
    而非狂重试把封禁续得更长。"""
    last_err = None
    for attempt in range(max_retries):
        try:
            r = _cf.get(url, params=_tweak_params(params), timeout=timeout,
                        impersonate="chrome", proxies=_NO_PROXY)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1 + attempt)
    raise last_err


_ak_request.request_with_retry = _cf_request_with_retry
_ak_func.request_with_retry = _cf_request_with_retry

import akshare as ak
import pandas as pd

import cache
import danginvest

# --- 限速/重试配置:从 config.yaml 读取,改配置无需改代码 ---
_CFG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
_DATA = _CFG.get("data", {})
INTERVAL = float(_DATA.get("request_interval_sec", 1.0))
RETRIES = int(_DATA.get("max_retries", 2))


def _throttle() -> None:
    """带抖动的限速:避免固定节奏被识别为机器人,也错开突发。"""
    time.sleep(INTERVAL + random.uniform(0, INTERVAL * 0.5))


def _call(fn, *args, **kwargs):
    """统一重试 + 限速包装。次数用尽抛 RuntimeError(由上层决定降级还是用缓存)。"""
    last_err = None
    for i in range(RETRIES):
        try:
            _throttle()
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{fn.__name__} 连续{RETRIES}次失败: {last_err}")


def _cached(key: str, live_fn, allow_stale: bool = True, accept_cached=None):
    """三层兜底:当天缓存 → 实时拉取(成功即落库)→ 陈旧缓存。
    live_fn 内部可自带双源降级。
    accept_cached(data)->bool:判定当天缓存是否够格短路;返回 False 则重新尝试实时拉取
    (用于避免"备源降级结果"占住当天缓存、导致主源恢复后仍不回到主源)。"""
    data, fetched_date = cache.load(key)
    if (data is not None and cache.is_fresh_today(fetched_date)
            and (accept_cached is None or accept_cached(data))):
        return data
    try:
        fresh = live_fn()
        cache.save(key, fresh)
        return fresh
    except Exception as e:  # noqa: BLE001
        if allow_stale and data is not None:
            print(f"  [缓存兜底] {key} 实时获取失败,回退到 {fetched_date} 的缓存 —— {e}")
            return data
        raise


# ---------------------------------------------------------------------------
# 板块数据:双源互备。东财(push2 clist)与 DangInvest 各成体系,排名+成分股
# 各自命名自洽 —— 数据源在"排名"环节一次决定,并通过 _源/_key 串到"成分股",
# 保证同一次运行内 板块 与其 成分股 出自同一源(不会名字对不上)。
#
# prefer_source 可在 config.yaml 切换主源:
#   eastmoney(默认)—— 数据最全(含领涨股),正常日用请求量极小不触发限流;
#   danginvest      —— 彻底不碰 push2 clist,一劳永逸避开东财限流(领涨股置空)。
# ---------------------------------------------------------------------------
_PREFER = str(_DATA.get("prefer_source", "eastmoney")).lower()
_DI_MODE = str(_DATA.get("danginvest_mode", "industry")).lower()


def _rank_eastmoney() -> pd.DataFrame:
    df = _call(ak.stock_board_industry_name_em)
    df["_源"], df["_key"] = "东财", df["板块名称"]
    return df


def _rank_danginvest() -> pd.DataFrame:
    df = _call(danginvest.board_summary, mode=_DI_MODE)
    df["_源"] = "DangInvest"  # board_summary 已带 _key(groupKey)
    return df


def _sector_rank_live() -> pd.DataFrame:
    primary, backup = ((_rank_eastmoney, _rank_danginvest) if _PREFER != "danginvest"
                       else (_rank_danginvest, _rank_eastmoney))
    try:
        return primary()
    except Exception as e:  # noqa: BLE001
        print(f"  [双源降级] 板块排名主源失败,切换备源 —— {e}")
        return backup()


def get_sector_rank() -> pd.DataFrame:
    """行业板块行情(含涨跌幅/领涨股)。主源失败自动切备源,再失败用缓存。"""
    return _cached("sector_rank", _sector_rank_live)


def get_sector_cons(sector) -> pd.DataFrame:
    """板块成分股。sector 为 get_sector_rank() 的一行(含 _源/_key/板块名称),
    据其数据源分派到对应接口,保证与排名同源、命名一致。"""
    src = sector.get("_源", "东财")
    key = sector.get("_key") or sector["板块名称"]
    name = sector["板块名称"]
    if src == "DangInvest":
        live = lambda: _call(danginvest.board_cons, key, mode=_DI_MODE)
    else:
        live = lambda: _call(ak.stock_board_industry_cons_em, symbol=name)
    return _cached(f"sector_cons:{src}:{key}", live)


def _to_sina_symbol(code: str) -> str:
    """6 位代码 → 新浪/akshare daily 前缀符号(6/9 开头沪市,余深市;北交所已在上游排除)。"""
    return ("sh" if code[0] in ("6", "9") else "sz") + code


def _kline_eastmoney(code: str, days: int) -> pd.DataFrame:
    start = (dt.date.today() - dt.timedelta(days=days * 2)).strftime("%Y%m%d")
    df = _call(ak.stock_zh_a_hist, symbol=code, period="daily",
               start_date=start, adjust="qfq")
    # 东财 换手率 为百分数 → 换算成小数(供筹码分布用),缺列则置 0
    df["换手"] = pd.to_numeric(df.get("换手率", 0), errors="coerce").fillna(0) / 100
    return df


def _kline_sina(code: str, days: int) -> pd.DataFrame:
    """新浪日K(前复权),列名对齐东财 schema 供 indicators 使用。"""
    df = _call(ak.stock_zh_a_daily, symbol=_to_sina_symbol(code), adjust="qfq")
    df = df.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                            "high": "最高", "low": "最低", "volume": "成交量"})
    # 新浪 turnover 已是小数换手率;缺失则置 0
    df["换手"] = pd.to_numeric(df.get("turnover", 0), errors="coerce").fillna(0)
    return df


def _coerce_date_col(s: pd.Series) -> pd.Series:
    """归一化日期列为 YYYY-MM-DD 字符串。个别接口偶尔返回毫秒时间戳
    (如 1783036800000),直接 to_datetime 会按年份解析而溢出,这里按需转换。"""
    def one(v):
        t = str(v)
        if t.isdigit() and len(t) >= 12:      # 毫秒 epoch
            return pd.to_datetime(int(t), unit="ms").strftime("%Y-%m-%d")
        return pd.to_datetime(t).strftime("%Y-%m-%d")
    return s.map(one)


def get_kline(code: str, days: int = 180) -> pd.DataFrame:
    """个股日K(前复权)。东财主源,失败切新浪,再失败用缓存。"""
    def _live():
        try:
            df = _kline_eastmoney(code, days)
        except Exception as e:  # noqa: BLE001
            print(f"    [K线降级] {code} 东财失败,切新浪 —— {e}")
            df = _kline_sina(code, days)
        df = df.tail(days).reset_index(drop=True)
        df["日期"] = _coerce_date_col(df["日期"])
        return df
    return _cached(f"kline:{code}:{days}", _live)


def _fund_flow_live(code: str, lmt: int) -> list[dict]:
    """东财个股资金流(日频):主力/超大/大/中/小单净额。走 push2his + Chrome 指纹。
    主力 = 超大单+大单(机构/游资/庄家);散户 ≈ 中单+小单。"""
    secid = ("1." if code[0] in ("6", "9") else "0.") + code
    r = _cf.get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                params={"lmt": str(lmt), "klt": "101",
                        "fields1": "f1,f2,f3,f7",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                        "ut": "b2884a393a59ad64002292a3e90d46a5", "secid": secid},
                timeout=15, impersonate="chrome", proxies=_NO_PROXY)
    r.raise_for_status()
    klines = (r.json().get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        f = line.split(",")
        # f[1]主力 f[2]小单 f[3]中单 f[4]大单 f[5]超大单 (净额,元)
        def w(x):  # 元→万元
            try:
                return round(float(x) / 1e4, 1)
            except (ValueError, TypeError):
                return 0.0
        main, small, mid, big, sup = w(f[1]), w(f[2]), w(f[3]), w(f[4]), w(f[5])
        out.append({"d": f[0],
                    "super": sup,   # 超大单 ≈ 机构/主力
                    "big": big,     # 大单   ≈ 游资/大户
                    "mid": mid,     # 中单   ≈ 中户
                    "small": small,  # 小单   ≈ 散户
                    "main": main,   # 主力 = 超大+大(保留兼容)
                    "main_pct": float(f[6]) if len(f) > 6 and f[6] not in ("", "-") else None})
    return out


def get_fund_flow(code: str, lmt: int = 60) -> list[dict]:
    """个股资金流向(近 lmt 日)。仅东财有;失败回退缓存,再失败返回空列表(报告优雅降级)。"""
    try:
        return _cached(f"fundflow:{code}", lambda: _fund_flow_live(code, lmt))
    except Exception as e:  # noqa: BLE001
        print(f"    [资金流缺失] {code} —— {e}")
        return []


def get_zt_pool(date: str | None = None) -> pd.DataFrame:
    """涨停池(默认最近交易日)。date 格式 YYYYMMDD。"""
    key = f"zt_pool:{date or 'latest'}"
    if date:
        return _cached(key, lambda: _call(ak.stock_zt_pool_em, date=date))
    return _cached(key, lambda: _call(ak.stock_zt_pool_em))


def get_index_snapshot() -> list[dict]:
    """三大指数最近两日收盘,算涨跌幅(新浪源)。"""
    def _live():
        out = []
        for sym, name in [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]:
            df = _call(ak.stock_zh_index_daily, symbol=sym).tail(2)
            c0, c1 = df["close"].iloc[0], df["close"].iloc[1]
            out.append({"name": name, "close": round(float(c1), 2),
                        "pct": round((c1 / c0 - 1) * 100, 2),
                        "date": str(df["date"].iloc[-1])})
        return out
    return _cached("index_snapshot", _live)


def get_index_regime() -> dict:
    """大盘环境总开关(regime gate):上证指数相对 MA20/MA60 的位置定风险档位。
    历史经验:同一策略在指数 MA20 上/下,胜率差 15~20 个百分点。用于动态收紧选股。
    进攻(≥MA20)/ 谨慎(MA60~MA20)/ 防守(<MA60,超跌易接飞刀)。"""
    def _live():
        df = _call(ak.stock_zh_index_daily, symbol="sh000001")
        close = df["close"]
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1])
        c = float(close.iloc[-1])
        if c >= ma20:
            lvl, sd, pf, strict, note = "进攻", 0, 1.0, False, "指数站上MA20,顺势可为,正常选股"
        elif c >= ma60:
            lvl, sd, pf, strict, note = "谨慎", 10, 0.5, False, "指数跌破MA20但守MA60,提高门槛、减少持仓"
        else:
            lvl, sd, pf, strict, note = "防守", 10, 0.4, True, "指数跌破MA60,弱势市;超跌路仅收最强企稳信号(底背离),趋势为主、控仓"
        return {"level": lvl, "close": round(c, 2), "ma20": round(ma20, 2), "ma60": round(ma60, 2),
                "score_delta": sd, "picks_factor": pf, "oversold_ok": True,
                "oversold_strict": strict, "note": note}
    try:
        return _cached("index_regime", _live)
    except Exception:  # noqa: BLE001
        return {"level": "未知", "score_delta": 0, "picks_factor": 1.0, "oversold_ok": True,
                "oversold_strict": False, "note": "大盘数据不可用,按常规处理",
                "close": None, "ma20": None, "ma60": None}


def check_freshness(kline: pd.DataFrame, max_age_days: int = 4) -> tuple[bool, str]:
    """数据新鲜度门禁:最新K线太旧则拒绝出结论(缓存兜底的陈旧数据在此被拦下)。"""
    last = pd.to_datetime(str(kline["日期"].iloc[-1])).date()
    age = (dt.date.today() - last).days
    return age <= max_age_days, f"最新数据 {last}(距今{age}天)"
