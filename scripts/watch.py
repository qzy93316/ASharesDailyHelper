# -*- coding: utf-8 -*-
"""盘中监控 watch.py —— 独立循环,盯 持仓+荐股池,价格触及作战方案里事先算好的计划价位时
桌面弹窗提醒。绕过当日缓存(直接批量快照),只在交易时段轮询。

数据:
  · 告警 —— 腾讯 qt.gtimg.cn 批量快照(现价/昨收/涨跌幅/涨停价/跌停价),每轮拉,不封IP。
  · 分时图 —— 腾讯 ifzq.gtimg.cn 全天分时(每分钟价+累计量),每分钟拉一次,落地
    reports/<日期>/分时缓存.json。拿的是当日 9:30→当前整段,与启动时间无关,
    重启不丢前半段;拉取失败自动沿用磁盘缓存。
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


def fetch_minute(codes: list[str]) -> dict:
    """腾讯全天分时:{code: [{t, p, cvol}]}。一次返回当日 9:30 至当前每分钟的
    价格与**累计成交量**(手),与本脚本启动时间无关 —— 这是分时图完整性的关键:
    无论几点启动、重启几次,拉到的都是从开盘起的整段。cvol 由上层差分成量柱。
    行:"HHMM 价 累计量 累计额"。逐只请求(每分钟仅拉一次,不加请求压力)。"""
    out = {}
    for c in codes:
        c = str(c).zfill(6)
        pre = ("sh" if c[0] in ("6", "9") else "bj" if c[0] in ("8", "4") else "sz") + c
        try:
            r = fetcher._cf_request_with_retry(
                "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                params={"code": pre}, timeout=10)
            node = json.loads(r.text)["data"][pre]["data"]
            rows = []
            for ln in node.get("data", []):
                fld = ln.split()
                if len(fld) < 3:
                    continue
                hhmm = fld[0]
                try:
                    rows.append({"t": hhmm[:2] + ":" + hhmm[2:],
                                 "p": float(fld[1]), "cvol": float(fld[2])})
                except ValueError:
                    continue
            if rows:
                out[c] = rows
        except Exception:  # noqa: BLE001
            continue  # 单只失败不影响其余;本轮无新数据则沿用磁盘缓存
    return out


def _cache_path(date: str) -> Path:
    return ROOT / "reports" / date.replace("-", "") / "分时缓存.json"


def load_series_cache(date: str) -> dict:
    """开机先读当日分时缓存 —— 即使首次拉取前、或收盘后回看,也有完整图形。"""
    p = _cache_path(date)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_series_cache(date: str, series: dict) -> None:
    p = _cache_path(date)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(series, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


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
        # 均线阶梯告警(ma_ladder,信号层):破 MA5 减半 / MA10 全减 / MA20 波段减 / MA60 离场 + 偏离MA5高抛。
        # 已跌破的下行线跳过(现价早在线下,无新鲜穿越,不刷屏);结构止损仍是独立的清仓底线。
        lad = h.get("ladder") or {}
        broken = set(lad.get("broken") or [])
        for r in (lad.get("rungs") or []):
            if r["dir"] == "down":
                if r["line"] in broken:
                    continue
                add(code, name, r["price"], "down", f"📉破{r['line']}",
                    f"跌破{r['line']}({r['price']})→{r['action']}[{r['ratio']}·{r['tier']}]")
            else:  # 高抛做T(偏离MA5>5%,向上穿越触发)
                add(code, name, r["price"], "up", "📈高抛做T", f"{r['action']} {r['price']}({r['why']})")
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
    out = list(items.values())
    # 附当日 MA5/MA10(日级常量、盘中不变),供分时图画参考线辅助研判。K线走缓存(档期新鲜),开机一次。
    for it in out:
        try:
            k = fetcher.get_kline(it["code"], 60)
            closes = [float(x) for x in k["收盘"]]
            it["ma5"] = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else None
            it["ma10"] = round(sum(closes[-10:]) / 10, 2) if len(closes) >= 10 else None
        except Exception:  # noqa: BLE001
            it["ma5"] = it["ma10"] = None
    return out


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
.c{{width:480px;height:300px}} .card{{position:relative;margin:8px}}
.card.big .c{{width:960px;height:600px}} .hd{{padding:4px 12px;color:#9fb0cc}}
.mx{{position:absolute;right:6px;top:4px;z-index:6;cursor:pointer;color:#9fb0cc;background:#1b2536;border:1px solid #25324a;border-radius:3px;padding:0 7px;font-size:15px;line-height:20px;user-select:none}}
.mx:hover{{color:#fff;border-color:#4c8dff}}
.up{{color:#f6465d}}.down{{color:#2ebd85}} .al{{padding:2px 12px;color:#f0b429}}</style></head>
<body><h2>🖥️ 盘中监控 · {date} <span class="hd">更新 {updated}(每{refresh}秒自刷新)· 研究提醒,非喊单</span></h2>
<div class="al">{alerts}</div><div class="g" id="g"></div>
<script>var D={data};
D.forEach(function(s){{var card=document.createElement('div');card.className='card';
var btn=document.createElement('div');btn.className='mx';btn.textContent='⤢';btn.title='放大/还原';
var el=document.createElement('div');el.className='c';el.id='c_'+s.code;
card.appendChild(btn);card.appendChild(el);document.getElementById('g').appendChild(card);
var ch=echarts.init(el);btn.onclick=function(){{card.classList.toggle('big');ch.resize();}};
var T=s.series.map(function(p){{return p.t;}});
var lvl=s.levels.map(function(l){{return {{yAxis:l.price,label:{{formatter:l.type+l.price,color:'#f0b429',position:'insideEndTop',fontSize:9}},lineStyle:{{color:'#f0b429',type:'dashed'}}}};}});
var refs=[];
if(s.ma5!=null)refs.push({{yAxis:s.ma5,label:{{formatter:'MA5 '+s.ma5,color:'#c586ff',position:'insideStartTop',fontSize:9}},lineStyle:{{color:'#c586ff',width:1}}}});
if(s.ma10!=null)refs.push({{yAxis:s.ma10,label:{{formatter:'MA10 '+s.ma10,color:'#4ec9b0',position:'insideStartBottom',fontSize:9}},lineStyle:{{color:'#4ec9b0',width:1}}}});
if(s.prev_close!=null)refs.push({{yAxis:s.prev_close,label:{{formatter:'昨收 '+s.prev_close,color:'#7a869c',position:'insideEndBottom',fontSize:9}},lineStyle:{{color:'#5b6980',type:'dashed',width:1}}}});
// Y轴范围纳入 MA5/MA10/昨收,避免只按分时价自适应导致参考线被裁出区间(看趋势位置比看分时更重要)
var pv=s.series.map(function(p){{return p.p;}}).filter(function(v){{return v!=null;}});
var lo=Math.min.apply(null,pv),hi=Math.max.apply(null,pv);
[s.ma5,s.ma10,s.prev_close].forEach(function(v){{if(v!=null){{lo=Math.min(lo,v);hi=Math.max(hi,v);}}}});
var pad=(hi-lo)*0.06||hi*0.01||1;lo=+(lo-pad).toFixed(2);hi=+(hi+pad).toFixed(2);
// 金叉(现价上穿均价,红▲)/死叉(下穿,绿▼)高亮
var xp=s.crosses.map(function(c){{return {{coord:[c.t,c.p],value:c.kind,symbol:'triangle',symbolRotate:c.kind==='金叉'?0:180,symbolSize:9,itemStyle:{{color:c.kind==='金叉'?'#f6465d':'#2ebd85'}},label:{{show:false}},emphasis:{{label:{{show:true,formatter:c.kind+' '+c.t+' '+c.p,position:'top',color:'#fff',fontSize:10,backgroundColor:'#1b2536',borderColor:'#25324a',borderWidth:1,padding:[2,4]}}}}}};}});
ch.setOption({{title:{{text:s.name+' '+s.code+'  '+(s.price||'-')+'  '+(s.pct>=0?'+':'')+(s.pct||0)+'%',textStyle:{{color:s.pct>=0?'#f6465d':'#2ebd85',fontSize:13}}}},
legend:{{data:['现价','均价'],right:40,top:4,itemWidth:14,itemHeight:8,textStyle:{{color:'#9fb0cc',fontSize:10}}}},
tooltip:{{trigger:'axis',axisPointer:{{link:[{{xAxisIndex:'all'}}]}},backgroundColor:'#1b2536',borderColor:'#25324a',textStyle:{{color:'#d5dced'}}}},
axisPointer:{{link:[{{xAxisIndex:'all'}}]}},
grid:[{{left:50,right:14,top:'12%',height:'48%'}},{{left:50,right:14,top:'71%',height:'19%'}}],
xAxis:[{{type:'category',gridIndex:0,data:T,boundaryGap:false,axisLabel:{{show:false}},axisLine:{{lineStyle:{{color:'#25324a'}}}}}},
{{type:'category',gridIndex:1,data:T,axisLabel:{{color:'#7a869c',fontSize:10}},axisLine:{{lineStyle:{{color:'#25324a'}}}}}}],
yAxis:[{{scale:true,gridIndex:0,min:lo,max:hi,axisLabel:{{color:'#7a869c'}},splitLine:{{lineStyle:{{color:'#25324a'}}}}}},
{{scale:true,gridIndex:1,splitNumber:2,axisLabel:{{color:'#7a869c',fontSize:10,formatter:function(v){{return v>=10000?(v/10000).toFixed(0)+'万手':v;}}}},splitLine:{{show:false}}}}],
series:[{{name:'现价',type:'line',xAxisIndex:0,yAxisIndex:0,data:s.series.map(function(p){{return p.p;}}),showSymbol:false,lineStyle:{{color:'#4c8dff'}},markLine:{{symbol:'none',data:lvl.concat(refs)}},markPoint:{{data:xp}}}},
{{name:'均价',type:'line',xAxisIndex:0,yAxisIndex:0,data:s.series.map(function(p){{return p.a;}}),showSymbol:false,lineStyle:{{color:'#f0cd6a',width:1}}}},
{{name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,barWidth:'60%',data:s.series.map(function(p){{return {{value:p.v,itemStyle:{{color:p.up?'#f6465d':'#2ebd85'}}}};}})}}]}});}});
</script></body></html>"""


def write_chart(date: str, series: dict, snaps: dict, wl: list[dict], alerts_log: list[str]) -> None:
    day = date.replace("-", "")
    data = []
    for it in wl:
        code = it["code"]
        s = snaps.get(code, {})
        raw = series.get(code, [])
        # 累计量差分成分时量柱:本柱 = 本分钟末累计 − 上一分钟末累计。
        # 数据来自全天分时(必从 09:30 起),故首柱 = 其自身累计(开盘首分钟量),不再置 0。
        # 量柱红绿沿用涨跌色:现价 >= 上一分钟现价 记红(涨),否则绿(跌)。
        ser = []
        prev_cvol = prev_p = None
        cum_pv = cum_v = 0.0
        for pt in raw:
            cvol, p = pt.get("cvol"), pt.get("p")
            vol = 0.0
            if cvol is not None:
                vol = max(cvol - prev_cvol, 0.0) if prev_cvol is not None else cvol
            up = prev_p is None or (p is not None and p >= prev_p)
            # 分时均价线(同花顺黄线,VWAP 近似):累计(分钟价×分钟量)/累计量;首分钟或零量退化为现价
            avg = p
            if p is not None:
                cum_pv += p * vol
                cum_v += vol
                avg = round(cum_pv / cum_v, 2) if cum_v > 0 else p
            ser.append({"t": pt["t"], "p": p, "v": round(vol, 1), "up": 1 if up else 0, "a": avg})
            if cvol is not None:
                prev_cvol = cvol
            if p is not None:
                prev_p = p
        # 图上计划价位去掉"📉破MAx"(下面用专门的 MA 参考线画,避免重复),告警逻辑用的 it["levels"] 不动
        chart_levels = [lv for lv in it["levels"] if not lv["type"].startswith("📉破")]
        # 金叉/死叉:现价上穿分时均价=金叉(偏多),下穿=死叉(偏空)
        crosses = []
        for i in range(1, len(ser)):
            a0, p0, a1, p1 = ser[i - 1]["a"], ser[i - 1]["p"], ser[i]["a"], ser[i]["p"]
            if None in (a0, p0, a1, p1):
                continue
            if p0 - a0 <= 0 < p1 - a1:
                crosses.append({"t": ser[i]["t"], "p": p1, "kind": "金叉"})
            elif p0 - a0 >= 0 > p1 - a1:
                crosses.append({"t": ser[i]["t"], "p": p1, "kind": "死叉"})
        data.append({"code": code, "name": it["name"], "price": s.get("price"),
                     "pct": s.get("pct"), "prev_close": s.get("prev_close"),
                     "ma5": it.get("ma5"), "ma10": it.get("ma10"),
                     "levels": chart_levels, "crosses": crosses, "series": ser})
    html = CHART_TMPL.format(
        date=date, refresh=20, updated=dt.datetime.now().strftime("%H:%M:%S"),
        alerts="　".join(alerts_log[-6:]) or "(暂无告警)",
        data=json.dumps(data, ensure_ascii=False))
    (ROOT / "reports" / day / "盘中监控-分时.html").write_text(html, encoding="utf-8")


def _bucket(now: dt.datetime) -> str:
    """半小时时间桶(HH-A=前半 / HH-B=后半),用于告警额度按半小时滚动重置。"""
    return f"{now:%H}-{'A' if now.minute < 30 else 'B'}"


def run(date: str, interval: int, once: bool, cfg: dict) -> None:
    wc = cfg.get("watch", {}) or {}
    max_per = int(wc.get("max_alerts_per_type", 3))
    near = float(wc.get("near_pct", 0.3)) / 100
    th_only = bool(wc.get("trading_hours_only", True)) and not once
    wl = build_watchlist(date)
    codes = [it["code"] for it in wl]
    print(f"监控 {len(codes)} 只:{', '.join(it['name'] for it in wl)}")
    print(f"轮询 {interval}s · 交易时段限制 {'开' if th_only else '关'} · 告警只绑计划价位 · "
          f"每股每类每半小时最多 {max_per} 次(滚动重置)\n")
    fired: dict = {}          # (code,label,半小时桶) → 次数;每半小时滚动重置额度
    zt_state: dict = {}       # code → 是否处于涨停(判炸板)
    series: dict = load_series_cache(date)  # code → [{t,p,cvol}] 全天分时,开机先读缓存
    last_min: str = ""        # 上次拉分时的分钟,用于每分钟只拉一次
    alerts_log: list = []
    while True:
        now = dt.datetime.now()
        if th_only and not _in_trading_hours(now):
            print(f"  {now:%H:%M:%S} 非交易时段,等待…"); time.sleep(30); continue
        try:
            snaps = snapshot(codes)
        except Exception as e:  # noqa: BLE001
            print(f"  {now:%H:%M:%S} 快照失败:{e}"); time.sleep(interval); continue
        # 分时数据:每分钟拉一次腾讯全天分时(不随快照高频拉,省请求),落地缓存。
        # 拿全天段而非自攒点 —— 启动/重启都不缺前半段。拉取失败则沿用已有缓存。
        cur_min = now.strftime("%H:%M")
        if cur_min != last_min:
            last_min = cur_min
            try:
                fresh = fetch_minute(codes)
                if fresh:
                    series.update(fresh)   # 每只用最新全天段整体替换
                    save_series_cache(date, series)
            except Exception as e:  # noqa: BLE001
                print(f"    [分时拉取失败,沿用缓存] {e}")
        for it in wl:
            code, name = it["code"], it["name"]
            s = snaps.get(code)
            if not s or s.get("price") is None:
                continue
            price = s["price"]
            # 触价告警(计划价位,带缓冲)
            for lv in it["levels"]:
                hit = (price <= lv["price"] * (1 + near) if lv["dir"] == "down"
                       else price >= lv["price"] * (1 - near))
                if not hit:
                    continue
                # 日志额度:每股每类每半小时 max_per 次(防 20s 刷屏,每类仍留痕)
                lkey = (code, lv["label"], _bucket(now))
                if fired.get(lkey, 0) >= max_per:
                    continue
                fired[lkey] = fired.get(lkey, 0) + 1
                # 弹窗额度:每股每半小时 max_per 次(合并所有类型),超出只记录不弹窗
                pkey = ("POP", code, _bucket(now))
                popup = fired.get(pkey, 0) < max_per
                if popup:
                    fired[pkey] = fired.get(pkey, 0) + 1
                _emit(alerts_log, now, lv["type"], name, code, price, lv["label"], popup)
            # 涨停/炸板(现算)
            lu = s.get("limit_up")
            if lu:
                if price >= lu - 0.01:
                    if not zt_state.get(code):
                        zt_state[code] = True
                        k = (code, "涨停", _bucket(now))
                        if fired.get(k, 0) < max_per:
                            fired[k] = fired.get(k, 0) + 1
                            pkey = ("POP", code, _bucket(now))
                            popup = fired.get(pkey, 0) < max_per
                            if popup:
                                fired[pkey] = fired.get(pkey, 0) + 1
                            _emit(alerts_log, now, "📈涨停", name, code, price, f"封涨停 {lu}", popup)
                elif zt_state.get(code):  # 曾涨停,现开板 = 炸板
                    zt_state[code] = False
                    k = (code, "炸板", _bucket(now))
                    if fired.get(k, 0) < max_per:
                        fired[k] = fired.get(k, 0) + 1
                        pkey = ("POP", code, _bucket(now))
                        popup = fired.get(pkey, 0) < max_per
                        if popup:
                            fired[pkey] = fired.get(pkey, 0) + 1
                        _emit(alerts_log, now, "💥炸板", name, code, price, f"涨停开板(涨停价{lu})", popup)
            # 当日涨幅>5% 未涨停 → 高抛做T(均线战法 T2/S1,现算:看盘中涨幅不看收盘)
            pct = s.get("pct")
            not_zt = not (lu and price >= lu - 0.01)
            if pct is not None and pct >= 5 and not_zt:
                k = (code, "涨幅高抛", _bucket(now))
                if fired.get(k, 0) < max_per:
                    fired[k] = fired.get(k, 0) + 1
                    pkey = ("POP", code, _bucket(now))
                    popup = fired.get(pkey, 0) < max_per
                    if popup:
                        fired[pkey] = fired.get(pkey, 0) + 1
                    _emit(alerts_log, now, "📈涨幅高抛", name, code, price,
                          f"当日涨幅 {pct}%>5% 未涨停,短线高抛做T(不看收盘)", popup)
        try:
            write_chart(date, series, snaps, wl, alerts_log)
        except Exception as e:  # noqa: BLE001
            print(f"    [分时页写入失败] {e}")
        if once:
            print("\n(--once 单轮结束)"); break
        time.sleep(interval)


def _emit(log, now, atype, name, code, price, detail, popup: bool = True) -> None:
    line = f"{now:%H:%M:%S} {atype} {name}({code}) 现价{price} — {detail}"
    print(("  🔔 " if popup else "  📝 ") + line + ("" if popup else "  [超弹窗额度·仅记录]"))
    log.append(f"{now:%H:%M} {atype}{name} {price}")
    if popup:  # 弹窗额度内才桌面弹窗;超额只落日志/控制台
        notify_desktop(f"{atype} {name}", f"{name}({code}) 现价{price}\n{detail}\n(计划价位告警·非喊单)")
    # 落告警日志(无论是否弹窗,均记录以便追溯)
    day = now.strftime("%Y%m%d")
    logf = ROOT / "reports" / day / "盘中告警.log"
    try:
        logf.parent.mkdir(parents=True, exist_ok=True)
        with logf.open("a", encoding="utf-8") as f:
            f.write(line + ("" if popup else "  [仅记录]") + "\n")
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
