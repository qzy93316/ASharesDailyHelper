# CLAUDE.md — 项目上下文(给 Claude Code 与贡献者)

A股投资分析助手:个人研究工具,Python + AKShare/DangInvest + ECharts。**仅供研究参考,不构成投资建议。**
新用户的"一句话触发"用法见 `README.md` 顶部「🚀 快速上手」。本文件给的是**做事的原则与约定**。

## 四条核心原则(改代码/加功能都要守)

1. **AI 零计算**:所有指标/评分/筹码/缠论/命中率由**代码**算,AI 只做**解读**。别把计算塞进提示词。
2. **数据韧性**:免费接口会限流/失效,一律走 `scripts/fetcher.py` 的三层兜底(当日缓存 → 实时主源→备源 → 陈旧缓存兜底)。东财限流是常态,备源(新浪/DangInvest)要能顶上。
3. **知识驱动**:领域知识(缠论、K线形态)蒸馏成 `knowledge/kb/` 下的**结构化文件**供代码调用,不塞记忆系统。
   - `kb/candlestick-patterns.json` 70 种K线形态(含可编程 `detection`);`kb/chan-rules.md` 缠论规则;`kb/raw/` 原始图(PDF文本层是CID乱码,靠看图蒸馏)。
4. **不报喜不报忧**:强制反方理由 + 硬规则否决(RSI6>80/乖离>5% 等直接不进池)。

## 安全边界(铁线,不可逾越)

- 本工具可能被用于跟踪**真实资金**持仓。**绝不接收/索取/使用**证券账户密码、交易密码、资金密码——发给 AI 或任何第三方都不行。
- **只读诊断可以,绝不代下单/代交易**。
- 持仓引入走**手动导出的持仓文件**(券商/同花顺导出的 代码/名称/数量/成本价 表,放 `portfolio/`),无需任何凭证。
- 若用户要"给账号密码自动同步",礼貌坚决拒绝,引导到 CSV 导入;坚持则只推荐券商官方只读接口(QMT/miniQMT)在其**自己电脑**跑、密码只存官方客户端。

## 数据层要点(易踩坑)

- **日K缓存按"收盘档期"新鲜度**(`cache.is_fresh_kline`):以收盘 15:05 分 pre/post 档。盘前抓的缓存到盘后自动失效、重拉当日收盘——否则**盘后复盘/诊断会拿到隔日旧价**(下跌日系统性报喜)。
- **盘中要实时的场景用 `get_kline(..., force_fresh=True)`**(如盘中机会池的现价/量比/换手),不吃当日盘前缓存。
- 全市场清单走 `cache.load_aged("universe_snapshot", ttl)` **周级缓存**,不每次重拉 5000 只。
- 本机沙箱外才能连行情源;Claude 沙箱连不上,涉及实时/联网的步骤须在**用户本机**跑。

## 复盘 = 两个维度,必须隔离

- 用户说「复盘/盘后复盘」→ 跑一键 `python scripts/run_review.py <上一交易日>`,**自动同时**出:
  - **荐股维度**(`review.py`,热点/全局/盘中三池):用今日结果检验**上一交易日**荐股决策对错(默认次日 hold=1;先触目标=胜/先触止损=败/未触线看当日涨幅),`stats` 出 by_pool 分池命中率。
  - **持仓维度**(`diagnose_portfolio.py`):按**用户的实际成本**做健康度诊断。
  - 合成暗色图文 HTML(`review_to_html.py`,复用 `report_to_html` 主题;**别用 `md_to_html` 渲复盘,表格会挤成一团**)。
- **两维度不可混**:荐股按其计划判对错(与持仓成本无关),持仓按成本判健康度。荐股常不立即买入,即使买了也分开看。
- **复盘不出作战方案**(作战方案留给次日盘前工作流结合最新消息面)。归档/复盘统一锚定"最近一个交易日的操作版报告"。

## 持仓诊断(v3.4)

`scripts/diagnose_portfolio.py` + `skills/portfolio-diagnosis/`,触发词「诊断持仓」(复盘时也会自动带上)。
读 `portfolio/` 持仓文件(同花顺"xls"其实是 **GBK 编码 Tab 分隔文本**,解析器已兼容 csv/真xlsx,自动识别中文列名)→ 逐只复用 `analyze.analyze_one` + 成本感知六档标签(止损/重亏警戒/逢反弹减/持有/持有观察/观察)+ 组合体检。

## 目录速览

`scripts/`(数据/分析/入口)· `skills/`(自建 skill:trading-memory/render-html/backtest-review/news-analysis/portfolio-diagnosis/action-plan/intraday-watch)· `knowledge/kb/`(领域知识)· `reports/`·`reviews/`·`trades/`·`portfolio/`(个人产出)· `data/market.db`(缓存)。
更全的版本演进见 `README.md` 的 Changelog。

## 验证

- `python scripts/selftest.py` 离线自测(不联网,指标+评分断言)。
- 改数据层/复盘/渲染后:自测 + 用 `reports/样例日报-离线演示数据.md` 的虚构数据核对渲染。
