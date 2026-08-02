"""指标注册表 —— 宏观面板**唯一的指标真源**。

新增/停用/调权重只改这里（再跑一次 `macro-sync --sync-meta`），
**前端与计算层不得出现任何硬编码的指标列表或 `if code == 'xxx'` 分支**。

字段语义见 `store._SCHEMA`。几条约定：
- `direction`：1=数值高对A股偏多，-1=偏空，**0=中性（不参与分层评分，只展示）**
- `enabled=False`：已登记但本期不取数（数据源缺失/留待后续），链路图上显示为灰色"未接入"
- `hist_break`：口径断点日（逗号分隔，可多个），配合 `break_mode`
    · `truncate` —— **语义断点**：断点前后根本不是同一个量（如北向净买入→成交额），
                    分位/zscore 窗口必须从断点起算，否则算出来的分位是错的
    · `mark`     —— **制度断点**：同一个量，只是政策环境变了（如两融保证金比例调整），
                    可比性未被破坏，不截断窗口，只在图上画竖线 + note 说明
- `score_from`：该日起才参与分层评分，之前只展示不计分。**这是 `break_mode='mark'` 的必要补丁**——
  竖线是给人看的，`layer_score` 是机器算的、看不见竖线；制度断点会把指标中枢系统性下移，
  评分函数会把"制度性下移"误读成"情绪降温"，导致该层得分虚高数月。
- `source` / `source_fallback`：主源失败降级到备源；**实际用到的源写进 `macro_daily.source`**，
  便于事后排查"这个值当时到底是从哪来的"。
- 数据源优先级（硬约束）：**东财/新浪 > akshare 封装的境内源 > 境外源**。
  服务器在腾讯云境内，**境外源在本地测通不代表夜间任务能跑**，必须在服务器实测。
- 命名纪律：**口径不同就把口径写进 code**（`fdr007` 不叫 `dr007`，
  `cn_term_spread_10y2y` 不叫 `cn_term_spread`），杜绝"看着像但其实不是"的替代。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDef:
    code: str
    name_cn: str
    layer: str
    freq: str
    unit: str
    direction: int
    source: str
    api: str
    source_fallback: str = ""
    lag_days: int = 0
    weight: float = 1.0
    enabled: bool = True
    hist_break: str = ""
    break_mode: str = "truncate"
    score_from: str = ""
    # -1 = 按 freq 自动取默认（见 _resolve_carry）；显式 0 = 严格模式(不允许任何结转)
    max_carry_days: int = -1
    sort_order: int = 100
    note: str = ""

    def as_row(self) -> dict:
        d = self.__dict__.copy()
        d["enabled"] = int(self.enabled)
        d["max_carry_days"] = self._resolve_carry()
        return d

    def _resolve_carry(self) -> int:
        """按发布频率给出合理的结转上限（-1 表示用默认）。

        默认值不是拍的，是由"下一次发布前该值仍是当前有效值"决定的：
        · monthly=45 —— 月频数据在下月发布前一直有效；设 0 会让月频指标只在发布当天有值、
          次日即 NULL（实测踩到：1月CPI 只在 2/10 出现一天）；
        · weekly=15、daily=5 —— 覆盖周末 + 小长假；外盘指标在 A 股交易日常因休市而无当日值。
        超过上限仍取不到 → 写 NULL 并告警，不无限期挂着陈旧值冒充当前值。
        """
        if self.max_carry_days >= 0:
            return self.max_carry_days
        return {"monthly": 45, "weekly": 15}.get(self.freq, 5)


L0, L1, L2, L3 = "L0_liquidity", "L1_flow", "L2_sentiment", "L3_external"

# ──────────────────────────────────────────────
# L0 流动性 —— 决定"有没有水"
# ──────────────────────────────────────────────
_L0: list[MetricDef] = [
    MetricDef(
        code="fdr007", name_cn="FDR007 存款类机构7天回购定盘利率", layer=L0, freq="daily",
        unit="%", direction=-1, source="akshare", api="repo_rate_hist", sort_order=10,
        note="⚠️口径：这是 FDR007(11:00 定盘利率)，**不是 DR007(加权平均利率)**。两者高度相关但不是同一个数。"
             "选它是为了免掉中国货币网爬虫这个最脆弱的部件。利率高=资金面紧→对A股偏空。"
             "实测数据可回溯至 2019 年；接口单次区间上限约12个月(超出报 KeyError frValueMap)，回补按6个月切片。",
    ),
    MetricDef(
        code="cn_10y", name_cn="中国10年期国债收益率", layer=L0, freq="daily",
        unit="%", direction=-1, source="akshare", api="bond_zh_us_rate", sort_order=11,
        note="Tushare `yc_cb` 在本账号5100分**无权限**，改用 akshare。"
             "direction=-1 采用无风险利率上行压制权益估值的教科书口径；"
             "但A股实践中10Y上行也常伴随经济复苏预期(顺周期利好)，**方向本身有争议**，可在 metric_meta 调整。",
    ),
    MetricDef(
        code="cn_term_spread_10y2y", name_cn="中债期限利差(10Y−2Y)", layer=L0, freq="daily",
        unit="%", direction=1, source="derived", api="bond_zh_us_rate", sort_order=12,
        note="⚠️原需求写的是 10Y−1Y，但数据源只提供 2Y/5Y/10Y/30Y，**没有1Y**。"
             "故显式命名为 10y2y，不做静默替换。曲线陡峭化=宽松预期/经济改善→偏多。",
    ),
    MetricDef(
        code="us_10y", name_cn="美国10年期国债收益率", layer=L0, freq="daily",
        unit="%", direction=-1, source="akshare", api="bond_zh_us_rate", sort_order=13,
        note="全球无风险利率锚。上行=全球流动性收紧，压制风险资产。",
    ),
    MetricDef(
        code="us_2y", name_cn="美国2年期国债收益率", layer=L0, freq="daily",
        unit="%", direction=-1, source="akshare", api="bond_zh_us_rate", sort_order=14,
        note="更贴近美联储政策路径预期。",
    ),
    MetricDef(
        code="cn_us_spread_10y", name_cn="中美利差(中10Y−美10Y)", layer=L0, freq="daily",
        unit="%", direction=1, source="derived", api="bond_zh_us_rate", sort_order=15,
        note="⚠️原需求写 `us_10y − cn_10y`，那是**美中**利差。此处按通行口径取 中国−美国(当前为负)，"
             "并把方向写进 name。利差收窄(向0靠)=人民币贬值压力小=资金流出压力小→偏多。",
    ),
    MetricDef(
        code="usdcnh", name_cn="离岸人民币 USDCNH", layer=L0, freq="daily",
        unit="", direction=-1, source="tushare", api="fx_daily(USDCNH.FXCM)", sort_order=16,
        note="比在岸敏感。数值上行=人民币贬值=外资流出压力→偏空。"
             "⚠️`fx_daily` 用 trade_date= 参数实测返回空，必须用 start_date/end_date 区间查询。",
    ),
    MetricDef(
        code="shibor_3m", name_cn="Shibor 3个月", layer=L0, freq="daily",
        unit="%", direction=-1, source="tushare", api="shibor", sort_order=17,
        note="中期资金价格。与 FDR007 互补：FDR007 看隔夜/短端松紧，3M 看中期预期。",
    ),
    # ── 已登记但本期不取数 ────────────────────────────────────────────────
    MetricDef(
        code="dxy", name_cn="美元指数(ICE)", layer=L0, freq="daily",
        unit="", direction=-1, source="eastmoney", api="push2his kline secid=100.UDI",
        source_fallback="akshare:index_global_hist_em(美元指数)", enabled=False, sort_order=18,
        note="职责：把人民币走弱拆成『美元故事 vs 中国故事』——usdcnh 单看分不清是美元强还是人民币弱。"
             "⚠️**刻意不用 FXCM 的 USDOLLAR 篮子**：它含商品货币敞口，做不了这个拆分。"
             "取数走**自写东财适配器**而非 akshare 封装：服务器实测 akshare 的 index_global_hist_em "
             "被拦(RemoteDisconnected)，而裸接 push2his + 浏览器UA/Referer 返回 HTTP 200/46ms 有效数据。"
             "❌本期 enabled=0：数据本身已验证可得，但**服务器取数通道待定**，不阻塞主线，另作独立小任务。"
             "⚠️注意我此前的测试是错的：连打20次把IP打封、再拿该结果评估『每天1次』的生产场景，"
             "严苛二十倍、对生产无推断力。正确方案按性价比排序："
             "①**回补与增量分离**(最优)——750日历史在本地跑一次全量、导出文件灌进服务器 macro.db，"
             "服务器夜间只做单日增量(1次/单secid/单条K线)；回补脚本与原始导出文件一并入库，"
             "source_run_id 标 manual_backfill_YYYYMMDD 保证可追溯。此模式对所有东财系指标通用。"
             "②换源——优先腾讯行情 qt.gtimg.cn(服务器在腾讯云·同云内最不可能被反爬)；"
             "其次新浪(index_us_stock_sina 取 SOX 在服务器实测可用，说明新浪系是通的，"
             "只是 .VIX 该 symbol 不支持)——须抓新浪全球指数页 XHR 拿真实代码，**不许猜 symbol**。"
             "③仍用东财则：Connection: close 不复用 Session、间隔≥5s 加随机抖动、"
             "Referer 设为对应 quote 页、绝不并发；验证按真实节奏(每天1次连续5天)。",
    ),
    MetricDef(
        code="omo_net", name_cn="央行公开市场净投放", layer=L0, freq="daily",
        unit="亿元", direction=1, source="", api="", enabled=False, sort_order=19,
        note="❌未接入(**系统性排查后的否定结论**)：对 akshare 1095 个函数做了两轮扫描——"
             "①函数名关键词；②**全库默认 symbol 参数值的中文语义**（akshare 惯用中文 symbol + 通用函数名，"
             "只扫函数名必漏）。『逆回购/公开市场/净投放/操作/央行/货币政策/MLF/SLF』**全部零命中**。"
             "`macro_china_central_bank_balance` 是月频央行资产负债表(354行)、"
             "`macro_china_reserve_requirement_ratio` 是准备金率调整事件表(58行)，都不是日频 OMO。"
             "要接只能爬央行官网/东财专题页。按'不编造数据源+不静默失败'，宁可留空也不先上脆弱爬虫。"
             "（逆回购到期量同样零命中，一并留待 Phase 2。）",
    ),
]

# ── L0 月频（写入发布日，前端 forward fill 并标 is_stale=1）────────────────
_L0_MONTHLY: list[MetricDef] = [
    MetricDef(code="m1_yoy", name_cn="M1 同比", layer=L0, freq="monthly", unit="%",
              direction=1, source="tushare", api="cn_m", lag_days=15, sort_order=30,
              note="M1 反映企业活期存款/交易性需求，对A股领先性优于 M2。"),
    MetricDef(code="m2_yoy", name_cn="M2 同比", layer=L0, freq="monthly", unit="%",
              direction=1, source="tushare", api="cn_m", lag_days=15, sort_order=31),
    MetricDef(code="social_finance_inc", name_cn="社融增量(当月)", layer=L0, freq="monthly",
              unit="亿元", direction=1, source="tushare", api="sf_month", lag_days=15, sort_order=32,
              note="⚠️原需求写的接口名 `cn_sf` **不存在**，实测正确接口是 `sf_month`(列 inc_month/inc_cumval/stk_endval)。"),
    MetricDef(code="cpi_yoy", name_cn="CPI 同比", layer=L0, freq="monthly", unit="%",
              direction=0, source="tushare", api="cn_cpi", lag_days=10, sort_order=33,
              note="direction=0：通胀对A股是双刃(温和通胀利好顺周期，高通胀压制估值)，不参与评分只展示。"),
    MetricDef(code="ppi_yoy", name_cn="PPI 同比", layer=L0, freq="monthly", unit="%",
              direction=1, source="tushare", api="cn_ppi", lag_days=10, sort_order=34,
              note="PPI 回升=工业品价格改善=企业盈利修复→偏多。"),
    MetricDef(code="pmi_mfg", name_cn="制造业 PMI", layer=L0, freq="monthly", unit="",
              direction=1, source="tushare", api="cn_pmi", lag_days=1, sort_order=35,
              note="荣枯线50。发布快(月初)，是月频里最及时的。"),
    MetricDef(code="lpr_1y", name_cn="LPR 1年期", layer=L0, freq="monthly", unit="%",
              direction=-1, source="tushare", api="shibor_lpr", lag_days=0, sort_order=36,
              note="每月20日发布。下调=宽松→偏多，故 direction=-1。"),
    MetricDef(code="lpr_5y", name_cn="LPR 5年期以上", layer=L0, freq="monthly", unit="%",
              direction=-1, source="tushare", api="shibor_lpr", lag_days=0, sort_order=37,
              note="房贷定价基准，对地产链影响直接。"),
]

# ──────────────────────────────────────────────
# L1 市场内部资金 —— 决定"水流到哪"
# ──────────────────────────────────────────────
_MARGIN_BREAKS = "20230911,20260119"
# 最近断点 20260119 + 3 个月：断点后杠杆上限 1.25→1.00 会把比值中枢系统性下移，
# 这段时间内只展示不计分，避免评分函数把"制度性下移"读成"杠杆情绪降温"而虚高 L1 得分。
_MARGIN_SCORE_FROM = "20260419"
_MARGIN_NOTE = (
    "两融保证金比例断点(已核实交易所原文)："
    "① 20230911 —— 上证发〔2023〕140号(2023-08-27发布)将融资保证金最低比例 100%→80%，"
    "原文「自2023年9月8日**收市后**实施」，故 9/8 收盘数据仍属旧口径，第一个新口径交易日为 9/11(周一)；"
    "该次**存量合约证券公司可相应下调**，影响释放较快。"
    "② 20260119 —— 上证发〔2026〕5号(2026-01-14发布)将比例 80%→100%，原文「自2026年1月19日起施行」，"
    "且「实施前尚未了结的融资合约及其展期…仍按原规定执行」= **严格新老划断，影响渐进释放约4–8周**。"
    "break_mode=mark：这是**制度断点**不是语义断点，同一个量、可比性未被破坏，"
    "故不截断分位窗口(硬截断会让分位留空到约2027-01)，只在图上画竖线标注。"
    "如需改为硬截断，把 metric_meta.break_mode 改成 truncate 即可，无需改代码。"
    f"⚠️但竖线只解决『给人看』：评分函数看不见竖线，会把杠杆上限 1.25→1.00 造成的中枢系统性下移"
    f"误读成『杠杆情绪降温』→ L1 得分虚高数月。故设 score_from={_MARGIN_SCORE_FROM}"
    f"(断点+3个月)，在此之前只展示不计分。卡片须同时显示文字标注『窗口内含制度断点 20260119』。"
)

_L1: list[MetricDef] = [
    MetricDef(
        code="turnover_total", name_cn="两市成交额", layer=L1, freq="daily",
        unit="亿元", direction=1, source="internal", api="get_daily聚合", sort_order=10,
        note="复用项目现有行情缓存聚合(amount 千元→亿元)，不新增取数。",
    ),
    MetricDef(
        code="margin_ratio", name_cn="融资余额/A股流通市值", layer=L1, freq="daily",
        unit="%", direction=1, source="derived", api="margin + daily_basic", lag_days=1,
        hist_break=_MARGIN_BREAKS, break_mode="mark", score_from=_MARGIN_SCORE_FROM, sort_order=11,
        note="⭐两融的**主指标**。绝对值三年从14863亿单边升到29971亿(翻倍)，分位会永远卡在100%、天天误报异动；"
             "比值有均值回复特性——实测三年只在 2.03%~2.83% 区间摆动，2025年均值 2.40%。" + _MARGIN_NOTE,
    ),
    MetricDef(
        code="margin_balance", name_cn="融资余额(绝对值)", layer=L1, freq="daily",
        unit="亿元", direction=0, source="tushare", api="margin", lag_days=1,
        hist_break=_MARGIN_BREAKS, break_mode="mark", score_from=_MARGIN_SCORE_FROM, sort_order=12,
        note="**副指标·direction=0 不参与评分**(单边趋势项做分位无意义，见 margin_ratio)。仅供看绝对规模。"
             "⚠️必须 SSE+SZSE+BSE **三所齐全**才可汇总：实测 20260731 仅 SSE 有数据，"
             "裸 sum() 跳过 NaN 会得 13274亿(实际约25845亿)，静默产出 -49% 假暴跌。缺任一所即写 NULL。",
    ),
    MetricDef(
        code="margin_buy_ratio", name_cn="融资买入额/两市成交额", layer=L1, freq="daily",
        unit="%", direction=1, source="derived", api="margin + get_daily聚合", lag_days=1,
        sort_order=13, note="杠杆资金参与度。比融资余额更贴近'当下情绪'，余额是存量、买入额是流量。",
    ),
    MetricDef(
        code="mainflow_total", name_cn="全市场主力净流入", layer=L1, freq="daily",
        unit="亿元", direction=1, source="internal", api="moneyflow.main_net_wan", sort_order=14,
        note="**复用项目现有口径**(超大单+大单净·东财口径)，不重新实现。"
             "禁用 net_mf_amount(与东财口径约50%符号相反)。",
    ),
    MetricDef(
        code="northbound_turnover", name_cn="北向成交额(非净额)", layer=L1, freq="daily",
        unit="亿元", direction=0, source="tushare", api="moneyflow_hsgt", lag_days=0,
        hist_break="20240819", break_mode="truncate", sort_order=15,
        note="⚠️⚠️**此列不是净流入**。沪深交易所自 2024-08-19 调整沪深港通披露机制后，"
             "`moneyflow_hsgt.north_money` 字段**没有变空、但含义变了**：由「净买入」变为「成交额」。"
             "证据：① 变更后该值每日稳定占全A成交额14%±1%(净额是买卖轧差，不可能达此比例)；"
             "② 变更前一周均值-10.1亿、有正有负，变更后一周均值+863亿、此后300个交易日**零负值**；"
             "③ 单位为百万元，8/16 的 -6774.99 = 净卖出67.75亿，8/19 起跳至 88110.55 = 881亿。"
             "故 direction=0(只作活跃度代理，不判多空)，break_mode=truncate(**语义断点**，"
             "两段不可拼接，分位窗口必须自 2024-08-19 起算)。**禁止在面板上做'北向净流入'卡片。**",
    ),
    MetricDef(
        code="etf_share_chg", name_cn="宽基ETF份额变化", layer=L1, freq="daily",
        unit="亿份", direction=1, source="tushare", api="fund_share", lag_days=1, sort_order=16,
        note="宽基ETF份额增加≈场外资金借道入场(常见于国家队/机构申购)。",
    ),
    MetricDef(
        code="buyback_amt", name_cn="回购金额(近20交易日)", layer=L1, freq="daily",
        unit="亿元", direction=1, source="tushare", api="repurchase", lag_days=1, sort_order=17,
        note="产业资本自身的买入。用滚动20日累计而非单日(单日噪音大且公告分布不均)。",
    ),
    MetricDef(
        code="float_release", name_cn="解禁规模(未来20交易日)", layer=L1, freq="daily",
        unit="亿元", direction=-1, source="tushare", api="share_float", lag_days=0, sort_order=18,
        note="前瞻指标：统计未来20交易日待解禁市值。规模大=潜在抛压→偏空。",
    ),
    MetricDef(
        code="new_fund_share", name_cn="新成立基金份额(近20交易日)", layer=L1, freq="weekly",
        unit="亿份", direction=1, source="tushare", api="fund_basic", lag_days=5, sort_order=19,
        note="增量资金的先行指标。按成立日聚合，滚动20交易日。",
    ),
]

# ──────────────────────────────────────────────
# L2 情绪温度 —— Phase 2（全部 enabled=False 先登记）
# 纪律：L2 落地时必须走与 L0/L1 **完全相同**的 Adapter→macro_daily→compute→service 路径，
#       前端只认 metric_meta.layer，代码里不得出现任何按层的特例分支。
# ──────────────────────────────────────────────
_L2: list[MetricDef] = [
    MetricDef(code="limit_up_cnt", name_cn="涨停家数", layer=L2, freq="daily", unit="家",
              direction=1, source="tushare", api="limit_list_d(U)", enabled=False, sort_order=10,
              note="Phase 2。**必须复用** market_sentiment 的现成口径，避免与🌡️大盘情绪页出现两个数。"),
    MetricDef(code="limit_down_cnt", name_cn="跌停家数", layer=L2, freq="daily", unit="家",
              direction=-1, source="tushare", api="limit_list_d(D)", enabled=False, sort_order=11,
              note="Phase 2。⚠️实测 20260731 当日 limit 分布只有 U/Z 没有 D，"
                   "落地时需确认跌停是否必须用 limit_type='D' 单独查。"),
    MetricDef(code="broken_board_ratio", name_cn="炸板率", layer=L2, freq="daily", unit="%",
              direction=-1, source="derived", api="limit_list_d(limit=Z/U)", enabled=False, sort_order=12,
              note="Phase 2。limit_list_d 的 `limit` 字段直接给 U/Z/D，炸板率=Z/(U+Z)，不必自算。"),
    MetricDef(code="max_consecutive", name_cn="最高连板高度", layer=L2, freq="daily", unit="板",
              direction=1, source="derived", api="limit_list_d(limit_times)", enabled=False, sort_order=13,
              note="Phase 2。limit_list_d 有现成的 limit_times/up_stat 字段，不必自算。"),
    MetricDef(code="yst_limit_ret", name_cn="昨日涨停股今日平均涨幅", layer=L2, freq="daily", unit="%",
              direction=1, source="internal", api="limit_list_d + get_daily", enabled=False, sort_order=14,
              note="**赚钱效应核心指标**。项目现有模块里确实没有，Phase 2 新写。"),
    MetricDef(code="adv_dec_ratio", name_cn="涨跌家数比", layer=L2, freq="daily", unit="",
              direction=1, source="internal", api="get_daily聚合", enabled=False, sort_order=15,
              note="Phase 2。复用 market_sentiment._breadth_series 口径，勿另起炉灶。"),
    MetricDef(code="median_ret", name_cn="全市场涨跌幅中位数", layer=L2, freq="daily", unit="%",
              direction=1, source="internal", api="get_daily聚合", enabled=False, sort_order=16,
              note="Phase 2。比指数真实——指数被权重股扭曲，中位数才是'大部分票今天怎么样'。"),
    MetricDef(code="turnover_rate_all", name_cn="全A换手率", layer=L2, freq="daily", unit="%",
              direction=1, source="tushare", api="daily_basic", enabled=False, sort_order=17,
              note="Phase 2。daily_basic 逐日取，回补成本 1 次/日。"),
    MetricDef(code="style_ratio", name_cn="沪深300/中证2000 比值", layer=L2, freq="daily", unit="",
              direction=0, source="derived", api="index_daily", enabled=False, sort_order=18,
              note="Phase 2。direction=0：风格无好坏之分，只指示大盘股/小盘股谁占优。"),
    MetricDef(code="erp", name_cn="股债性价比 ERP", layer=L2, freq="daily", unit="%",
              direction=1, source="derived", api="index_dailybasic + bond_zh_us_rate", enabled=False,
              sort_order=19, note="Phase 2。1/PE(沪深300) − 中国10Y。高=股票相对债券便宜。"),
    MetricDef(code="qvix_300", name_cn="中国版VIX(300ETF期权隐波)", layer=L2, freq="daily", unit="%",
              direction=-1, source="akshare", api="index_option_300etf_qvix", enabled=False, sort_order=20,
              note="**归属 L2 而非 L3**：这是国内期权市场的隐含波动率，属国内情绪，"
                   "放进'外部输入'会让层含义失真。实测有2780行历史。Phase 2 启用。"),
]

# ──────────────────────────────────────────────
# L3 外部输入
# ──────────────────────────────────────────────
_L3: list[MetricDef] = [
    MetricDef(
        code="sox", name_cn="费城半导体指数 SOX", layer=L3, freq="daily",
        unit="点", direction=1, source="akshare", api="index_us_stock_sina(.SOX)",
        weight=2.0, sort_order=10,
        note="⭐**权重2倍**(用户主做半导体，该指数对A股半导体链的传导最直接)。"
             "Tushare `index_global` 不含 SOX，实测 akshare 新浪源有 3152 行历史(2014-01-16 起)。",
    ),
    MetricDef(code="nasdaq", name_cn="纳斯达克综合指数", layer=L3, freq="daily", unit="点",
              direction=1, source="tushare", api="index_global(IXIC)", sort_order=11,
              note="⚠️`index_global` 限频 **10次/分钟**，适配器需自行 sleep，不能套用项目默认1.5s间隔。"),
    MetricDef(code="hk_tech", name_cn="恒生科技指数", layer=L3, freq="daily", unit="点",
              direction=1, source="tushare", api="index_global(HKTECH)", sort_order=12,
              note="⚠️代码是 `HKTECH` 不是 HSTECH(实测 HSTECH 返回空)。中概/港股科技情绪的同步指标。"),
    MetricDef(
        code="vix", name_cn="VIX 恐慌指数", layer=L3, freq="daily", unit="",
        direction=-1, source="eastmoney", api="push2his kline secid=167.VIX", enabled=False, sort_order=13,
        note="服务器实测：东财 secid=167.VIX 返回 HTTP 200/141ms，name 字段确认为『VIX恐慌指数』。"
             "其余路径均不可用——Tushare `index_global` 无 VIX；"
             "akshare `index_us_stock_sina('.VIX')` 报 IndexError（同函数 '.SOX' 正常，属该 symbol 不支持）。"
             "**刻意不用国内 qvix 顶替**：qvix 是国内300ETF期权隐波、属国内情绪(已归 L2)，"
             "拿它填 L3 会让『外部输入』这一层的含义失真。"
             "❌本期 enabled=0：同 dxy——数据已验证可得，服务器取数通道待定，另作独立小任务，不阻塞主线。"
             "换源优先级：腾讯 qt.gtimg.cn > 新浪(需抓 XHR 拿真实代码·不猜) > 东财(需 Connection:close+≥5s抖动)。",
    ),
    MetricDef(code="brent", name_cn="布伦特原油", layer=L3, freq="daily", unit="美元",
              direction=0, source="", api="", enabled=False, sort_order=14, note="Phase 2 评估数据源。"),
    MetricDef(code="comex_gold", name_cn="COMEX 黄金", layer=L3, freq="daily", unit="美元",
              direction=0, source="", api="", enabled=False, sort_order=15, note="Phase 2 评估数据源。"),
    MetricDef(code="lme_copper", name_cn="LME 铜", layer=L3, freq="daily", unit="美元",
              direction=1, source="", api="", enabled=False, sort_order=16, note="Phase 2 评估数据源。"),
]

METRICS: tuple[MetricDef, ...] = tuple(_L0 + _L0_MONTHLY + _L1 + _L2 + _L3)

_BY_CODE: dict[str, MetricDef] = {m.code: m for m in METRICS}


def get(code: str) -> MetricDef | None:
    return _BY_CODE.get(code)


def enabled_codes(layer: str | None = None) -> list[str]:
    return [m.code for m in METRICS if m.enabled and (layer is None or m.layer == layer)]


def sync_to_db() -> int:
    """把注册表写入 metric_meta（幂等）。用户在库里调过的 weight/enabled/hist_break/break_mode 会被保留。"""
    from app.macro import store
    store.init_db()
    return store.upsert_meta(m.as_row() for m in METRICS)


def validate() -> list[str]:
    """自检注册表一致性，返回问题列表（空=通过）。CI/单测与 `macro-sync` 启动时都会跑。"""
    from app.macro.store import LAYERS
    problems: list[str] = []
    seen: set[str] = set()
    for m in METRICS:
        if m.code in seen:
            problems.append(f"{m.code}: 重复定义")
        seen.add(m.code)
        if m.layer not in LAYERS:
            problems.append(f"{m.code}: 未知 layer={m.layer}")
        if m.direction not in (-1, 0, 1):
            problems.append(f"{m.code}: direction 必须是 -1/0/1，实为 {m.direction}")
        if m.freq not in ("daily", "weekly", "monthly"):
            problems.append(f"{m.code}: 未知 freq={m.freq}")
        if m.break_mode not in ("truncate", "mark"):
            problems.append(f"{m.code}: break_mode 必须是 truncate/mark，实为 {m.break_mode}")
        for d in filter(None, m.hist_break.split(",")):
            if not (len(d) == 8 and d.isdigit()):
                problems.append(f"{m.code}: hist_break 日期格式应为 YYYYMMDD，实为 {d!r}")
        if m.enabled and not m.source:
            problems.append(f"{m.code}: enabled=True 但没有 source")
        if not m.enabled and "❌" not in m.note and "Phase 2" not in m.note:
            problems.append(f"{m.code}: enabled=False 但 note 未说明原因")
        if m.weight < 0:
            problems.append(f"{m.code}: weight 不能为负")
    return problems
