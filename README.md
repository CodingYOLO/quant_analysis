# A股多Agent选股系统

每日盘后自动运行的选股简报系统，产出「市场状态 + 候选股 + 理由 + 风险」推送到微信。

## 快速开始

### 1. 安装依赖

```bash
cd astock-agent
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. 配置 .env

```bash
cp .env.example .env
# 用编辑器打开 .env，填入以下三个必填项：
# TUSHARE_TOKEN=
# DEEPSEEK_API_KEY=
# SERVERCHAN_SEND_KEY=
```

### 3. 验证数据接口

```bash
python -m app.run verify
```

所有接口 ✅ 通过后再运行主流程。

### 4. 运行选股流水线

```bash
# 运行昨天的数据（默认）
python -m app.run run

# 指定日期
python -m app.run run --date 20250613

# 只生成本地报告，不推送微信
python -m app.run run --no-notify

# 查看今日 LLM 费用
python -m app.run cost
```

报告输出到 `reports/<date>.md`，同时推送到微信。

## 项目结构

```
astock-agent/
  app/
    config.py              # 配置（pydantic-settings）
    state.py               # PipelineState 共享状态
    graph.py               # LangGraph 流水线图
    run.py                 # CLI 入口
    data/
      provider.py          # DataProvider 抽象接口
      tushare_provider.py  # Tushare 实现
      akshare_provider.py  # Akshare 实现
      composite_provider.py # 组合提供者（上层使用这个）
      cache.py             # 缓存/限频/重试
      verify.py            # 接口真实验证脚本
    llm/
      client.py            # LLMClient（成本日志）
    nodes/                 # 各 LangGraph 节点
    notify/
      notifier.py          # 推送（Server酱/邮箱）
    backtest/              # Phase 1 实现
  data_cache/              # 缓存（gitignore）
  reports/                 # 每日报告（gitignore）
```

## 配置参数说明

| 参数 | 说明 | 默认值 |
|---|---|---|
| `TUSHARE_TOKEN` | Tushare Pro token（必填） | - |
| `DEEPSEEK_API_KEY` | DeepSeek API key（必填） | - |
| `SERVERCHAN_SEND_KEY` | Server酱 SendKey，微信推送（必填） | - |
| `LLM_PROVIDER` | LLM 提供商 | `deepseek` |
| `NOTIFY_CHANNEL` | 推送渠道 | `serverchan` |
| `MAX_CANDIDATES` | 最大候选股数 | `10` |
| `MIN_MARKET_CAP` | 市值下限（亿） | `20` |
| `MAX_MARKET_CAP` | 市值上限（亿） | `500` |

## 数据源说明（Tushare 5100积分）

| 数据 | 数据源 | 备注 |
|---|---|---|
| 日线/股票列表/交易日历/指数 | Tushare | 基础接口，积分够用 |
| 资金流/龙虎榜/北向资金 | Tushare | 需2000积分，5100积分账号可用 |
| 行业资金流排名 | Akshare | Tushare需5000积分，暂用免费替代 |
| 实时快照/概念板块/千股千评/新闻 | Akshare | Tushare无此接口 |

## 免责声明

本系统定位是「信息聚合 + 量化初筛」工具，不构成投资建议。选股线的有效性需通过历史回测验证，所有数字可追溯到真实数据源。
