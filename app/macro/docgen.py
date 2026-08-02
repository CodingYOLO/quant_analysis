"""口径文档生成器（§8 验收项·commit 6 最高优先级）。

**单一真源原则**：逐指标口径全部由 `metric_meta`（即 registry）渲染生成——
文档与代码不可能漂移；方法论/陷阱实录/运维等章节为静态正文，与实现同仓演进。
重新生成：`python -m app.run macro-doc`（文件名带生成时刻·旧版留档不覆盖）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from app.macro import store
from app.macro.compute import ANOMALY, WINDOWS

logger = logging.getLogger(__name__)

_DIR_LABEL = {1: "高=偏多", -1: "高=偏空", 0: "中性(不评分)"}


def generate(out_dir: str | Path = "docs") -> Path:
    store.init_db()
    from app.macro import registry
    registry.sync_to_db()
    now = datetime.now()
    path = Path(out_dir) / f"宏观指标口径说明_{now:%Y%m%d_%H%M}.md"
    path.write_text(_render(now), encoding="utf-8")
    logger.info("[macro] 口径文档已生成: %s", path)
    return path


def _render(now: datetime) -> str:
    metas = store.get_meta(enabled_only=False)
    parts = [_head(now), _method(), _anomaly_doc(), _metrics_doc(metas),
             _traps(), _ops(), _attach_doc(), _limits(), _llm_status()]
    return "\n\n".join(parts) + "\n"


def _head(now: datetime) -> str:
    return f"""# 宏观传导面板 · 指标口径说明

> 生成时间：{now:%Y-%m-%d %H:%M}（由 `python -m app.run macro-doc` 从 metric_meta 渲染·逐指标口径与代码同源不漂移）
> 定位：**约束器·不是信号源**——回答"环境在什么位置、今天什么变了、传导卡在哪一环"，
> 不产生买卖信号、不给仓位建议、不做涨跌预测。

## 一、总体结构

- **四层**：L0 流动性(有没有水) → L1 市场内部资金(水流到哪) → L2 情绪温度(Phase 2) → L3 外部输入
- **存储**：独立 `data_cache/macro.db`（SQLite·WAL——cron 写 + Web 读并发必需），与 strategy.db 分离
- **每日流水**：cron 19:50（在 19:25 warmup 因子任务之后）`macro-sync --resync 10`：
  取数(滚动重写近10天·修迟到数据) → point-in-time 对齐 → 全历史重算派生量与评分 → 事件日历播种
- **页面**：`/macro`·只读库·断网可开；回看 `?date=` 严格 point-in-time"""


def _method() -> str:
    w = WINDOWS
    return f"""## 二、计算方法（全部 point-in-time·滚动窗右端=当前行）

**分位/zscore 窗口**（按发布频率缩放·统计只在**新值序列**上算·沿用日继承）：

| 频率 | 分位窗口 | 分位最少样本 | zscore窗口 |
|---|---|---|---|
| daily | {w['daily']['pct_win']} 交易日 | {w['daily']['pct_min']}（不足**留空**·绝不短窗凑数） | {w['daily']['z_win']} |
| weekly | {w['weekly']['pct_win']} 期 | {w['weekly']['pct_min']} | {w['weekly']['z_win']} |
| monthly | {w['monthly']['pct_win']} 次发布 | {w['monthly']['pct_min']} | {w['monthly']['z_win']} |

**分层评分**：`score = Σ(好坏分位 × weight) / Σweight`，恒在 0-100。
- 好坏分位 = direction=1 时取原分位·direction=-1 时取 100−分位（颜色/评分语义统一为"对A股好坏"）
- **动态分母**：指标被跳过时其权重同步从 Σweight 剔除（否则得分被系统性拉低）；
  UI 显示 n_part/n_total（"15/15 项参与评分"）
- 参与六条件：值非空 · 分位非空 · direction≠0 · weight>0 · 日期≥score_from ·
  （daily→已沿用≤1个会话；weekly/monthly 沿用是固有节奏照常计分）
- 「为什么是64?」拆解：`score = 50 + Σ pull`，`pull_i = (好坏分位_i − 50) × w_i/Σw`——逐项可核账

**结转（max_carry_days）与"绝不 fallback 昨值"的边界**：红线禁的是静默伪装，不是显式降级。
沿用必须同时满足：as_of 保真 + is_stale=沿用会话数 + 不参与评分（daily≥2会话时）。超限写 NULL。
daily=2 个**交易日会话**（自然日会把周五值沿用到周一误判成3天）；monthly=45/weekly=15 自然日。

**断点两模式**：
- `truncate`（语义断点·断点前后不是同一个量）：统计窗口硬截断·值本身也不入库（北向）
- `mark`（制度断点·同一个量政策变了）：分位全窗口照算正常显示·仅评分由 score_from 把门
  （竖线给人看·score_from 给机器——评分函数看不见竖线）"""


def _anomaly_doc() -> str:
    a = ANOMALY
    return f"""## 三、异动判定（目标 0-2 条/天·价值在稀缺）

`raw = |z| > {a['z_th']} 或 |1日变动| > 近一年新值日的95分位(滚动·shift(1)不含当日)`；
日频再加确认：**连续 2 个新值日同向触发才报**（日频序列尾部厚·滤单日噪音）；月频=发布事件单次即报。

档位由 624 个交易日 × 15 指标真实频率研究选定（V3）：日均 0.40→chg口径修正后 0.73·最多 5/日。
研究中的多条日全是真实宏观转折（20240220=LPR5Y大降息日·20240906=美联储降息前美债急跌·
20241227=年末资金面收紧）——相关指标同日共振·多条是特征不是噪音。

**复检待办（用户定档）**：上线满一个月回看实际触发频率，若 <1条/周 → z 阈值 2.0→1.8。"""


def _metrics_doc(metas: list[dict]) -> str:
    out = ["## 四、指标明细（由 metric_meta 生成·与代码同源）"]
    for layer in store.LAYERS:
        mine = [m for m in metas if m["layer"] == layer]
        if not mine:
            continue
        out.append(f"\n### {store.LAYER_LABELS[layer]}")
        for m in sorted(mine, key=lambda x: (not x["enabled"], x["sort_order"])):
            flag = "" if m["enabled"] else "〔未启用〕"
            head = (f"**{m['code']}** · {m['name_cn']} {flag}\n"
                    f"  - 口径：{m['unit'] or '—'} · {m['freq']} · {_DIR_LABEL.get(m['direction'])}"
                    f" · 权重{m['weight']} · 滞后{m['lag_days']}天 · 源 `{m['source'] or '—'}`"
                    f"（{m['api'] or '—'}）")
            if m["hist_break"]:
                head += f"\n  - 断点：{m['hist_break']}（{m['break_mode']}"
                head += f"·{m['score_from']}起计分）" if m["score_from"] else "）"
            if m.get("no_dist"):
                head += "\n  - 前瞻计划类：不算分位/z/异动（无历史分布可比）"
            if m["note"]:
                head += f"\n  - 备注：{m['note']}"
            if m.get("explain"):
                head += f"\n  - 怎么读：{m['explain']}"
            out.append(head)
    return "\n\n".join(out)


def _traps() -> str:
    return """## 五、数据陷阱实录（全部实测·各自的证据在指标备注与代码注释里）

1. **北向 north_money 没变空但含义变了**（2024-08-19 净买入→成交额）：变更后每日稳定=全A成交额14%±1%
   （净额是轧差不可能达此比例）；变更前一周均值-10.1亿有正有负·变更后300交易日零负值。
   单位百万元（8/16 的 -6774.99 = 净卖出67.75亿）。→ 只做成交额指标·中性·断点前值不入库。
2. **两融三所部分缺失**：20260731 仅 SSE 发布·裸 sum 得 13274亿(实际约25845亿)=-49%假暴跌。
   → 三所齐全才汇总·判据只看"有没有行"不看值大小（北交所82亿是真值）。
3. **share_float 单位实测是股**（文档标万股）：920180.BJ 30万÷0.254%=总股本1.18亿股自洽·
   按万股反推总股本1.18万亿股荒谬。错按万股会得出"未来4周解禁1300万亿"。另：offset全局上限≈10万行·
   解禁季单月即超 → 15天切片。
4. **moneyflow_mkt_dc 单位是元**（非万元）·与项目 market_fund(Σdc·剔BJ)口径逐日完全吻合。
5. **接口静默截断先例**：fina_indicator_vip 12000行·moneyflow_hsgt 300行·fund_basic(O) 15000行·
   repo_rate_hist >12个月报错——所有区间接口必须分页/切片并校验行数。
6. **可正可负的流量指标禁用涨跌幅**：pct_change 分母过零会算出-517%（主力净流入实测）→
   量级类(亿元/亿份/家/%/bp)一律绝对差·仅无单位比值与点位用涨跌幅。
7. **deepseek-v4-flash 思考截断**：数据分析任务自动进思考·max_tokens=700 全被 reasoning 吃光·
   content 空且 finish_reason=length（曾误判为荐股过滤）→ max_tokens 2500 起·截断加倍重试。
8. **回购 repurchase 未接入**：proc=实施的 vol/amount 是程序内累计值且无程序ID·按行求和数倍重复
   （中石化31行实测）→ 宁缺毋假。"""


def _ops() -> str:
    return """## 六、更新时间表与运维

| 什么 | 何时 | 说明 |
|---|---|---|
| macro-sync（取数+计算+日历） | 交易日 19:50 cron | 在 19:25 warmup 之后；--resync 10 滚动重写近旬 |
| 单指标可见时点 | as_of + lag_days | 两融 T+1 晨、月度数据发布日、美债隔夜差1会话属正常 |
| 全量回补 | 手动 `macro-backfill --days 750` | 幂等·与增量同一段代码 |
| 口径文档再生成 | 手动 `macro-doc` | 指标部分从 metric_meta 渲染 |
| 调权重/断点/评分门禁 | 直接改 metric_meta 行 | 无需改代码；`--reset-tuning` 恢复注册表默认 |
| 取数结果核查 | macro_run_log 表 | 每指标 成功/失败/行数/耗时·Bark 告警失败项 |"""


def _attach_doc() -> str:
    return """## 七、跨库联查（ATTACH·只读）

macro.db 与 strategy.db 均为 WAL：跨库**只读 join 安全**；WAL 下 SQLite 不支持跨库写事务，
故 macro-sync 只写 macro.db。示例——"什么宏观环境下我的选股有效"：

```sql
ATTACH DATABASE '/home/ubuntu/astock-agent/data_cache/strategy.db' AS s;

SELECT CAST(m.pctile_750/20 AS INT)*20 AS 流动性分位段,
       COUNT(*) AS 样本, ROUND(AVG(p.pct_return),2) AS 平均收益
FROM macro_daily m
JOIN s.selection_records r ON r.run_date = m.trade_date
JOIN s.performance_records p ON p.selection_id = r.id AND p.horizon = 5
WHERE m.code='fdr007' AND m.pctile_750 IS NOT NULL
GROUP BY 1 ORDER BY 1;
```"""


def _limits() -> str:
    return """## 八、已知局限（如实列出·别当没有）

- **总分=已启用层均值**（当前 L0+L1），不是四层全貌；L2 Phase 2、L3 数据源待接入
- FDR007 是定盘利率**不是** DR007 加权价（两者高度相关但不同数）——为免掉 chinamoney 爬虫
- 两市成交额=综指口径不含北交所（与全A差~0.7%）
- DXY/VIX 已验证东财源可得（100.UDI/167.VIX）但服务器取数通道待定——独立任务
- 月频指标"距下次发布约N天"为规则估算（±2天）；事件日历三类诚实度见 calendar_seed 模块头
- 异动档位在 2024-2026 波动率环境下调校，环境变了要复检（见三）
- 事件日历不是历史快照——回看模式下日历显示当前登记状态
- 分位是"跟自己历史比"，回答"高不高"，不回答"该不该"——本面板全部输出均无操作含义"""


def _llm_status() -> str:
    return """## 九、LLM 讲解模块（讲解≠结论·用户定档 2026-08-02）

- **卡片级「这是什么」**：静态教学文本（metric_meta.explain·不用 LLM·点 ℹ️ 展开）
- **「为什么是64?」**：纯数学拆解（score=50+Σ拉动·逐项核账·点体温计块）
- **每日讲解**：LLM·3-5句·每句=「指标位置→机制→传导含义」；输出侧禁词硬校验
  （建议/应该/可以考虑/看多/看空/加仓/减仓/机会/风险偏好升降）·两次违规如实报错不展示
- **位置纪律**：讲解固定放页面**底部**——先自己读数据形成判断·讲解做校正而非替代
- **回看可用**：任意历史日可生成（输入是该日 point-in-time 行·回看 20240924 等关键日=最高效学习）
- 结论型摘要（"今天偏松可以积极"）**永久禁止**——会让用户跳过数据看结论·手感建立不起来"""
