# 会话交接文档 — 2026-06-14
> 新窗口启动时，把本文件内容告诉 Claude，即可无缝继续开发

---

## 项目位置
```
/Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent/
```

## 当前完成状态

### ✅ Phase 0（完成）
骨架、配置、数据适配层（Tushare+Akshare）、LangGraph空图

### ✅ Phase 1（完成）
节点A市场择时（4态）、节点C选股线（五步过滤+振幅+走势摘要）、回测脚手架

### ✅ Phase 1.5（完成）
- VWAP计算（20日，作为保守买入价）
- 止损止盈自动计算（止损=MA5，止盈1=+5%，止盈2=+8%）
- 市场4态细化（衰退/弱势/退潮反抽/震荡/升温/主升）
- 次日盘前观察清单（09:30-09:40 T+1执行框架）
- 千股千评人气排名整合（Clash规则配置后已生效）

### ⬜ Phase 2（下一步，立即开始）
**板块热度评分 + 主题阶段识别（不需要LLM，纯量化）**

---

## Phase 2 立即要实现的内容

### 背景（从吴川AI回答中提取）

板块阶段判断条件：
- **升温(new)**：5日资金流入 > 3日资金（加速）+ 广度从低位扩张 + 连板高度开始抬升
- **趋势**：资金流入稳定 + 板块内站上MA20比例>30% + 最高连板>4板
- **退潮(decay)**：最高连板反复缩减 + 5日位置转负 + 资金净流出

板块热度公式（反向还原）：
```
热度 = 5日资金净流入(40%) + 涨停结构强度(30%) + 20日广度(20%) + 新闻权威性(10%，留Phase 3)
```

### 需要实现的文件

**1. `app/sector_analyzer.py`（新建）**
功能：
- `calc_sector_stats(trade_date, provider, close_m, vol_m)` → 对每个行业板块计算：
  - 5日/3日资金净流入（从 Tushare moneyflow 按行业聚合）
  - 板块内站上MA20的比例（广度）
  - 板块内涨停家数
  - 最高连板高度（consecutive_limit_high）
  - 综合热度分(0~100)
  - 阶段标签(new/趋势/decay)
- 输出：DataFrame，每行一个行业

数据来源：
- `provider.get_money_flow(trade_date)` → 个股资金流，按industry聚合
- `provider.get_stock_basic()` → ts_code→industry映射
- `close_m`（历史价格矩阵，已在选股节点加载）
- `provider.get_daily(trade_date)` → 当日涨跌幅，算涨停

**2. 连板高度追踪**
在 `app/nodes/a_market_gate.py` 中实现真实连板高度：
- 拉最近5个交易日的 daily 数据
- 对每只股票检查：是否连续N天涨幅≥9.5%
- 取最大的N值作为最高连板高度

**3. `app/nodes/b_theme_analysis.py` 真实实现**
- 调用 `sector_analyzer.calc_sector_stats()`
- 输出 Top 热度板块（热度>50分）
- 输出退潮预警板块（decay阶段）
- 写入 `state.themes`

**4. 报告节点升级**
在报告第二部分显示：
```
## 二、板块热度与阶段
| 板块 | 阶段 | 5日资金(亿) | 3日资金(亿) | 广度 | 连板高度 | 热度分 | 信号 |
| 半导体 | 趋势↗ | +560 | +150 | 34% | 4板 | 72 | ⚠️ 注意高位 |
| 稀土 | 升温🔥 | +89 | +47 | 28% | 2板 | 65 | 🔥 加速升温 |
| 汽车零部件 | 退潮📉 | -21 | -18 | 12% | 0板 | 18 | 📉 回避 |
```

---

## 关键数据字段（已验证可用）

### Tushare moneyflow 字段
```
ts_code, trade_date,
buy_elg_amount, sell_elg_amount,  # 超大单
buy_lg_amount, sell_lg_amount,    # 大单
buy_md_amount, sell_md_amount,    # 中单
buy_sm_amount, sell_sm_amount,    # 小单
net_mf_amount                     # 全部净流入
```

### Tushare daily 字段
```
ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount
```

### stock_basic 字段
```
ts_code, symbol, name, area, industry, market, list_date
```

### 千股千评字段（akshare，VPN Clash规则已配置）
```
代码, 名称, 最新价, 涨跌幅, 换手率, 市盈率, 主力成本,
机构参与度, 综合得分, 上升, 目前排名, 关注指数, 交易日
```

---

## 环境信息
- Python 虚拟环境：`.venv/bin/python`
- 运行命令：`cd astock-agent && .venv/bin/python -m app.run run --date 20260605 --no-notify`
- Tushare积分：5100分（有效期至2027-04-30）
- 推送：Server酱（.env已配置）
- VPN：Clash Verge，已配置eastmoney.com等直连规则

## 已知问题
- akshare东方财富接口：Clash规则已加，千股千评已可用，板块列表接口(concept/industry)待验证
- 连板高度目前用涨停家数粗估，Phase 2实现精确追踪

---

## 下一步指令

新窗口里说：
**"请读取 /Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent/docs/session_handoff_20260614.md，继续实现 Phase 2 板块热度评分"**

然后按以下顺序实现：
1. `app/sector_analyzer.py` — 板块热度评分核心
2. `app/nodes/a_market_gate.py` — 真实连板高度
3. `app/nodes/b_theme_analysis.py` — 真实实现
4. `app/nodes/e_report.py` — 板块热度区块
5. 运行验证：`python -m app.run run --date 20260605 --no-notify`
