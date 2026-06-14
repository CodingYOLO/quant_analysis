# astock-agent 项目约定

## 架构原则
- 所有数据访问通过 `CompositeProvider`（DataProvider 抽象接口），禁止上层直接调用 akshare/tushare
- 所有 LLM 调用通过 `LLMClient`，禁止直接调用 openai SDK
- 配置全部走 pydantic-settings + `.env`，禁止硬编码任何 key/token
- 每个 LangGraph 节点只做一件事，读写 PipelineState，依赖注入

## Tushare 积分说明
账号当前积分：**5100分**（非原规格文档中的500分），有效期至 2027-04-30。

## 数据源分工（以5100积分为准）

| 数据需求 | 所需积分 | 实际数据源 | 接口 |
|---|---|---|---|
| 全市场日线行情 | 120 | ✅ Tushare | `daily` |
| 股票基础信息/列表 | 120 | ✅ Tushare | `stock_basic` |
| 交易日历 | 120 | ✅ Tushare | `trade_cal` |
| 指数日线（大盘MA） | 120 | ✅ Tushare | `index_daily` |
| 个股资金流 | 2000 | ✅ Tushare | `moneyflow` |
| 龙虎榜明细 | 2000 | ✅ Tushare | `top_list` |
| 北向资金汇总 | 2000 | ✅ Tushare | `moneyflow_hsgt` |
| 同花顺行业资金流 | 5000 | ❌ 积分不足→ Akshare | `stock_sector_fund_flow_rank` |
| 全市场实时快照 | — | Akshare | `stock_zh_a_spot_em` |
| 概念板块列表/成分 | — | Akshare | `stock_board_concept_name_em` |
| 行业板块列表/成分 | — | Akshare | `stock_board_industry_name_em` |
| 千股千评 | — | Akshare | `stock_comment_em` |
| 财联社电报/快讯 | — | Akshare | `stock_info_global_cls` |
| 个股新闻 | — | Akshare | `stock_news_em` |

## 数据层纪律
- akshare 接口使用前必须有对应 `verify_*()` 通过真实运行
- 同一交易日同一接口只拉一次，缓存到 `data_cache/` 目录（parquet）
- akshare 调用间隔 >= 1.5s，失败重试 3 次指数退避

## LLM 成本分层
- 高频任务（新闻打分/字段抽取）：`task_type="flash"` → deepseek-chat
- 低频任务（综合报告/多空辩论）：`task_type="pro"` → deepseek-reasoner
- 每次运行结束打印 token 消耗与预估费用（`python -m app.run cost`）

## 分阶段状态
- Phase 0 ✅ 骨架 + 数据适配层 + LangGraph空图
- Phase 1 ✅ 选股线（吴川三层过滤 + 量化因子 + 回测脚手架）
- Phase 1.5 ✅ 交易执行升级（VWAP/止损止盈/4态市场/次日观察清单/千股千评）
- Phase 2 ⬜ 情绪/主题线（新闻 + LLM + 概念映射）
- Phase 3 ⬜ 风控 + 多空辩论
- Phase 4 ⬜ 完整报告 + 研报RAG + Docker部署

## 选股线因子说明（Phase 1，吴川体系三层）
**第一层 - 市场择时（节点A）**
- 涨停≥80 + 跌停<15 + 下跌家数<2000 + MA5占比>55% → 强势(仓位0.6)
- 下跌家数>3000 或 跌停>30 → 弱势(不开仓)
- 其余 → 震荡(仓位0.3)

**第三层 - 个股因子（节点C，五步流水线）**
1. 基础：市值200-5000亿、非ST、成交额≥1亿、未跌停
2. 趋势：收盘>MA20（硬过滤）、MA20斜率向上
3. 量价：换手率1%-15%、MACD金叉 或 缩量回踩MA20(得分≥50)
4. 资金：超大单+大单净流入（影响评分）
5. 强弱：RPS50≥70（近50日跑赢70%个股）

**评分权重**：RPS强度30 + 主力资金25 + 技术形态25 + 均线结构10 + 加分项10

## 选股参数约定
- **市值下限：200亿**（过滤小微盘，避免流动性坑）
- **市值上限：5000亿**（不设过严上限，大盘龙头也纳入）
- **优选区间：500亿以上**（流动性好、机构覆盖足，风险可控）
- 以上为默认值，可在 `.env` 的 `MIN_MARKET_CAP` / `MAX_MARKET_CAP` 覆盖

## 禁止事项
- 禁止 LLM 输出"胜率/成功率"并据此排序
- 禁止 mock 数据冒充真实数据
- 禁止接入任何下单/交易接口
- 禁止一次把所有 Phase 写完
