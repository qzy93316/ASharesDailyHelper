# -*- coding: utf-8 -*-
"""扩充术语库 glossary.json —— 把 K线形态库(candlestick-patterns.json)里全部形态名
自动并入术语库,补齐报告里 K线形态 的 hover 解释;并把渲染器记录的"未命中术语"
(glossary_misses.json)打印出来,提示需要人工/AI 补充的缺口。

设计:candlestick-patterns.json 已含每个形态的 name_cn/description/signal/category,
可直接转成 term/plain/usage,无需再唤起 AI。运行时缺失(如新指标名)则记录到 misses,
由后续补充 —— 这就是"发现缺失→补充→缓存"的可持续闭环(静态HTML无法浏览器端实时生成)。

用法:python scripts/expand_glossary.py
"""
import json
from pathlib import Path

KB = Path(__file__).parent.parent / "knowledge" / "kb"
GLOSSARY = KB / "glossary.json"
PATTERNS = KB / "candlestick-patterns.json"
MISSES = KB / "glossary_misses.json"

CAT_CN = {"reversal_top": "见顶反转", "reversal_bottom": "见底反转",
          "continuation_up": "上升持续", "continuation_down": "下降持续",
          "indecision": "变盘/中性"}
BIAS_CN = {"bull": "看涨", "bear": "看跌", "neutral": "中性"}


def main() -> None:
    gl = json.loads(GLOSSARY.read_text(encoding="utf-8")) if GLOSSARY.exists() else {}
    pats = json.loads(PATTERNS.read_text(encoding="utf-8")) if PATTERNS.exists() else []
    added = 0
    for p in pats:
        name = p.get("name_cn")
        if not name or name in gl:
            continue
        cat = CAT_CN.get(p.get("category", ""), p.get("category", ""))
        bias = BIAS_CN.get(p.get("bias", ""), "")
        gl[name] = {
            "term": name,
            "plain": (p.get("description") or "").strip() or f"一种{cat}的K线组合形态。",
            "example": f"属「{cat}」类形态,{bias}信号;{p.get('signal','')}".strip(";， "),
            "usage": f"{p.get('signal','')}(可靠度{p.get('reliability','中')});出现在相应趋势位置才有效,需结合量能/均线确认。",
            "level": "进阶",
        }
        added += 1
    GLOSSARY.write_text(json.dumps(gl, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"术语库合并完成:新增 {added} 条(K线形态),现共 {len(gl)} 条 → {GLOSSARY.name}")

    if MISSES.exists():
        miss = json.loads(MISSES.read_text(encoding="utf-8"))
        pending = sorted(set(m for m in miss if m not in gl))
        if pending:
            print(f"\n⚠️ 渲染时仍未命中的术语 {len(pending)} 个(建议补充):")
            for m in pending:
                print("  -", m)
        else:
            print("渲染记录的术语均已覆盖。")


if __name__ == "__main__":
    main()
