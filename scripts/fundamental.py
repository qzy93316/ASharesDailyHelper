# -*- coding: utf-8 -*-
"""基本面速览 —— 荐股池个股的估值/成长/质量三维快照(纯代码计算,AI 只解读)。

数据源(全免费、防封):
  估值现值   腾讯财经 qt.gtimg.cn(PE-TTM/PE静/PB/总市值/流通市值,不封IP,配方见
             third_party/a-stock-data §1.2)
  估值分位   百度股市通 近五年 PE-TTM/PB 日序列 → 当前值历史百分位(akshare)
  成长/质量  东财业绩报表(akshare stock_yjbb_em):营收/净利同比、ROE、毛利率、EPS
             —— 一次拉全市场按报告期缓存,自动回退最近已披露期

输出维度:
  估值:PE-TTM(+近5年分位)、PB(+分位)、市值
  成长:最近报告期 营收YoY / 净利YoY(+PEG 粗算 = PE-TTM / 净利YoY)
  质量:ROE(报告期)、毛利率
  结论:规则化标签(高增长/负增长/估值历史高低位/PEG透支…)+ 一句话解读

用法:
  python fundamental.py 600584 600176      # 打印基本面速览
注意:业绩为披露口径(Q1 ROE 未年化);PEG 用单季 YoY 粗算仅作参考;
     银行/券商/亏损股的 PE 口径失真,标签会注明。
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402

_YJBB_CACHE: dict[str, "object"] = {}   # 进程内:报告期 → DataFrame(全市场)


def tencent_quote(codes: list[str]) -> dict[str, dict]:
    """腾讯财经批量实时估值(PE/PB/市值)。GBK,~分隔;不封IP。"""
    pre = []
    for c in codes:
        c = str(c).zfill(6)
        pre.append(("sh" if c.startswith(("6", "9")) else "bj" if c.startswith(("8", "4")) else "sz") + c)
    r = fetcher._cf.get("https://qt.gtimg.cn/q=" + ",".join(pre),
                        timeout=10, impersonate="chrome", proxies=fetcher._NO_PROXY)
    r.raise_for_status()
    out = {}
    for line in r.content.decode("gbk", errors="replace").strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = line.split("=")[0].split("_")[-1][2:]
        def f(i):
            try:
                return float(vals[i]) if vals[i] else None
            except ValueError:
                return None
        out[code] = {"name": vals[1], "price": f(3), "pe_ttm": f(39), "pe_static": f(52),
                     "pb": f(46), "mcap_yi": f(44), "float_mcap_yi": f(45),
                     "turnover_pct": f(38)}
    return out


def _report_periods(today: dt.date) -> list[str]:
    """按今天倒推最近三个报告期(YYYYMMDD),披露有时滞,逐期回退。"""
    qs = []
    y = today.year
    for yy in (y, y - 1):
        for md in ("1231", "0930", "0630", "0331"):
            d = dt.date(yy, int(md[:2]), int(md[2:]))
            if d < today:
                qs.append(f"{yy}{md}")
    return qs[:3]


def _yjbb(period: str):
    """东财业绩报表(全市场,按报告期)。当日缓存;未出季报返回 None。"""
    if period in _YJBB_CACHE:
        return _YJBB_CACHE[period]
    import akshare as ak
    try:
        df = fetcher._cached(f"yjbb:{period}", lambda: fetcher._call(ak.stock_yjbb_em, date=period))
        df = df.set_index(df["股票代码"].astype(str).str.zfill(6)) if df is not None and len(df) else None
    except Exception:  # noqa: BLE001
        # 报告期未到披露窗口时东财返回空,属正常,回退上一期
        print(f"    [业绩报表] {period} 尚无数据,回退上一报告期")
        df = None
    _YJBB_CACHE[period] = df
    return df


def growth(code: str) -> dict | None:
    """最近已披露报告期的成长/质量指标。"""
    code = str(code).zfill(6)
    for period in _report_periods(dt.date.today()):
        df = _yjbb(period)
        if df is None or code not in df.index:
            continue
        row = df.loc[code]
        def g(col):
            try:
                v = float(row[col])
                return None if v != v else round(v, 2)  # NaN → None
            except (KeyError, TypeError, ValueError):
                return None
        return {"period": f"{period[:4]}年报" if period.endswith("1231")
                else f"{period[:4]}Q{(int(period[4:6]) + 2) // 3}",
                "rev_yoy": g("营业总收入-同比增长"), "profit_yoy": g("净利润-同比增长"),
                "roe": g("净资产收益率"), "gross_margin": g("销售毛利率"),
                "eps": g("每股收益")}
    return None


def valuation_percentile(code: str, indicator: str = "市盈率(TTM)",
                         timeout_sec: int = 25) -> float | None:
    """百度股市通近五年估值序列 → 当前值百分位(0~100)。失败/超时优雅缺省。
    注:百度接口对部分 indicator(如市净率)会无响应挂死且 akshare 未设超时,
    这里用线程级超时兜底;PB 分位因此不取,只取 PE-TTM 分位。"""
    import threading
    import akshare as ak
    key = f"val_pct:{code}:{indicator}"
    def _live():
        df = fetcher._call(ak.stock_zh_valuation_baidu, symbol=code,
                           indicator=indicator, period="近五年")
        s = df["value"].dropna()
        if len(s) < 250:
            return None
        cur = float(s.iloc[-1])
        return round(float((s < cur).mean()) * 100, 1)
    def _cached_live():
        return fetcher._cached(key, _live)
    # daemon 线程跑取数:挂死也不阻塞主流程/进程退出(ThreadPoolExecutor 的
    # 非 daemon 工作线程会在解释器退出时被 join,挂死接口会卡住整个脚本)
    box: list = [None]
    def _run():
        try:
            box[0] = _cached_live()
        except Exception:  # noqa: BLE001
            box[0] = None
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_sec)
    return box[0]


def summarize(code: str) -> dict | None:
    """单只个股基本面速览(估值+分位+成长+质量+标签+一句话)。"""
    code = str(code).zfill(6)
    try:
        q = tencent_quote([code]).get(code) or {}
    except Exception as e:  # noqa: BLE001
        print(f"    [估值缺失] {code} —— {e}")
        q = {}
    gr = growth(code) or {}
    pe, pb = q.get("pe_ttm"), q.get("pb")
    pe_pct = valuation_percentile(code, "市盈率(TTM)") if pe and pe > 0 else None
    pb_pct = None  # 百度市净率序列接口不稳(挂死),暂不取分位
    profit_yoy = gr.get("profit_yoy")
    # 净利YoY>300% 多为低基数/一次性损益,PEG 失真不算
    peg = round(pe / profit_yoy, 2) if (pe and pe > 0 and profit_yoy
                                        and 5 < profit_yoy <= 300) else None
    tags = []
    if pe is None or pe <= 0:
        tags.append("PE为负(亏损/微利)")
    elif pe_pct is not None:
        tags.append(f"PE处近5年{pe_pct:.0f}%分位" + ("(历史高位)" if pe_pct >= 80
                    else "(历史低位)" if pe_pct <= 20 else ""))
    if profit_yoy is not None:
        tags.append(f"净利{'+' if profit_yoy >= 0 else ''}{profit_yoy}%"
                    + ("(高增长)" if profit_yoy >= 30 else "(负增长)" if profit_yoy < 0 else ""))
    if gr.get("rev_yoy") is not None and profit_yoy is not None:
        if gr["rev_yoy"] > 0 and profit_yoy > 0:
            tags.append("营收净利双增")
        elif gr["rev_yoy"] < 0 and profit_yoy > 0:
            tags.append("增利不增收(留意持续性)")
    if peg is not None:
        tags.append(f"PEG≈{peg}" + ("(估值划算)" if peg < 1 else "(增速透支)" if peg > 3 else ""))
    if not gr:
        tags.append("近期报告期未披露/未覆盖")
    # 一句话解读(规则化)
    parts = []
    if pe and pe > 0:
        parts.append(f"PE-TTM {pe}" + (f"(近5年{pe_pct:.0f}%分位)" if pe_pct is not None else ""))
    else:
        parts.append("PE-TTM 为负")
    if pb:
        parts.append(f"PB {pb}" + (f"({pb_pct:.0f}%分位)" if pb_pct is not None else ""))
    if q.get("mcap_yi"):
        parts.append(f"市值{q['mcap_yi']:.0f}亿")
    if gr:
        parts.append(f"{gr['period']} 营收{gr['rev_yoy']:+}%/净利{profit_yoy:+}%"
                     if gr.get("rev_yoy") is not None and profit_yoy is not None else f"{gr['period']}")
        if gr.get("roe") is not None:
            parts.append(f"ROE {gr['roe']}%(报告期)")
    return {"code": code, "pe_ttm": pe, "pe_static": q.get("pe_static"), "pb": pb,
            "mcap_yi": q.get("mcap_yi"), "pe_pct5y": pe_pct, "pb_pct5y": pb_pct,
            "peg": peg, **{k: gr.get(k) for k in ("period", "rev_yoy", "profit_yoy",
                                                  "roe", "gross_margin", "eps")},
            "tags": tags, "comment": " · ".join(parts)}


def main() -> None:
    codes = sys.argv[1:]
    if not codes:
        print("用法: python fundamental.py 600584 600176"); return
    for c in codes:
        f = summarize(c)
        print(f"\n===== {c} =====")
        print(json.dumps(f, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
