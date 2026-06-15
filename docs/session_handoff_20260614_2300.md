# A股Agent系统 — 会话交接文档
**更新时间：2026-06-14 23:00**

---

## 一、项目位置与运行

```bash
cd /Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent

# 每日选股流水线（默认取最近交易日）
.venv/bin/python -m app.run run --no-notify

# 启动 Web UI（http://localhost:8000）
.venv/bin/python -m app.run web

# 指定日期回测
.venv/bin/python -m app.run run --date 20260605 --no-notify
```

---

## 二、所有优化完成情况（对标吴川体系）

| # | 优化点 | 状态 | 报告位置 |
|---|---|---|---|
| O1 | RSI_14 动量因子 | ✅ | 个股详情 |
| O2 | VWAP 偏离率 | ✅ | 个股详情 |
| O3 | 7日累计涨幅 | ✅ | 个股详情 |
| O4 | 市场置信度 + 情绪综合分 | ✅ | 大盘择时区块 |
| O5 | 隔夜风险总览表 | ✅ | 板块热度末尾 |
| O6 | 新闻-量化交叉验证 | ✅ | 主题催化区块 |
| O7 | 行情联动分析 | ✅ | 独立区块 |
| O8 | 概念板块接口 | ✅ 代码就绪 | 等VPN/服务器 |
| O9 | 板块连续趋势分 0~100 | ✅ | 隔夜风险表"趋势分"列 |
| O10 | 千股千评人气排名 | ⬜ 等服务器 | — |
| O11 | 庄家控盘比例 | ⬜ 等服务器 | — |
| O12 | 个股实时新闻风控 | ✅ | Bear Agent per-stock |
| O13 | 历史胜率标签 | ✅ 积累中 | 主题名称旁 |
| O14 | 情绪综合分 | ✅（同O4） | 大盘区块 |
| O15 | 个股持仓追踪 | ✅ | 报告末尾 + Web UI |

---

## 三、今日新增文件

```
app/
  history_tracker.py   O13 历史胜率 SQLite
  tracker.py           O15 持仓追踪 SQLite
  web/
    __init__.py
    main.py            FastAPI Web UI（报告列表/报告查看/持仓追踪）
    templates/
      base.html        暗色主题基础布局
      index.html       报告列表首页
      report.html      Markdown报告渲染页
      tracking.html    持仓追踪 + 历史胜率页

data_cache/
  history.db           SQLite（candidate_records + position_tracking）
```

---

## 四、关键机制说明

### O13 回填机制（已接入 run.py）
每次 `run run` 启动时，自动用今日价格回填上一交易日候选股的实际涨跌幅，积累后在主题旁显示：
```
**⚡ 军工**　热度 10.0/10　历史T+1胜率 63%(16次)
```

### O15 持仓追踪（全自动）
- 通过多空辩论的候选股自动加入追踪（保守买入价为基准）
- 每次运行时拉全市场日线补充非候选股的最新价格
- 触达止盈/止损自动标注，满 10 个交易日自动停止追踪

### crontab 定时任务（已配置）
```
5 16 * * 1-5  scripts/run_daily.sh  → logs/run_YYYYMMDD.log
```
每个工作日 16:05 自动运行，日志保留 30 天。

---

## 五、下一步优先级

| 优先级 | 任务 | 说明 |
|---|---|---|
| 1 | 部署国内服务器 | 解决 O8/O10/O11 VPN 限制，同时跑 crontab |
| 2 | 回测验证 | `app/backtest/engine.py` 已有脚手架，验证选股胜率 |
| 3 | Web UI 板块热度图表 | Chart.js 可视化 sector_stats，替代纯文本表格 |
| 4 | O13 胜率积累 | 每日运行后自动积累，约 2~3 周后有统计意义 |

---

## 六、已知问题

| 问题 | 严重性 | 说明 |
|---|---|---|
| 千股千评本地不可用 | 低 | 国内服务器自动生效 |
| 财联社Cookie有效期约2个月 | 中 | 到期重新登录更新 `.env` 的 `CLS_COOKIE` |
| 非交易日数据为空 | 低 | 正常现象，代码已加防空判断 |

---

*对应代码状态：2026-06-14 23:00*
