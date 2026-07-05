# -*- coding: utf-8 -*-
"""盘中监控 watch.py —— 独立循环,盯 持仓+荐股池,价格触及作战方案里事先算好的计划价位时
桌面弹窗提醒。绕过当日缓存(直接批量快照),只在交易时段轮询。

数据:腾讯行情 qt.gtimg.cn 批量快照(现价/昨收/涨跌幅/涨停价/跌停价,不封IP)。
告警(只绑事先写好的计划价位,不做盘中临时喊单):
  ⛔止损   跌破结构止损
  🎯介入   触及建仓/逢低吸/回踩加仓价(下行到位)
  🚀突破   突破压力/逢高建仓价(上行到位)
  🔺反弹减 减仓触发价(上行到位)
  📈涨停 / 💥炸板(涨停后开板)—— 用涨停价现算
去重:每股每类告警每日限 N 次(config),防"每3秒焦虑一次"。
通知:桌面弹窗(win11toast>win10toast>MessageBox 兜底)+ 控制台 + 日志;并写自刷新分时页。

安全边界:只读行情、只提醒,**不接触账户密码、不代下单**。

用法(在你本机、交易时段跑):
  python watch.py                     # 读当日作战方案,循环监控
  python watch.py --once              # 单轮冒烟测(休市也能测阈值判断)
  python watch.py --date 2026-07-05 --interval 20
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetcher  # noqa: E402
import yaml  # noqa: E402

ROOT = Path(__file__).parent.parent


def snapshot(codes: list[str]) -> dict:
    """腾讯批量快照:{code: {name, price, prev_close, pct, limit_up, limit_down}}。"""
    if not codes:
        return {}
    pre = []
    for c in codes:
        c = str(c).zfill(6)
        pre.append(("sh" if c[0] in ("6", "9") else "bj" if c[0] in ("8", "4") else "sz") + c)
    r = fetcher._cf.get("https://qt.gtimg.cn/q=" + ",".join(pre),
                        timeout=10, impersonate="chrome", proxies=fetcher._NO_PROXY)
    r.raise_for_status()
    out = {}
    for line in r.content.decode("gbk", errors="replace").strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        v = line.split('"')[1].split("~")
        if len(v) < 49:
            continue
        code = line.split("=")[0].split("_")[-1][2:]

        def f(i):
            try:
                return float(v[i]) if v[i] else None
            except ValueError:
                return None
        out[code] = {"name": v[1], "price": f(3), "prev_close": f(4), "pct": f(32),
                     "limit_up": f(47), "limit_down": f(48)}
    return out


def build_watchlist(date: str) -> list[dict]:
    """从作战方案侧车提取监控清单 + 计划价位 → 每股一组告警关卡 levels。"""
    day = date.replace("-", "")
    p = ROOT / "reports" / day / f"作战方案-{date}.json"
    if not p.exists():
        raise SystemExit(f"找不到作战方案:{p}(先跑 action_plan.py / run_workflow.py)")
    ap = json.loads(p.read_text(encoding="utf-8"))
    items = {}

    def add(code, name, price, dirn, atype, label):
        if not price:
            return
        it = items.setdefault(code, {"code": code, "name": name, "levels": []})
        it["levels"].append({"price": round(float(price), 2), "dir": dirn,
                             "type": atype, "label": label})

    for h in ap.get("holdings", []):
        code, name = h["code"], h["name"]
        add(code, name, h.get("stop"), "down", "⛔止损", f"跌破结构止损 {h.get('stop')}")
        if h.get("trigger") and h.get("trigger") != h.get("stop"):
            if h.get("trigger_dir") == "up":
                add(code, name, h["trigger"], "up", "🔺反弹减",
                    f"反弹到 {h['trigger']},{h['action']}")
            else:
                add(code, name, h["trigger"], "down", "🎯回踩加",
                    f"回踩到 {h['trigger']},{h['action']}")
    for p_ in ap.get("pool", []):
        code, name, act = p_["code"], p_["name"], p_.get("action", "")
        add(code, name, p_.get("stop"), "down", "⛔止损", f"跌破止损 {p_.get('stop')}")
        pp = p_.get("plan_price")
        if act in ("建仓", "逢低吸", "观察(超跌影子)"):
            add(code, name, pp, "down", "🎯介入", f"回踩到介入价 {pp}({act})")
        elif act == "逢高突破建仓":
            add(code, name, pp, "up", "🚀突破", f"突破 {pp} 确认介入")
        if p_.get("target"):
            add(code, name, p_["target"], "up", "🏁目标", f"触及目标 {p_['target']}")
    return list(items.values())


def _in_trading_hours(now: dt.datetime) -> bool:
    t = now.time()
    return ((dt.time(9, 30) <= t <= dt.time(11, 30))
            or (dt.time(13, 0) <= t <= dt.time(15, 0)))


def notify_desktop(title: str, msg: str) -> None:
    """桌面弹窗:win11toast > win10toast > MessageBox(守护线程,非阻塞) > 控制台。"""
    try:
        from win11toast import notify  # type: ignore
        notify(title, msg)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        from win10toast import ToastNotifier  # type: ignore
        ToastNotifier().show_toast(title, msg, duration=8, threaded=True)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes
        import threading
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, msg, title, 0x40 | 0x1000), daemon=True).start()  # MB_ICONINFO|MB_TOPMOST
    except Exception:  # noqa: BLE001
        print(f"    [弹窗降级] {title} — {msg}")
    try:
        import winsound
        winsound.Beep(880, 300)
    except Exception:  # noqa: BLE001
        pass


CHART_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}"><title>盘中监控 {date}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>body{{margin:0;background:#0d1420;color:#d5dced;font:14px "Microsoft YaHei",sans-serif}}
h2{{padding:10px 16px;margin:0}} .g{{display:flex;flex-wrap:wrap}}
.c{{width:480px;height:240px;margin:8px}} .hd{{padding:4px 12px;color:#9fb0cc}}
.up{{color:#f6465d}}.down{{color:#2ebd85}} .al{{padding:2px 12px;color:#f0b429}}</style></head>
<body><h2>🖥️ 盘中监控 · {date} <span class="hd">更新 {updated}(每{refresh}秒自刷新)· 研究提醒,非喊单</span></h2>
<div class="al">{alerts}</div><div class="g" id="g"></div>
<script>var D={data};
D.forEach(function(s){{var el=document.createElement('div');el.className='c';el.id='c_'+s.code;
document.getElementById('g').appendChild(el);var ch=echarts.init(el);
var lvl=s.levels.map(function(l){{return {{yAxis:l.price,label:{{formatter:l.type+l.price,color:'#f0b429',position:'insideEndTop'}},lineStyle:{{color:'#f0b429',type:'dashed'}}}};}});
ch.setOption({{title:{{text:s.name+' '+s.code+'  '+(s.price||'-')+'  '+(s.pct>=0?'+':'')+(s.pct||0)+'%',textStyle:{{color:s.pct>=0?'#f6465d':'#2ebd85',fontSize:13}}}},
grid:{{left:44,right:16,top:30,bottom:20}},xAxis:{{type:'category',data:s.series.map(function(p){{return p.t;}}),axisLabel:{{color:'#7a869c'}}}},
yAxis:{{scale:true,axisLabel:{{color:'#7a869c'}},splitLine:{{lineStyle:{{color:'#25324a'}}}}}},
series:[{{type:'line',data:s.series.map(function(p){{return p.p;}}),showSymbol:false,lineStyle:{{color:'#4c8dff'}},markLine:{{symbol:'none',data:lvl}}}}]}});}});
</script></body></html>"""


def write_chart(date: str, series: dict, snaps: dict, wl: list[dict], alerts_log: list[str]) -> None:
    day = date.replace("-", "")
    data = []
    for it in wl:
        code = it["code"]
        s = snaps.get(code, {})
        data.append({"code": code, "name": it["name"], "price": s.get("price"),
                     "pct": s.get("pct"), "levels": it["levels"],
                     "series": series.get(code, [])})
    html = CHART_TMPL.format(
        date=date, refresh=20, updated=dt.datetime.now().strftime("%H:%M:%S"),
        alerts="　".join(alerts_log[-6:]) or "(暂无告警)",
        data=json.dumps(data, ensure_ascii=False))
    (ROOT / "reports" / day / "盘中监控-分时.html").write_text(html, encoding="utf-8")


def run(date: str, interval: int, once: bool, cfg: dict) -> None:
    wc = cfg.get("watch", {}) or {}
    max_per = int(wc.get("max_alerts_per_type", 3))
    near = float(wc.get("near_pct", 0.3)) / 100
    th_only = bool(wc.get("trading_hours_only", True)) and not once
    wl = build_watchlist(date)
    codes = [it["code"] for it in wl]
    print(f"监控 {len(codes)} 只:{', '.join(it['name'] for it in wl)}")
    print(f"轮询 {interval}s · 交易时段限制 {'开' if th_only else '关'} · 告警只绑计划价位\n")
    fired: dict = {}          # (code,label) → 次数
    zt_state: dict = {}       # code → 是否处于涨停(判炸板)
    series: dict = {}         # code → [{t,p}]
    alerts_log: list = []
    while True:
        now = dt.datetime.now()
        if th_only and not _in_trading_hours(now):
            print(f"  {now:%H:%M:%S} 非交易时段,等待…"); time.sleep(30); continue
        try:
            snaps = snapshot(codes)
        except Exception as e:  # noqa: BLE001
            print(f"  {now:%H:%M:%S} 快照失败:{e}"); time.sleep(interval); continue
        ts = now.strftime("%H:%M")
        for it in wl:
            code, name = it["code"], it["name"]
            s = snaps.get(code)
            if not s or s.get("price") is None:
                continue
            price = s["price"]
            series.setdefault(code, []).append({"t": ts, "p": price})
            # 触价告警(计划价位,带缓冲)
            for lv in it["levels"]:
                key = (code, lv["label"])
                if fired.get(key, 0) >= max_per:
                    continue
                hit = (price <= lv["price"] * (1 + near) if lv["dir"] == "down"
                       else price >= lv["price"] * (1 - near))
                if hit:
                    fired[key] = fired.get(key, 0) + 1
                    _emit(alerts_log, now, lv["type"], name, code, price, lv["label"])
            # 涨停/炸板(现算)
            lu = s.get("limit_up")
            if lu:
                if price >= lu - 0.01:
                    if not zt_state.get(code):
                        zt_state[code] = True
                        k = (code, "涨停")
                        if fired.get(k, 0) < max_per:
                            fired[k] = fired.get(k, 0) + 1
                            _emit(alerts_log, now, "📈涨停", name, code, price, f"封涨停 {lu}")
                elif zt_state.get(code):  # 曾涨停,现开板 = 炸板
                    zt_state[code] = False
                    k = (code, "炸板")
                    if fired.get(k, 0) < max_per:
                        fired[k] = fired.get(k, 0) + 1
                        _emit(alerts_log, now, "💥炸板", name, code, price, f"涨停开板(涨停价{lu})")
        try:
            write_chart(date, series, snaps, wl, alerts_log)
        except Exception as e:  # noqa: BLE001
            print(f"    [分时页写入失败] {e}")
        if once:
            print("\n(--once 单轮结束)"); break
        time.sleep(interval)


def _emit(log, now, atype, name, code, price, detail) -> None:
    line = f"{now:%H:%M:%S} {atype} {name}({code}) 现价{price} — {detail}"
    print("  🔔 " + line)
    log.append(f"{now:%H:%M} {atype}{name} {price}")
    notify_desktop(f"{atype} {name}", f"{name}({code}) 现价{price}\n{detail}\n(计划价位告警·非喊单)")
    # 落告警日志
    day = now.strftime("%Y%m%d")
    logf = ROOT / "reports" / day / "盘中告警.log"
    try:
        logf.parent.mkdir(parents=True, exist_ok=True)
        with logf.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="盘中监控(触价桌面弹窗)")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--interval", type=int, default=None, help="轮询秒数(默认读config)")
    ap.add_argument("--once", action="store_true", help="单轮冒烟测")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    interval = args.interval or int((cfg.get("watch", {}) or {}).get("interval_sec", 20))
    try:
        run(args.date, interval, args.once, cfg)
    except KeyboardInterrupt:
        print("\n已停止监控。")


if __name__ == "__main__":
    main()
