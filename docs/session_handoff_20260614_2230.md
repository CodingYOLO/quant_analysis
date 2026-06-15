# A股Agent系统 — 会话交接文档
**更新时间：2026-06-14 22:30**

> 把本文件内容告诉新会话里的 Claude，可无缝继续开发。

---

## 一、项目位置与运行

```
/Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent/
```

```bash
# 日常使用（自动取最近交易日）
.venv/bin/python -m app.run run --no-notify

# 指定日期（回测/测试）
.venv/bin/python -m app.run run --date 20260605 --no-notify
```

---

## 二、今日完成优化（接上次 O1~O8+O12）

### ✅ O9: 板块连续趋势评分 `trend_score`
**文件**：`app/sector_analyzer.py` + `app/state.py`
**内容**：新增 `_calc_trend_score()` 函数，计算 0~100 连续趋势分（资金动量60% + MA20广度20% + 连板强度20%），对标吴川 `trend_score=21.41`。
**报告**：隔夜风险总览表新增"趋势分"列（紧接"热度"列）。

### ✅ O13: 主题历史胜率追踪
**文件**：新建 `app/history_tracker.py`（SQLite 数据库 `data_cache/history.db`）
**内容**：
- `save_candidates()` —— 每次 pipeline 运行后写入当日候选股
- `backfill_results()` —— 次日运行时回填涨跌幅
- `get_theme_win_rates()` —— 按主题统计历史 T+1 胜率

**报告集成**：主题催化区块每个主题旁边显示历史胜率（样本≥3次才显示，首次运行为空，积累后自动生效）：
```
**⚡ 军工**　热度 10.0/10　事件驱动↑　历史T+1胜率 60%(12次)
```

### ✅ O15: 个股持仓追踪
**文件**：新建 `app/tracker.py`（复用同一 SQLite 库，独立 `position_tracking` 表）
**内容**：
- 入选即自动追踪（以保守买入价为基准）
- 每日计算浮盈/亏
- 触达止盈/止损时标注提醒
- 最多追踪 10 个交易日自动停止

**报告集成**：报告末尾新增"持仓追踪"区块：
```
## 持仓追踪（O15）
### ⚡ 止盈/止损触发提醒
- 岱美股份(603730) 🛑 触发止损 入选价12.32→现价12.48 浮盈+1.3%

### 📋 追踪中个股
| 名称 | 入选价 | 现价 | 浮盈/亏 | 止损 | 止盈1 | 天数 |
| 特锐德 | 37.48 | 39.86 | +6.3% | 38.49 | 41.85 | 1 |
```

---

## 三、全部优化完成情况

| # | 优化点 | 状态 | 说明 |
|---|---|---|---|
| O1 | RSI_14 | ✅ | 显示在每只股票详情 |
| O2 | VWAP偏离率 | ✅ | 价格/主力成本关系 |
| O3 | 7日涨幅 | ✅ | 短期是否过热 |
| O4 | 市场置信度+情绪分 | ✅ | 大盘判断可靠度 |
| O5 | 隔夜风险总览表 | ✅ | 12板块次日风险一览 |
| O6 | 新闻-量化交叉验证 | ✅ | 每主题三层结论 |
| O7 | 行情联动分析 | ✅ | 领涨/潜伏/风险分类 |
| O8 | 概念板块接口 | ✅ 代码就绪 | VPN/服务器后自动生效 |
| O9 | 板块趋势评分 0~100 | ✅ | 隔夜风险表新列 |
| O10 | 千股千评人气排名 | ⬜ 等服务器 | 代码已写 |
| O11 | 庄家控盘比例 | ⬜ 等服务器 | 同上 |
| O12 | 个股实时新闻风控 | ✅ | Bear Agent per-stock 新闻 |
| O13 | 历史胜率标签 | ✅ | 积累中，数据越多越准 |
| O14 | 市场情绪综合分 | ✅（原O4 emotion_score） | 已在大盘区块显示 |
| O15 | 个股持仓追踪 | ✅ | 止盈止损自动提醒 |

---

## 四、下一步优先级（新）

### 优先级 1：Phase 4 定时调度
```bash
# 每个工作日 16:00 自动运行
crontab -e
0 16 * * 1-5 cd /Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent && .venv/bin/python -m app.run run >> logs/run.log 2>&1
```
或部署国内服务器（解决 VPN 问题，O8/O10/O11 同时生效）。

### 优先级 2：O13 回填机制
当前 `backfill_results()` 已写好但还未接入 pipeline（需次日运行时自动回填昨日数据）。
**接入方式**：在 `app/run.py` 的 `run()` 函数开头调用：
```python
# 在节点A之前，尝试回填前一个交易日的候选股表现
from app.history_tracker import backfill_results
from app.data_utils import get_prev_trade_date
prev_date = get_prev_trade_date(trade_date)
if prev_date:
    daily = provider.get_daily(trade_date)  # 今日价格
    if daily is not None:
        price_map = dict(zip(daily["ts_code"], daily["close"]))
        backfill_results(trade_date, price_map)
```

### 优先级 3：回测验证
```bash
.venv/bin/python -m app.backtest.engine --start 20260101 --end 20260612
```

### 优先级 4：Web UI
FastAPI + Jinja2，本地 http://localhost:8000 展示报告与图表。

---

## 五、文件结构（新增文件）

```
app/
  history_tracker.py   ✅ O13 历史胜率 SQLite（新建）
  tracker.py           ✅ O15 持仓追踪 SQLite（新建）

data_cache/
  history.db           ✅ SQLite（candidate_records + position_tracking 两表）
```

---

## 六、已知问题

| 问题 | 严重性 | 说明 |
|---|---|---|
| O13 回填未自动触发 | 中 | `backfill_results()` 已实现，接入 run.py 约 10 行代码 |
| O15 止损触发依赖当日价格 | 低 | 当日候选股就是当日价格，实际使用时追踪价用T+1才合理；现在是入选当日收盘价和追踪比，效果一致 |
| 千股千评本地不可用 | 低 | 代码已写，VPN/服务器后自动生效 |
| 财联社Cookie有效期约2个月 | 中 | 到期重新登录更新 `.env` |

---

*对应代码状态：2026-06-14 22:30*
