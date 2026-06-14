# A股Agent系统实现计划
> 创建时间：2026-06-14 15:01  
> 基于：吴川体系两份markdown文档的反向工程分析

---

## 核心结论（来自吴川回测数据）

**回测揭示的真相：纯技术策略几乎全部负期望**

| 策略 | T+1胜率 | T+1均收益 | 是否可用 |
|---|---|---|---|
| 事件动量（新闻+动量）| 52.11% | +0.75% | ✅ 唯一正期望 |
| 低位吸筹 | 45.18% | -0.11% | ❌ |
| 趋势突破 | 44.96% | -0.05% | ❌ |
| 回踩反转 | 41.63% | -0.23% | ❌ |
| 资金跟随 | 42.77% | -0.27% | ❌ |

**结论：Phase 2（新闻主题线）是系统的核心，不是可选项。**

---

## 当前完成状态

### ✅ Phase 0（已完成）
- 项目骨架、配置层、DataProvider适配层
- LLMClient（带费用日志）
- LangGraph空图（5节点占位）
- 数据验证：Tushare 7个接口全通过

### ✅ Phase 1（已完成）
- 节点A（市场择时）：3态判断（强势/震荡/弱势）
- 节点C（选股线）：五步过滤（市值→均线→量价→资金→RPS）
- 振幅过滤（近5日均振幅≥3%）
- 个股走势摘要（替代分时图）
- 回测脚手架（backtest/engine.py）

### ⬜ Phase 1.5（下一步，待实现）
吴川文档分析后的补充升级，不需要LLM即可实现

### ⬜ Phase 2（待实现）
主题/情绪线，需要LLM

### ⬜ Phase 3（待实现）
风控+多空辩论

### ⬜ Phase 4（待实现）
完整报告+研报RAG+Docker部署

---

## Phase 1.5 实现计划（优先级排序）

> 所有升级都不需要LLM，纯量化实现，数据来自已验证的Tushare接口

### 任务1：止损止盈价格自动计算 ⭐⭐⭐
**优先级：最高（对实际交易最直接有用）**

实现内容：
- 止盈1（+5%）= 收盘价 × 1.05
- 止盈2（+8%）= 收盘价 × 1.08
- 止损（个股级）= MA5价格（跌破MA5止损）
- 保守买入价 = 20日VWAP（主力平均成本）
- 激进买入价 = 当日收盘价
- 单票仓位建议 = 根据市场状态：强势5%，震荡3%，弱势0%

修改文件：
- `app/factors.py` → 新增 `calc_vwap()`, `calc_stop_loss_price()`
- `app/state.py` → Candidate 新增 `stop_loss`, `take_profit_1`, `take_profit_2`, `buy_zone_conservative`, `buy_zone_aggressive`, `position_pct`
- `app/nodes/c_stock_selection.py` → 在build_candidate_objects中填充上述字段
- `app/nodes/e_report.py` → 报告新增"操作参考"区块

输出样例：
```
| 股票 | 保守买入 | 激进买入 | 止损价 | 止盈1(+5%) | 止盈2(+8%) | 建议仓位 |
| 北方华创 | 638.89 | 667.19 | MA5 | 700.55 | 720.57 | 3% |
```

---

### 任务2：次日盘前观察清单自动生成 ⭐⭐⭐
**优先级：最高（每日必用）**

实现内容（吴川体系的T+1执行框架）：
- 对每只候选股生成具体的"明日09:30-09:40观察条件"
- 条件1：开盘价范围（允许高开幅度，超过则等回踩）
- 条件2：前15分钟成交额参考值（=今日成交额/交易分钟数×15）
- 条件3：失效条件（低开多少%放弃）
- 条件4：板块联动验证（同板块龙头代码列表）

修改文件：
- 新增 `app/nodes/c_stock_selection.py` → `generate_execution_plan()`
- `app/state.py` → Candidate 新增 `execution_plan: str`
- `app/nodes/e_report.py` → 每只股票下面输出执行清单

---

### 任务3：市场状态细化为4态 ⭐⭐
**优先级：高**

吴川体系4态（比我们现在的3态更精确）：
- **升温期**：站上MA5>60% + 涨停>100 + 连板>6板
- **主升期**：站上MA5>70% + 成交额持续放大
- **退潮反抽**：站上MA5 30-50% + 连板只有4板 + 反弹连续性不确认
- **衰退期**：站上MA5<20% + 跌停>50

仓位对应：升温0.5，主升0.6，退潮反抽0.2，衰退0.0

修改文件：
- `app/nodes/a_market_gate.py` → `_determine_regime()` 改为4态
- `app/state.py` → MarketRegime 新增 `phase_confidence: float`

---

### 任务4：千股千评人气排名整合 ⭐⭐
**优先级：高（数据已验证可用）**

千股千评已有字段（已验证）：
- `综合得分` → 情绪热度
- `目前排名` → 人气排名（越小越热门）
- `机构参与度` → 机构关注度
- `主力成本` → 主力持仓成本（类似庄家成本）
- `关注指数` → 关注热度

实现内容：
- 人气排名 < 500 → 加分项（热门股）
- 综合得分 > 60 → 情绪偏热
- 3日人气排名改善（需要存历史排名或当日就用当天排名趋势替代）

修改文件：
- `app/data/composite_provider.py` → 确认 `get_stock_comment()` 字段映射
- `app/nodes/c_stock_selection.py` → 在因子里加入人气排名和综合得分
- `app/factors.py` → 新增 `popularity_score()`

注意：千股千评目前因VPN问题在本地无法访问，部署到服务器后自动生效。代码先写好，不影响其他因子。

---

### 任务5：板块阶段识别 ⭐⭐
**优先级：中（Phase 2的前置准备）**

板块阶段用于过滤候选股（decay板块的股票降权或排除）：
- 板块趋势评分 = 板块内个股均RPS
- 板块广度 = 站上MA20的股票占比
- 板块阶段 = new/趋势/decay（根据趋势评分变化率）
- 人气集中度 = top3股票成交额/板块总成交额（>0.2=拥挤）

数据来源：
- 板块成分股 → akshare `stock_board_industry_cons_em`（VPN问题待解决）
- 板块内个股日线 → Tushare daily（已可用）

实现文件：
- 新增 `app/sector_analyzer.py`
- `app/nodes/c_stock_selection.py` → 加入板块阶段过滤

---

### 任务6：VWAP计算 ⭐
**优先级：中（已被任务1依赖）**

VWAP（成交量加权平均价）= Σ(价格×成交量) / Σ(成交量)

实现内容：
- 20日VWAP = 近20日(close×vol)之和 / 近20日vol之和
- VWAP位置 = (当前价-VWAP)/VWAP（正值=价格在主力成本上方）

修改文件：
- `app/factors.py` → 新增 `calc_vwap(close, vol, n=20)`

---

## Phase 2 实现计划

### 核心：事件动量策略（唯一正期望策略）

**数据流：**
```
财联社新闻/快讯
    ↓ DeepSeek-flash 批量打标
新闻→主题映射（半导体/稀土/低空经济等）
    ↓
板块热度评分 + 阶段判断（new/趋势/decay）
    ↓
主题→概念代码映射（akshare板块列表）
    ↓
主题内个股筛选（在Phase 1选出的候选股中过滤）
    ↓
事件动量加分（有明确新闻催化的股票额外加20分）
```

**板块热度评分公式（吴川体系）：**
```
热度 = 新闻数量×权重 + 资金流入×权重 + 价格涨幅×权重
阶段判断：
  - 3日热度变化>+30 → new（新启动）
  - 热度>50 且 变化稳定 → 趋势
  - 3日热度变化<-20 → decay（退潮）
```

**LLM任务分配：**
- DeepSeek-flash：新闻→主题打标（批量，每次20条）
- DeepSeek-pro：综合报告生成（每日1次）

实现文件：
- `app/nodes/b_theme_analysis.py` → 真实实现
- `app/llm/prompts/theme_scoring.txt` → 主题打标prompt
- `app/llm/prompts/report_generation.txt` → 报告生成prompt

---

## Phase 3 实现计划

### 风控硬规则（否决优先于LLM）

**个股级硬规则（空头一票否决）：**
1. 名称含ST/*ST → 直接排除（已在Phase 1实现）
2. 商誉/净资产 > 30% → 排除（需财报数据）
3. 大股东减持公告期内 → 排除（需公告数据）
4. 近30日有监管问询函 → 排除（需公告数据）
5. 当日股价异动（涨幅>15%）→ 排除（可能是利好兑现顶部）

**题材级风控：**
- 所属板块处于decay阶段 → 候选降权（不排除，但评分-20）
- 板块人气集中度>0.2 → 拥挤警告

**多空辩论（LLM参与）：**
- 多头agent：基于量化因子找支撑论据
- 空头agent：基于风险因子找否决理由
- 首席风控：硬规则优先于LLM观点

实现文件：
- `app/nodes/d_risk_debate.py` → 真实实现
- `app/llm/prompts/bull_agent.txt`
- `app/llm/prompts/bear_agent.txt`

---

## Phase 4 实现计划

### 报告完善
- 报告格式对标吴川输出（含隔夜风险总览表）
- Server酱推送（已实现）
- 同时输出本地MD文件（已实现）

### 定时调度
```bash
# crontab示例
0 16 * * 1-5 cd /app && python -m app.run run  # 每个交易日16:00盘后运行
```

### Docker化
- Dockerfile
- docker-compose.yml
- 环境变量通过.env传入

---

## 关键数据字段映射表

> 从吴川文档中提取，用于实现时对齐字段名

| 吴川字段 | 我们的数据来源 | 字段映射 |
|---|---|---|
| 站上5日线比例 | Tushare daily + 历史 | 自己算 |
| 涨停家数 | Tushare daily pct_chg≥9.5% | 自己算 |
| 连板高度 | Tushare daily 多日 | Phase 2实现 |
| 成交额 | Tushare daily amount | 已有 |
| 资金流_1日 | Tushare moneyflow net_mf_amount | 已有 |
| 资金流_3日合计 | Tushare moneyflow 3日累加 | 需实现 |
| 人气最新排名 | akshare stock_comment_em 目前排名 | VPN问题待解 |
| 人气排名改善_3日 | akshare stock_comment_em | 需存历史 |
| 庄家控盘 | akshare stock_comment_em | VPN待解 |
| RSI_14 | Tushare daily 计算 | 已在factors.py |
| VWAP位置 | Tushare daily 计算 | 待实现 |
| 倍量信号 | Tushare daily vol | 量比>2.0 |
| change_pct_7d | Tushare daily | 需计算 |
| 板块趋势评分 | akshare 板块+daily | 待实现 |
| 板块广度 | akshare 板块成分+daily | 待实现 |
| 次日风险惩罚 | 统计历史数据 | Phase 2 |
| 保守买入价 | 20日VWAP | 待实现 |
| 激进买入价 | 当日收盘价 | 已有 |
| 止损价 | MA5 | 已在factors.py |

---

## 文件结构（完整版）

```
astock-agent/
  app/
    config.py              ✅ 完成
    state.py               ✅ 完成（Phase 1.5需补充字段）
    graph.py               ✅ 完成
    run.py                 ✅ 完成
    factors.py             ✅ 完成（Phase 1.5需补充VWAP/止损）
    pattern_summary.py     ✅ 完成
    sector_analyzer.py     ⬜ Phase 1.5 任务5
    data/
      provider.py          ✅ 完成
      tushare_provider.py  ✅ 完成
      akshare_provider.py  ✅ 完成（VPN问题待解）
      composite_provider.py ✅ 完成
      cache.py             ✅ 完成
      history_loader.py    ✅ 完成
      verify.py            ✅ 完成
    llm/
      client.py            ✅ 完成
      prompts/
        theme_scoring.txt  ⬜ Phase 2
        report_generation.txt ⬜ Phase 2
        bull_agent.txt     ⬜ Phase 3
        bear_agent.txt     ⬜ Phase 3
    nodes/
      a_market_gate.py     ✅ 完成（Phase 1.5需升级4态）
      b_theme_analysis.py  ⬜ Phase 2
      c_stock_selection.py ✅ 完成（Phase 1.5需补充VWAP/仓位/执行计划）
      d_risk_debate.py     ⬜ Phase 3
      e_report.py          ✅ 完成（Phase 1.5需补充止损止盈展示）
    notify/
      notifier.py          ✅ 完成
    backtest/
      engine.py            ✅ 完成
  docs/
    implementation_plan_20260614_1501.md  ← 本文件
  tests/                   ⬜ 待补充
  data_cache/              ✅ 运行中（已有缓存数据）
  reports/                 ✅ 已生成报告
  CLAUDE.md                ✅ 完成
  .env                     ✅ 已配置
  README.md                ✅ 完成
```

---

## 执行顺序（建议）

```
立即执行（Phase 1.5）：
  任务6: VWAP计算          → factors.py
  任务1: 止损止盈价格      → factors.py + state.py + c_stock_selection.py + e_report.py
  任务2: 次日观察清单      → c_stock_selection.py + e_report.py
  任务3: 市场4态细化       → a_market_gate.py
  任务4: 千股千评整合      → composite_provider.py + c_stock_selection.py（代码先写好）
  任务5: 板块阶段识别      → sector_analyzer.py（依赖akshare，VPN解决后生效）

之后执行（Phase 2）：
  财联社新闻抓取 + DeepSeek打标 → b_theme_analysis.py
  主题→概念代码映射
  事件动量加分

之后执行（Phase 3）：
  硬规则风控 → d_risk_debate.py
  多空辩论LLM

最后（Phase 4）：
  报告美化 + 定时调度 + Docker
```

---

## 当前已知问题

| 问题 | 严重程度 | 解决方案 |
|---|---|---|
| akshare东方财富接口在本地VPN下不通 | 中 | 部署到国内服务器后自动解决；本地开发用Tushare替代 |
| stock_basic未返回circ_mv字段 | 低 | 已用daily_basic的circ_mv替代 |
| 连板高度目前用涨停家数粗估 | 低 | Phase 2实现精确追踪 |
| 回测验证未实际运行 | 中 | 需手动运行 python -m app.backtest.engine |
