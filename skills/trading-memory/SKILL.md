---
name: trading-memory
description: A股个人操盘记忆 —— 记录每日买卖/持仓/复盘,并统计胜率、盈亏、止损纪律。Use when 用户说"记一笔/我买了/我卖了/加仓/减仓/记录一下操作/复盘/看我的操盘记录/诊断持仓/我的持仓怎么样"，或每日盘后想沉淀交易与心得时。
---

# 操盘记忆(Trading Memory)

把用户的每一笔真实操作、持仓变化和复盘心得沉淀成结构化台账,让后续分析能**基于用户自己的交易历史**给建议——而不是每次从零开始。这是设计文档二期「持仓诊断」的数据底座。

## 何时触发

- 用户报告操作:"我买了 XX""卖了一半""XX 止损出了""加仓 YY"→ 记一笔
- 用户想复盘:"复盘一下""这周做得怎么样""看我的操盘记录"→ 出统计 + 解读
- 用户问持仓:"我的持仓""诊断持仓""还拿着的那几只怎么样"→ 列未平仓 + 结合当日评分给建议
- 每日盘后主动提醒沉淀(可选)

## 能力边界

- 只做记录、统计与复盘解读,**不下单、不承诺收益、不替代投顾**
- 所有盈亏/胜率/持仓天数由脚本计算(AI 零计算原则),AI 只负责解读与给纪律建议
- 数据全部存本地 `trades/`,不上传

## 数据存储

- `trades/ledger.jsonl` —— 事件流台账,一行一个事件(buy/add/trim/sell/watch)
- `trades/reviews/复盘-YYYY-MM-DD.md` —— 复盘随笔(可选,自由文本 + 心得)

事件字段(脚本会校验):

| 字段 | 必填 | 说明 |
|---|---|---|
| `date` | ✓ | 操作日期 YYYY-MM-DD |
| `action` | ✓ | buy 买入 / add 加仓 / trim 减仓 / sell 清仓卖出 / watch 只观察不建仓 |
| `code` `name` | ✓ | 6 位代码 + 名称 |
| `price` | 买卖必填 | 成交价 |
| `shares` | 买卖必填 | 股数(正整数;trim/sell 为卖出股数) |
| `reason` | 建议 | 操作理由(题材/技术信号/消息面) |
| `from_report` | 可选 | 来自哪份日报的荐股,如 `日报-2026-07-04.md` |
| `plan_stop` `plan_target` | 可选 | 计划止损/目标价(用于事后检验纪律) |
| `note` | 可选 | 情绪/纪律心得 |

## 用法(脚本)

脚本:`skills/trading-memory/scripts/ledger.py`(纯标准库,无需安装依赖)

```bash
# 记一笔买入(来自今天的荐股)
python skills/trading-memory/scripts/ledger.py add \
  --date 2026-07-04 --action buy --code 002430 --name 杭氧股份 \
  --price 27.49 --shares 500 --reason "化工机械板块领涨,多头排列" \
  --from-report 日报-2026-07-04.md --plan-stop 27.6 --plan-target 32.31

# 减仓 / 清仓
python .../ledger.py add --date 2026-07-08 --action trim --code 002430 --name 杭氧股份 --price 30.1 --shares 200
python .../ledger.py add --date 2026-07-10 --action sell --code 002430 --name 杭氧股份 --price 26.9 --shares 300 --note "跌破MA10按纪律出"

# 查看未平仓持仓
python .../ledger.py positions

# 统计复盘(胜率/盈亏/持仓天数/止损纪律)
python .../ledger.py stats --since 2026-01-01
```

## 复盘解读要点(AI 拿到 stats 后怎么说)

脚本输出结构化 JSON;AI 据此按以下维度**解读**,不复述数字:

1. **胜率与盈亏比**:胜率高但盈亏比<1(赚小亏大)= 拿不住盈利、止损太晚,是散户头号病
2. **止损纪律**:`stop_violations`(实际卖价低于计划止损才出的次数)>0 → 指出"扛单"习惯
3. **持仓天数分布**:盈利单平均持仓 < 亏损单平均持仓 = "截断利润、让亏损奔跑",反纪律
4. **来源有效性**:按 `from_report` 归因,看系统荐股的真实兑现率(为策略迭代提供依据)
5. 给 **1~2 条可执行的下一步纪律建议**,不空谈

## 与其他能力的联动

- 记一笔时,若 `from_report` 指向某日报,复盘可回溯当时的评分/反方理由,检验"当初的逻辑是否兑现"
- "诊断持仓" = `positions` 列未平仓 + 对每只跑一次当日技术面(`daily_report` 的评分逻辑)+ 给加/减/守建议
- 复盘随笔可交给 `render-html` skill 转成好读的 HTML 归档
