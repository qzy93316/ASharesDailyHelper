# -*- coding: utf-8 -*-
"""港股点名分析 —— 复用 indicators/scoring/chips/chan/judge 全套引擎,
数据走东财港股日K(stock_hk_hist,含换手率,列结构与A股一致)。
产出的 picks 追加进当日 reports/YYYYMMDD/个股分析-YYYY-MM-DD.json(同代码则覆盖),
可与 A股点名分析合并渲染成一份交互 HTML。

用法:
  python analyze_hk.py 01810=小米集团-W 09988=阿里巴巴-W
注意:港股资金流走东财 push2his(secid 116.xxxxx),失败则优雅缺省。
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
from indicators import compute_indicators  # noqa: E402
from scoring import score_stock  # noqa: E402

ROOT = Path(__file__).parent.parent


def get_kline_hk(code: str, days: int):
    """港股日K(前复权)。akshare 港股接口走普通 requests 会被东财 TLS 指纹拦截,
    这里直接用 fetcher 的 curl_cffi Chrome 指纹客户端调东财 push2his kline。
    列名对齐A股 schema:日期/开盘/收盘/最高/最低/成交量/换手。"""
    import pandas as pd

    def _live():
        r = fetcher._cf.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": f"116.{code}", "klt": "101", "fqt": "1",
                    "lmt": str(days),
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                    "end": "20500101",
                    "ut": "fa5fd1943c7b386f172d6893dbfba10b"},
            timeout=15, impersonate="chrome", proxies=fetcher._NO_PROXY)
        r.raise_for_status()
        klines = (r.json().get("data") or {}).get("klines") or []
        if not klines:
            raise RuntimeError(f"东财港股K线为空:{code}")
        rows = []
        for line in klines:
            f = line.split(",")
            # f0日期 f1开 f2收 f3高 f4低 f5量 f6额 f7振幅 f8涨跌幅 f9涨跌额 f10换手率
            rows.append({"日期": f[0], "开盘": float(f[1]), "收盘": float(f[2]),
                         "最高": float(f[3]), "最低": float(f[4]), "成交量": float(f[5]),
                         "换手": (float(f[10]) / 100) if len(f) > 10 and f[10] not in ("", "-") else 0.0})
        return pd.DataFrame(rows).tail(days).reset_index(drop=True)

    def _live_sina():
        """新浪备源:无换手率列,用总股本估算换手(仅供筹码分布衰减用)。"""
        import akshare as ak
        df = fetcher._call(ak.stock_hk_daily, symbol=code, adjust="qfq")
        df = df.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                                "high": "最高", "low": "最低", "volume": "成交量"})
        shares = HK_TOTAL_SHARES.get(code)
        df["换手"] = (pd.to_numeric(df["成交量"], errors="coerce") / shares).fillna(0) \
            if shares else 0.0
        df = df.tail(days).reset_index(drop=True)
        df["日期"] = df["日期"].astype(str)
        return df

    def _live_dual():
        try:
            return _live()
        except Exception as e:  # noqa: BLE001
            print(f"    [港股K线降级] {code} 东财失败,切新浪 —— {e}")
            return _live_sina()
    return fetcher._cached(f"kline_hk:{code}:{days}", _live_dual)


# 总股本(股,约值)——新浪源缺换手率时估算换手用,仅影响筹码分布衰减强度
HK_TOTAL_SHARES = {
    "01810": 25_100_000_000,   # 小米集团-W
    "09988": 19_100_000_000,   # 阿里巴巴-W
}


def get_fund_flow_hk(code: str, lmt: int = 60) -> list[dict]:
    """港股资金流(东财 push2his,secid=116.xxxxx)。不支持/限流则返回空,报告优雅降级。"""
    def _live():
        r = fetcher._cf.get(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params={"lmt": str(lmt), "klt": "101",
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                    "ut": "b2884a393a59ad64002292a3e90d46a5", "secid": f"116.{code}"},
            timeout=15, impersonate="chrome", proxies=fetcher._NO_PROXY)
        r.raise_for_status()
        klines = (r.json().get("data") or {}).get("klines") or []
        out = []
        for line in klines:
            f = line.split(",")
            def w(x):
                try:
                    return round(float(x) / 1e4, 1)
                except (ValueError, TypeError):
                    return 0.0
            out.append({"d": f[0], "super": w(f[5]), "big": w(f[4]),
                        "mid": w(f[3]), "small": w(f[2]), "main": w(f[1]),
                        "main_pct": float(f[6]) if len(f) > 6 and f[6] not in ("", "-") else None})
        return out
    try:
        return fetcher._cached(f"fundflow_hk:{code}", _live)
    except Exception as e:  # noqa: BLE001
        print(f"    [港股资金流缺失] {code} —— {e}")
        return []


def analyze_one_hk(code: str, name: str, cfg: dict) -> dict:
    rp, hr = cfg["report"], cfg["hard_rules"]
    dd = cfg["style"]["max_drawdown_pct"]
    chart_bars = int(rp.get("chart_bars", 120))
    k = get_kline_hk(code, rp["kline_days"])
    fresh, fresh_msg = fetcher.check_freshness(k, cfg["data"]["freshness_max_age_days"])
    ind = compute_indicators(k)
    sc = score_stock(ind, hr)
    target = round(ind["pressure"], 2)
    kk = k.tail(chart_bars)
    bars = [{"d": str(r["日期"]), "o": float(r["开盘"]), "c": float(r["收盘"]),
             "l": float(r["最低"]), "h": float(r["最高"]), "v": float(r["成交量"]),
             "换手": round(float(r.get("换手", 0) or 0), 5)}
            for _, r in kk.iterrows()]
    chip = chips.compute_chips(bars)
    flow = get_fund_flow_hk(code)
    chan_res = chan.analyze(bars)
    turn_pct = round((bars[-1].get("换手", 0) or 0) * 100, 2)
    jd = judge.synthesize(ind, chip, judge.flow_sum(flow), chan_res, target, dd, turn_pct)
    return {
        "code": code, "name": name, "sector": "点名分析(港股)", "sector_pct": 0.0,
        "signal": sc["signal"], "score": sc["total"], "breakdown": sc["breakdown"],
        "entry_date": ind["date"], "entry_close": ind["close"],
        "plan_stop": jd["structural_stop"]["stop"], "plan_target": target,
        "indicators": ind, "bars": bars,
        "chips": chip, "chip_comment": chips.control_comment(chip),
        "fund_flow": flow, "judge": jd, "fresh_msg": fresh_msg, "vetoes": sc["vetoes"],
    }


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法: python analyze_hk.py 01810=小米集团-W 09988=阿里巴巴-W"); return
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = dt.date.today()
    picks = []
    for a in args:
        code, _, name = a.partition("=")
        code = code.strip().zfill(5)
        name = name.strip() or code
        print(f"分析 {name}({code}) ...")
        try:
            picks.append(analyze_one_hk(code, name, cfg))
        except Exception as e:  # noqa: BLE001
            print(f"  失败:{e}")
    day_dir = ROOT / "reports" / str(today).replace("-", "")
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"个股分析-{today}.json"
    if out.exists():
        sidecar = json.loads(out.read_text(encoding="utf-8"))
        old = [p for p in sidecar["picks"] if p["code"] not in {x["code"] for x in picks}]
        sidecar["picks"] = old + picks
        sidecar["scanned"] = len(sidecar["picks"])
    else:
        sidecar = {"date": str(today), "source": "点名分析", "scanned": len(picks),
                   "indexes": fetcher.get_index_snapshot(), "sectors": [], "picks": picks}
    out.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n侧车 → {out}(picks 共 {sidecar['scanned']} 只)")


if __name__ == "__main__":
    main()
