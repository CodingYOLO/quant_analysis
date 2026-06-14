# 会话交接文档 — 2026-06-14 17:07
> 新窗口启动时，把本文件内容告诉 Claude，即可无缝继续开发

---

## 项目位置
```
/Users/vivianjin/Documents/quant/quant_stock_analysis/astock-agent/
```

## 运行命令
```bash
cd astock-agent
# 自动取最近交易日（推荐）
.venv/bin/python -m app.run run --no-notify

# 指定日期
.venv/bin/python -m app.run run --date 20260605 --no-notify
```

---

## 当前完成状态

### ✅ Phase 0（完成）
骨架、配置、数据适配层（Tushare+Akshare）、LangGraph空图

### ✅ Phase 1（完成）
节点A市场择时（6态）、节点C选股线（五步过滤+振幅+走势摘要）、回测脚手架
VWAP/止损止盈/次日盘前执行清单/千股千评整合

### ✅ Phase 1.5（完成）
- VWAP计算（20日，保守买入价）
- 止损止盈自动计算（止损=MA5，止盈1=+5%，止盈2=+8%）
- 市场6态细化（衰退/弱势/退潮反抽/震荡/升温/主升）
- 次日盘前观察清单（09:30-09:40 T+1执行框架）

### ✅ Phase 2（完成）
**板块热度 + 新闻主题 + 主题个股关联（20步中的1-10步）**

已实现内容：
- 量化板块热度评分（5日资金净流入+涨停结构+MA20广度）
- `pop_concentration` 人气集中度（板块拥挤度检测）
- `heat_score_delta_3d` 3日热度变化量（需历史缓存，首次运行为0）
- `nextday_risk_penalty` 次日风险惩罚分（退潮+拥挤+资金外流）
- 分层决策 buy/watch/avoid + decision_score（0-100）
- 东方财富财经快讯（60条/次）→ DeepSeek-flash 批量打标
- 主题热度聚合（name/heat/phase/evidence）
- 主题行业映射（_THEME_TO_INDUSTRY_KEYWORDS字典）
- **步骤10：主题龙头股关联**（热门主题→Top3龙头股，含涨跌/RPS/主力资金）
- 弱势市场依然输出候选股（观察模式，仓位建议0%）
- 自动识别最近交易日（周末/节假日自动回退）
- 报告标题清晰区分"数据交易日"和"报告生成时间"

### ✅ Phase 3（完成）
**风控 + 多空辩论（步骤16-19）**

已实现内容：
- 硬规则风控：ST/退市/涨幅>15%/市值<20亿 → 一票否决（不经LLM）
- 板块退潮阶段候选股自动标记风险
- 空头Agent（DeepSeek-flash）：识别风险点，判断是否软性否决
- 多头Agent（DeepSeek-flash）：3条做多理由
- 首席风控：硬规则 > 空头否决 > 多头论点
- 否决股票从候选池移除，保留在debate记录中
- 报告个股详情区块显示多空观点

### ⬜ Phase 4（下一步）
- 报告美化（可选Web UI）
- 定时调度（crontab 每个交易日16:00）
- Docker化部署
- 历史经验RAG（步骤13）

---

## 已知问题与限制

| 问题 | 状态 | 说明 |
|---|---|---|
| 财联社新闻接口 | ⚠️ 不可用 | 官方API需JS签名，已改用东方财富60条替代 |
| 财联社Cookie已配置 | ✅ | `.env` CLS_COOKIE 已填入，但API端点被锁 |
| `heat_score_delta_3d` 首次为0 | ✅ 正常 | 每日运行后自动积累缓存，次日起有真实delta |
| 千股千评 | ⚠️ 本地VPN限制 | 代码已写好，部署服务器后自动生效 |

---

## 关键文件变更（今日）

```
app/state.py              → 新增 ThemeLeader, SectorStat新字段(pop/delta/risk/decision)
app/config.py             → 新增 cls_cookie 配置项
app/sector_analyzer.py    → 完整重写：pop_concentration/delta/nextday_risk/decision
app/nodes/a_market_gate.py → 精确连板高度（真实多日数据）
app/nodes/b_theme_analysis.py → 完整实现：量化+LLM+龙头股关联
app/nodes/c_stock_selection.py → 事件催化加分(+20)、弱势不跳过选股
app/nodes/d_risk_debate.py → Phase 3完整实现
app/nodes/e_report.py     → 全面升级：决策表/多空观点/弱势观察模式
app/run.py               → 自动识别最近交易日、耗时写回报告
app/data/akshare_provider.py → 东方财富财经快讯
app/llm/prompts/theme_scoring.txt  → 新建
app/llm/prompts/bull_agent.txt     → 新建
app/llm/prompts/bear_agent.txt     → 新建
```

---

## 20步完成情况

| 步骤 | 状态 | 实现位置 |
|---|---|---|
| 1. 新闻抓取 | ✅ | akshare_provider.py → 东方财富 |
| 2. 主题热度 | ✅ | b_theme_analysis.py → DeepSeek打标 |
| 3. 标准化映射 | ✅ | _THEME_TO_INDUSTRY_KEYWORDS |
| 4. 主题校验 | ⬜ | Phase 4 |
| 5. 主题反馈整理 | ⬜ | Phase 4 |
| 6. 主题周期 | ⬜ | delta_3d 初步实现，需历史积累 |
| 7. 主题状态 | ✅ | phase: 升温/趋势/退潮/事件驱动 |
| 8. 市场状态 | ✅ | a_market_gate.py 6态 |
| 9. 交易确认 | ✅ | TradePlan: VWAP/止损/止盈/仓位 |
| 10. 主题个股关联 | ✅ | _attach_theme_leaders() |
| 11. 策略选择 | 部分 | 市场状态→仓位档位 |
| 12. 数据回填 | ⬜ | Phase 4 |
| 13. 历史经验 | ⬜ | Phase 4 RAG |
| 14. 选股 | ✅ | c_stock_selection.py 五步流水线 |
| 15. 评审过滤 | 部分 | 综合评分排序取Top10 |
| 16. 实时风控 | ✅ | d_risk_debate.py 硬规则 |
| 17. 多头观点 | ✅ | bull_agent.txt + DeepSeek |
| 18. 空头观点 | ✅ | bear_agent.txt + DeepSeek |
| 19. 首席风控 | ✅ | d_risk_debate.py 裁决逻辑 |
| 20. 报告生成 | ✅ | e_report.py 完整报告 |

---

## 下一步优先级

1. **Phase 4 定时调度**：crontab 每个交易日16:00自动运行
2. **财联社接入**：用 Selenium/Playwright 从浏览器提取页面内容（绕过JS签名限制）
3. **Web UI**：FastAPI + 简单前端，展示报告和历史记录
4. **历史经验RAG**（步骤13）：存储历史报告 → 向量检索相似行情

---

## 环境信息
- Python 虚拟环境：`.venv/bin/python`
- Tushare积分：5100分（有效期至2027-04-30）
- 推送：Server酱（.env已配置）
- LLM：DeepSeek（flash=打标，pro=高质量分析）
- 财联社Cookie：已配置，API端点待解决
