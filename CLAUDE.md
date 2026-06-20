# astock-agent 项目约定

## 架构原则
- 所有数据访问通过 `CompositeProvider`（DataProvider 抽象接口），禁止上层直接调用 akshare/tushare
- 所有 LLM 调用通过 `LLMClient`，禁止直接调用 openai SDK
- 配置全部走 pydantic-settings + `.env`，禁止硬编码任何 key/token
- 每个 LangGraph 节点只做一件事，读写 PipelineState，依赖注入

## Tushare 积分说明
账号当前积分：**5100分**（非原规格文档中的500分），有效期至 2027-04-30。

## 数据源分工（以5100积分为准）

> ⚠️ **2026-06-18 实测校正**：5100 积分实际解锁范围**远超**官方积分表标注的档位。
> 经真实 API 探测，以下接口在 5100 分**均可用**（官方表标 6000/8000 档）：
> `moneyflow`(个股资金流) / `ths_index`+`ths_member`(同花顺概念) / `moneyflow_cnt_ths`(概念资金流) /
> `stk_factor`(技术因子) / `cyq_perf`(筹码分布) / `moneyflow_dc`(东财资金流) / `fina_indicator`(财务指标) /
> `top_list`(龙虎榜) / `moneyflow_hsgt`(北向)。**结论：当前无需购买积分**（除非要做"历史分钟数据"——那是单独权限 ¥2000/年，与积分无关）。

| 数据需求 | 实际数据源 | 接口 | 备注 |
|---|---|---|---|
| 全市场日线/复权/指数/每日指标 | ✅ Tushare | `daily`/`adj_factor`/`index_daily`/`daily_basic` | 120分 |
| 个股资金流 / 龙虎榜 / 北向 | ✅ Tushare | `moneyflow`/`top_list`/`moneyflow_hsgt` | 5100实测可用 |
| **同花顺概念列表/成分** | ✅ Tushare | `ths_index`(type=N)/`ths_member` | **生产口径**(theme_wide)；5100实测可用。已加垃圾黑名单 `_CONCEPT_DENY`(剔除指数成份/融资融券/沪股通/证金持股/昨日涨停等非题材) |
| **同花顺概念资金流** | ✅ Tushare | `moneyflow_cnt_ths` | concept_flow 页用 |
| **行业分类(全系统口径)** | ✅ Tushare | `index_classify`+`index_member_all`(申万SW2021) | **2026-06-20 升级**：`get_stock_basic().industry` 已覆盖为**申万一级**(31个·机构标准)，原Tushare值留 `industry_src`、申万二级在 `industry_l2`；映射失败优雅回退。所有按 industry 聚合的板块分析(行业资金/宽表/全景看板/广度雷达/同类回测/选股池)统一升级 |
| 行业资金流 | ✅ Tushare(聚合) | 按 `industry`(=申万一级) 聚合 `moneyflow` | industry_flow 页；非 Akshare |
| 财务指标(ROE/同比/负债率) | ✅ Tushare | `fina_indicator` | 2000分 |
| 筹码分布(成本/获利盘) | ✅ Tushare | `cyq_perf` | 5100实测可用；股性页用 |
| 单股区间日线/复权(回测) | ✅ Tushare | `daily`/`adj_factor` by ts_code | kline_loader |
| 全市场实时快照 | Akshare | `stock_zh_a_spot_em` | 盘中用 |
| 千股千评 | Akshare | `stock_comment_em` | 因子选股千评分 |
| 财联社电报/快讯 | Akshare | `stock_info_global_cls` | +Cookie |
| 个股新闻 | ⚠️ Akshare(本环境pyarrow报错) | `stock_news_em` | **已改用博查**(fundamentals 近期提示) |
| 东财概念板列表/成分 | ❌ **死代码(未调用)** | `stock_board_concept_name_em` | sector_analyzer.calc_concept_stats 未被引用，概念已走 Tushare ths |

> 已解锁但**尚未用上**的金矿（可增强实操，已付费）：`stk_factor`(现成MACD/KDJ/RSI/BOLL/CCI)、
> `moneyflow_dc`(更细资金口径)、`forecast`/`express`(业绩预告/快报)。

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
