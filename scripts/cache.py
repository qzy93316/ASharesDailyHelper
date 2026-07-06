# -*- coding: utf-8 -*-
"""本地缓存/落库层 — SQLite 单文件,零运维(见设计文档 §3.1)。

作用有二:
1. 韧性:接口临时失效(限流/断连)时回退到最近一次成功数据,让脚本永远跑得完;
2. 省流:同一天重复运行不再打接口,天然规避免费源的短时限流阈值。

DataFrame 以 JSON(orient=split)序列化存入;list/dict 直接存 JSON。
"""
import io
import json
import sqlite3
import datetime as dt
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "market.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kv_cache (
               key          TEXT PRIMARY KEY,
               kind         TEXT NOT NULL,      -- 'df' | 'json'
               payload      TEXT NOT NULL,
               fetched_date TEXT NOT NULL,      -- YYYY-MM-DD,判定"当天是否已取"
               fetched_at   TEXT NOT NULL       -- ISO 时间戳
           )"""
    )
    return conn


def load_meta(key: str):
    """读缓存并带回抓取时间戳。返回 (data, fetched_date_str, fetched_at_iso) 或 (None, None, None)。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT kind, payload, fetched_date, fetched_at FROM kv_cache WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None, None, None
    kind, payload, fetched_date, fetched_at = row
    if kind == "df":
        return pd.read_json(io.StringIO(payload), orient="split"), fetched_date, fetched_at
    return json.loads(payload), fetched_date, fetched_at


def load(key: str):
    """读缓存。返回 (data, fetched_date_str) 或 (None, None)。
    data 为 DataFrame 或 原始 list/dict。"""
    data, fetched_date, _ = load_meta(key)
    return data, fetched_date


def save(key: str, data) -> None:
    """写缓存(覆盖同 key)。"""
    if isinstance(data, pd.DataFrame):
        # date_format="iso":避免 pandas 默认把日期列转成毫秒时间戳,
        # 否则回读时变成大整数,下游 to_datetime 会按年份解析而溢出
        kind, payload = "df", data.to_json(orient="split", force_ascii=False, date_format="iso")
    else:
        kind, payload = "json", json.dumps(data, ensure_ascii=False)
    now = dt.datetime.now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv_cache "
            "(key, kind, payload, fetched_date, fetched_at) VALUES (?,?,?,?,?)",
            (key, kind, payload, now.date().isoformat(), now.isoformat(timespec="seconds")),
        )


def is_fresh_today(fetched_date: str | None) -> bool:
    """缓存是否为"今天"取到的。"""
    return fetched_date == dt.date.today().isoformat()


# A 股收盘 15:00,EOD 数据通常盘后几分钟内可取;15:05 作为"数据档期"分界。
_EOD_BOUNDARY = (15, 5)


def _market_epoch(ts: dt.datetime) -> str:
    """把一个时刻映射到它所属的"日K数据档期":以当日收盘(15:05)为界,
    分成 pre(盘前/盘中,当日K线尚未定稿)与 post(盘后,当日K线已定稿)两档,
    并带上日期以区分不同交易日。同档期内的日K缓存可安全复用。"""
    boundary = ts.replace(hour=_EOD_BOUNDARY[0], minute=_EOD_BOUNDARY[1], second=0, microsecond=0)
    tag = "post" if ts >= boundary else "pre"
    return f"{ts.date().isoformat()}:{tag}"


def is_fresh_kline(fetched_at: str | None) -> bool:
    """日K专用新鲜度:抓取时点与当前处于同一"数据档期"才复用(见 _market_epoch)。
    根治"盘前抓、盘后用"取到隔日旧价:盘前填的缓存到盘后即判为过期、触发一次重拉补当日
    收盘;盘中互相复用、盘后互相复用都不额外请求(每跨一次收盘边界最多多拉一次)。"""
    if not fetched_at:
        return False
    try:
        ts = dt.datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return _market_epoch(ts) == _market_epoch(dt.datetime.now())


def load_aged(key: str, max_age_days: int):
    """按天龄读取:缓存距今 ≤ max_age_days 才返回,否则视为过期。
    用于"周级刷新"的全市场清单等——避免每次重拉。返回 (data, fetched_date) 或 (None, fetched_date)。"""
    data, fetched_date = load(key)
    if data is None or not fetched_date:
        return None, fetched_date
    try:
        age = (dt.date.today() - dt.date.fromisoformat(fetched_date)).days
    except ValueError:
        return None, fetched_date
    return (data, fetched_date) if age <= max_age_days else (None, fetched_date)
