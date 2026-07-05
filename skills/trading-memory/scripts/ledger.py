# -*- coding: utf-8 -*-
"""操盘台账 —— 事件流记录 + FIFO 盈亏/胜率/纪律统计。纯标准库,无需安装依赖。

设计原则(呼应项目「AI零计算」):所有盈亏、胜率、持仓天数由本脚本计算,
AI 只读取 JSON 结果做解读。数据存 trades/ledger.jsonl,一行一个事件。

子命令:
  add        追加一个事件(buy/add/trim/sell/watch)
  positions  列出未平仓持仓(FIFO 加权成本)
  stats      统计已平仓交易的胜率/盈亏/持仓天数/止损纪律
"""
import argparse
import json
import sys
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path

# trades/ 位于项目根:skills/trading-memory/scripts/ledger.py → 上溯 3 级
ROOT = Path(__file__).resolve().parents[3]
LEDGER = ROOT / "trades" / "ledger.jsonl"

BUY_ACTIONS = {"buy", "add"}
SELL_ACTIONS = {"trim", "sell"}
ALL_ACTIONS = BUY_ACTIONS | SELL_ACTIONS | {"watch"}


def _load() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    # 按日期稳定排序,保证 FIFO 正确
    return sorted(out, key=lambda e: (e.get("date", ""), e.get("_ts", "")))


def _append(event: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _days_between(d1: str, d2: str) -> int:
    return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days


def cmd_add(args) -> None:
    if args.action not in ALL_ACTIONS:
        print(f"未知 action={args.action},可用:{sorted(ALL_ACTIONS)}"); sys.exit(1)
    if args.action in (BUY_ACTIONS | SELL_ACTIONS):
        if args.price is None or args.shares is None:
            print("买卖操作必须提供 --price 和 --shares"); sys.exit(1)
        if args.shares <= 0:
            print("--shares 必须为正整数"); sys.exit(1)
    event = {
        "date": args.date, "action": args.action,
        "code": str(args.code).zfill(6), "name": args.name,
        "price": args.price, "shares": args.shares,
        "reason": args.reason, "from_report": args.from_report,
        "plan_stop": args.plan_stop, "plan_target": args.plan_target,
        "note": args.note,
        "_ts": datetime.now().isoformat(timespec="seconds"),
    }
    _append(event)
    print(json.dumps({"ok": True, "recorded": event}, ensure_ascii=False, indent=2))


def _fifo_match(events: list[dict]):
    """FIFO 撮合买卖,返回 (已平仓交易列表, 未平仓持仓 dict)。
    已平仓交易含:开/平仓日期、买价、卖价、股数、盈亏、收益率、持仓天数、计划止损。"""
    lots = defaultdict(deque)     # code -> deque of open buy lots
    names = {}
    closed = []
    for e in events:
        code = e.get("code"); names[code] = e.get("name", code)
        act = e.get("action")
        if act in BUY_ACTIONS:
            lots[code].append({"date": e["date"], "price": float(e["price"]),
                               "shares": int(e["shares"]), "plan_stop": e.get("plan_stop"),
                               "from_report": e.get("from_report")})
        elif act in SELL_ACTIONS:
            remain = int(e["shares"]); sell_price = float(e["price"])
            while remain > 0 and lots[code]:
                lot = lots[code][0]
                matched = min(remain, lot["shares"])
                pnl = (sell_price - lot["price"]) * matched
                closed.append({
                    "code": code, "name": names[code],
                    "open_date": lot["date"], "close_date": e["date"],
                    "buy_price": round(lot["price"], 3), "sell_price": round(sell_price, 3),
                    "shares": matched,
                    "pnl": round(pnl, 2),
                    "return_pct": round((sell_price / lot["price"] - 1) * 100, 2),
                    "hold_days": _days_between(lot["date"], e["date"]),
                    "plan_stop": lot.get("plan_stop"),
                    "from_report": lot.get("from_report"),
                    # 止损纪律:计划了止损,却在跌破止损后才卖(卖价<止损)= 扛单
                    "stop_violated": bool(lot.get("plan_stop") is not None
                                          and sell_price < float(lot["plan_stop"])),
                })
                lot["shares"] -= matched; remain -= matched
                if lot["shares"] == 0:
                    lots[code].popleft()
            # remain>0 表示卖空/记录不全,忽略多余卖出
    positions = {}
    for code, dq in lots.items():
        shares = sum(l["shares"] for l in dq)
        if shares > 0:
            cost = sum(l["price"] * l["shares"] for l in dq) / shares
            positions[code] = {
                "code": code, "name": names[code], "shares": shares,
                "avg_cost": round(cost, 3),
                "earliest_open": min(l["date"] for l in dq),
                "plan_stop": dq[0].get("plan_stop"),
            }
    return closed, positions


def cmd_positions(args) -> None:
    _, positions = _fifo_match(_load())
    print(json.dumps({"open_positions": list(positions.values()),
                      "count": len(positions)}, ensure_ascii=False, indent=2))


def cmd_stats(args) -> None:
    events = _load()
    if args.since:
        events = [e for e in events if e.get("date", "") >= args.since]
    closed, positions = _fifo_match(events)
    if not closed:
        print(json.dumps({"note": "暂无已平仓交易", "open_positions": len(positions)},
                         ensure_ascii=False, indent=2))
        return
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    total_pnl = round(sum(c["pnl"] for c in closed), 2)
    avg_win = round(sum(c["pnl"] for c in wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(c["pnl"] for c in losses) / len(losses), 2) if losses else 0.0
    stats = {
        "since": args.since or "全部",
        "closed_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1),
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        # 盈亏比:平均盈利 / 平均亏损绝对值(>1 才是"截亏损、放盈利")
        "profit_factor": round(avg_win / abs(avg_loss), 2) if avg_loss else None,
        "avg_hold_days_win": round(sum(c["hold_days"] for c in wins) / len(wins), 1) if wins else None,
        "avg_hold_days_loss": round(sum(c["hold_days"] for c in losses) / len(losses), 1) if losses else None,
        "stop_violations": sum(1 for c in closed if c["stop_violated"]),
        "open_positions": len(positions),
        "trades": closed,
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="操盘台账:记录 + FIFO 盈亏/纪律统计")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="追加一个操作事件")
    a.add_argument("--date", default=date.today().isoformat())
    a.add_argument("--action", required=True, help="buy/add/trim/sell/watch")
    a.add_argument("--code", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--price", type=float)
    a.add_argument("--shares", type=int)
    a.add_argument("--reason", default="")
    a.add_argument("--from-report", dest="from_report", default="")
    a.add_argument("--plan-stop", dest="plan_stop", type=float)
    a.add_argument("--plan-target", dest="plan_target", type=float)
    a.add_argument("--note", default="")
    a.set_defaults(func=cmd_add)

    sp = sub.add_parser("positions", help="列出未平仓持仓")
    sp.set_defaults(func=cmd_positions)

    ss = sub.add_parser("stats", help="统计已平仓交易")
    ss.add_argument("--since", default="", help="起始日期 YYYY-MM-DD")
    ss.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
