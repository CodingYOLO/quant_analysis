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
    no_dist: int = 0
    sort_order: int = 100
    note: str = ""

    def as_row(self) -> dict:
        d = self.__dict__.copy()
        d["enabled"] = int(self.enabled)
        d["max_carry_days"] = self._resolve_carry()
        d["explain"] = EXPLAIN.get(self.code, "")
        return d

    def _resolve_carry(self) -> int:
        """按发布频率给出合理的结转上限（-1 表示用默认）。

        默认值不是拍的，是由"下一次发布前该值仍是当前有效值"决定的：
        · monthly=45 —— 月频数据在下月发布前一直有效；设 0 会让月频指标只在发布当天有值、
          次日即 NULL（实测踩到：1月CPI 只在 2/10 出现一天）；
        · weekly=15（自然日）、daily=2（**交易日会话**·外盘隔夜差1个会话属正常，断2个即告警）。
        超过上限仍取不到 → 写 NULL 并告警，不无限期挂着陈旧值冒充当前值。
        """
        if self.max_carry_days >= 0:
            return self.max_carry_days
        # daily=2(**交易日会话**)：日频断2个会话不是延迟是源坏了——要的是早知道，
        # 不是让面板替源掩饰5天(2026-08-02用户定档·由5降2)。monthly/weekly 按自然日。
        return {"monthly": 45, "weekly": 15}.get(self.freq, 2)


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
        code="turnover_total", name_cn="两市成交额(沪深·综指口径)", layer=L1, freq="daily",
        unit="亿元", direction=1, source="tushare", api="index_daily(000001.SH+399106.SZ)",
        sort_order=10,
        note="口径：上证综指+深证综指成交额合计=**沪深两市·不含北交所**(新闻口径)。"
             "实测 20260731 综指合计 25419亿 vs 全A(含BJ) 25599亿·差0.7%——"
             "免去逐日全市场聚合(回补900+次调用)·2次range调用取全。",
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
        code="mainflow_total", name_cn="全市场主力净流入(沪深)", layer=L1, freq="daily",
        unit="亿元", direction=1, source="tushare", api="moneyflow_mkt_dc", sort_order=14,
        note="口径=东财大盘主力(超大单+大单)·沪深两市。**与项目 market_fund 模块**"
             "(Σmoneyflow_dc·剔.BJ·曾与东财直连验证226vs224.6亿吻合)**逐日对比完全一致**："
             "07-29两者均-119.5亿/07-30均-789.9亿/07-31均+625.4亿(2026-08-02实测)。"
             "⚠️单位为**元**(/1e8→亿)·非万元。1次range调用免去900+次逐日聚合。"
             "个股口径的 net_mf_amount 陷阱(约50%符号相反)与此无关·大盘级无该字段。",
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
        code="etf_share_chg", name_cn="宽基ETF份额日变化", layer=L1, freq="daily",
        unit="亿份", direction=1, source="tushare", api="fund_share", lag_days=1, sort_order=16,
        note="篮子=场内基金名称含 沪深300/中证500/1000/2000/上证50/创业板/科创50/A500 的ETF。"
             "**只累计前后两日都存在的基金的Δ**——新上市ETF首日份额直接进总量差分会制造假跳变。"
             "宽基份额增加≈场外资金借道入场(国家队/机构申购的主通道)。",
    ),
    MetricDef(
        code="buyback_amt", name_cn="回购金额(近20交易日)", layer=L1, freq="daily",
        unit="亿元", direction=1, source="tushare", api="repurchase", lag_days=1,
        enabled=False, sort_order=17,
        note="❌暂不接入(2026-08-02探针证据)：repurchase 的 vol/amount 是**回购程序内的累计值**"
             "——实测中石化31行·proc=实施 的连续公告 5596万→7998万→1.11亿→1.41亿严格递增，"
             "且表内**无程序ID**·同一公司多程序并行时Δ推导有二义。直接按行求和会数倍重复计入。"
             "需专项核实字段语义(累计口径/程序边界)后再接·宁可缺这个指标也不放一个虚高数倍的。",
    ),
    MetricDef(
        code="float_release", name_cn="解禁规模(未来4周)", layer=L1, freq="daily",
        unit="亿元", direction=0, no_dist=1, source="tushare", api="share_float(分页·6000行/页上限)",
        lag_days=0, sort_order=18,
        note="⚠️**前瞻计划·非已发生事实**(2026-08-02定档)：未来值没有历史分布可比·"
             "不算分位/z/异动(no_dist=1)·direction=0 不评分——纯展示项。"
             "值=Σ 当日收盘价×未来28自然日待解禁股数(万股→亿元)。"
             "share_float 单次6000行=静默上限·offset分页取满。只算最近≤60个交易日(展示无需长历史)。",
    ),
    MetricDef(
        code="new_fund_share", name_cn="新成立基金份额(近4周)", layer=L1, freq="daily",
        unit="亿份", direction=1, source="tushare", api="fund_basic(E+O·分页)", lag_days=0,
        sort_order=19,
        note="股票型+混合型 issue_amount 按 found_date 聚合·28自然日(≈4周)滚动求和·逐日出点。"
             "⚠️fund_basic 场外表单次15000行=上限·分页取满。"
             "末端2-3日可能因公告入库滞后而低估·每晚 --resync 重写近旬自动修正。",
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

# ──────────────────────────────────────────────
# IND 行业领先指标（领先指标页 /leadind·2026-08-03）
# 全部 direction=0：商品价格对"全A"没有统一方向(铜涨利好矿山利空下游)——
# 方向语义按产业链拆解，见 app/macro/leadind.py 的 IMPACT 映射；不进宏观面板与 TOTAL 评分。
# 数据源：Tushare fut_daily 主力连续(20260803 逐一实测 16/16 可得·当日收盘后即有数)。
# 换月口径：value=主力连续收盘价(真实拼接价·分位/走势用)；换月日 compute 的 chg 会被
# 新旧合约基差污染(约每月1次)——异动侧由 V3 连续2日确认天然防御(跳空是单日毛刺)，
# 显示侧由 leadind 服务用 fut_mapping 识别换月日并按新主力自身昨收(pre_close·实测换月感知)
# 重算当日涨跌幅。unit 以 元/ 开头 → compute.changes 走涨跌幅%而非绝对差。
# ──────────────────────────────────────────────
IND = "IND_leading"


def _fut(code: str, name_cn: str, ts_code: str, unit: str, order: int, note: str = "",
         enabled: bool = True) -> MetricDef:
    """期货主力连续指标的紧凑构造（16个定义重复字段太多·一处收拢）。"""
    return MetricDef(
        code=code, name_cn=name_cn, layer=IND, freq="daily", unit=unit, direction=0,
        source="tushare", api=f"fut_daily({ts_code})", sort_order=order, enabled=enabled,
        note=note or "主力连续收盘价·20260803实测2023年起全量可得·收盘后当日即更新。",
    )


_IND: list[MetricDef] = [
    _fut("fut_lh", "生猪期货(主力)", "LH.DCE", "元/吨", 10),
    _fut("fut_c", "玉米期货(主力)", "C.DCE", "元/吨", 11),
    _fut("fut_rb", "螺纹钢期货(主力)", "RB.SHF", "元/吨", 20),
    _fut("fut_i", "铁矿石期货(主力)", "I.DCE", "元/吨", 21),
    _fut("fut_fg", "玻璃期货(主力)", "FG.ZCE", "元/吨", 22),
    _fut("fut_sa", "纯碱期货(主力)", "SA.ZCE", "元/吨", 23),
    _fut("fut_lc", "碳酸锂期货(主力)", "LC.GFE", "元/吨", 30,
         note="2023-07-21 上市(广期所)·分位样本自上市起累计·20260803实测735行。"),
    _fut("fut_cu", "沪铜期货(主力)", "CU.SHF", "元/吨", 40),
    _fut("fut_al", "沪铝期货(主力)", "AL.SHF", "元/吨", 41),
    _fut("fut_au", "沪金期货(主力)", "AU.SHF", "元/克", 42),
    _fut("fut_ag", "沪银期货(主力)", "AG.SHF", "元/千克", 43),
    _fut("fut_sc", "原油期货(主力·INE)", "SC.INE", "元/桶", 50),
    _fut("fut_ur", "尿素期货(主力)", "UR.ZCE", "元/吨", 51),
    _fut("fut_v", "PVC期货(主力)", "V.DCE", "元/吨", 52),
    _fut("fut_sp", "纸浆期货(主力)", "SP.SHF", "元/吨", 53),
    _fut("fut_ec", "集运指数期货(欧线·主力)", "EC.INE", "点", 60,
         note="2023-08-18 上市(上期能源)·标的=SCFIS欧线结算运价指数·分位样本自上市起累计。"),
    # 天气敏感农产品（2026-08-03 用户提出厄尔尼诺跟踪·实测5/5可得）：
    # 气候异常(厄尔尼诺/拉尼娜)对板块的影响最终打进这些价格——期货价=市场对天气信息的日频聚合
    _fut("fut_sr", "白糖期货(主力)", "SR.ZCE", "元/吨", 70),
    _fut("fut_p", "棕榈油期货(主力)", "P.DCE", "元/吨", 71),
    _fut("fut_m", "豆粕期货(主力)", "M.DCE", "元/吨", 72),
    _fut("fut_cf", "棉花期货(主力)", "CF.ZCE", "元/吨", 73),
    _fut("fut_ap", "苹果期货(主力)", "AP.ZCE", "元/吨", 74),
    MetricDef(
        code="enso_oni", name_cn="厄尔尼诺指数(ONI)", layer=IND, freq="monthly",
        unit="", direction=0, source="", api="NOAA CPC ONI", enabled=False, sort_order=80,
        note="❌待接入：NOAA ONI(Niño3.4 三月滑动海温距平·月更)是厄尔尼诺/拉尼娜的官方判据，"
             "但属**境外源——服务器必须实测**(纪律：本地测通不代表腾讯云夜间任务能跑)。"
             "接入前气候影响由天气敏感农产品期货价代理(白糖/棕榈油/豆粕——天气信息的日频聚合)。"
             "备选通道：NOAA CPC 文本文件 > 国家气候中心ENSO监测(需爬虫)。独立小任务·不阻塞主线。",
    ),
]

METRICS: tuple[MetricDef, ...] = tuple(_L0 + _L0_MONTHLY + _L1 + _L2 + _L3 + _IND)

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


# ──────────────────────────────────────────────
# 卡片级「这是什么」——静态知识·不随日期变·不用 LLM（2026-08-02 用户定档）。
# 内容四件套：①定义/机制 ②在传导链条里的位置 ③什么水平算高/低(教读分位·不给操作含义)
# ④已知陷阱。目的：教用户怎么读数·帮其自建手感——**不是**替用户下结论。
# ──────────────────────────────────────────────
EXPLAIN: dict[str, str] = {
    "fdr007": "银行间市场存款类机构7天质押式回购的**定盘利率**(每日11:00定盘)——银行之间短钱的价格，"
              "是观察央行松紧最灵敏的日频窗口。链条位置：央行操作→银行间利率→实体/市场流动性的第一环。"
              "怎么读：绝对值看与政策利率(7天逆回购利率)的相对位置，日常主要看分位——高分位=资金面紧。"
              "陷阱：跨月/跨季/春节前例行冲高，单日尖峰≠转向；FDR007是定盘价不是全天加权的DR007，"
              "两者高度相关但数值有差。",
    "cn_10y": "中国10年期国债收益率——无风险利率的锚·全市场定价的分母。链条位置：既反映增长/通胀预期，"
              "也反映货币松紧的中期定价。怎么读：下行通常伴随宽松与避险(债牛)，上行伴随复苏预期或收紧。"
              "陷阱：**方向对股市是双刃**——利率上行压估值(尤其成长股)，但若因经济复苏而上行，"
              "顺周期板块反而受益；本面板按教科书口径记为利空高值，读的时候要带着这层辩证。",
    "cn_term_spread_10y2y": "中债期限利差=10Y−2Y。曲线形态浓缩了'现在的政策'与'未来的增长'的相对定价："
                            "陡峭化(利差走阔)=短端被压低(宽松)+长端稳(增长预期没垮)·历史上是股市友好组合；"
                            "平坦化/倒挂=紧货币或增长预期恶化。陷阱：陡峭化有两种——'牛陡'(短端降·好)与"
                            "'熊陡'(长端升·要辨认因何而升)，同一个数字两种含义。",
    "us_10y": "美债10年收益率——全球资产定价之锚。链条位置：美元流动性→全球风险资产→A股(经北向情绪与"
              "汇率两条路)。怎么读：快速上行=全球流动性收紧·压制成长股估值(对A股半导体/创新药等久期长的"
              "板块传导最直接)。陷阱：影响A股的是**变化速度**多于绝对水平。",
    "us_2y": "美债2年收益率——比10Y更贴近美联储政策路径预期(加降息预期几乎直接映射)。怎么读：2Y快速下行"
             "通常=市场开始定价降息。与10Y联动看：2Y降得比10Y快=曲线陡峭化·宽松交易。",
    "cn_us_spread_10y": "中美利差=中10Y−美10Y(当前为负=倒挂)。链条位置：决定人民币资产相对吸引力→"
                        "汇率压力→外资进出意愿。怎么读：利差收窄(向0回升)=贬值压力缓解=偏多；"
                        "继续走阔(更负)=资金外流压力。陷阱：它是**果也是因**——常滞后于两国基本面预期差。",
    "usdcnh": "离岸人民币汇率(数值↑=人民币贬值)。选离岸因为它不受在岸中间价管理·对情绪更敏感。"
              "链条位置：美元强弱+中国基本面预期→汇率→外资流向与政策空间。怎么读：急贬往往伴随A股承压"
              "(外资流出+宽松空间受限)，企稳回升常是行情修复的前置条件之一。陷阱：单看它分不清是"
              "'美元强'还是'人民币弱'——需配美元指数拆分(dxy待接入前可对照中美利差)。",
    "shibor_3m": "3个月Shibor——银行间**中期**资金价格·比隔夜/7天更能反映银行对未来一季资金面的预期。"
                 "与FDR007搭配读：短端松+3M降=宽松在延续；短端松但3M抬头=市场预期宽松接近尾声。",
    "m1_yoy": "M1同比——企业**活期**存款为主的狭义货币。企业把钱放活期=准备开工/投资/发工资，"
              "所以M1是经营活跃度与'钱是否愿意动起来'的领先信号，对A股的领先性优于M2。"
              "怎么读：M1回升+M1-M2剪刀差收窄=资金活化=历史上常与行情启动同步或领先。"
              "陷阱：春节错位月份会大幅失真·看趋势别看单月。",
    "m2_yoy": "M2同比——广义货币总量(含定期/储蓄)。反映银行体系信用扩张的总闸门。怎么读：与M1对照——"
              "M2高M1低=钱在定期里趴着(宽货币未到实体)；两者同升=真扩张。",
    "social_finance_inc": "社融当月增量——实体经济从金融体系拿到的全部新增融资(贷款+债券+表外)。"
                          "链条位置：政策意图→信用扩张→企业盈利的传导起点，历史上社融拐点领先"
                          "盈利拐点约2-3个季度。怎么读：看同比多增/少增与结构(中长贷占比)比看总量更有效。"
                          "陷阱：月度波动极大且有明显季节性(1月天量·7/10月低谷)，读分位前先想季节。",
    "cpi_yoy": "CPI同比——居民端通胀。对A股是双刃：温和通胀(1-3%)=需求健康·利好消费；过高挤压政策空间，"
               "过低(通缩)=需求不足。因此本面板设为中性(不参与评分)，读它主要为判断'政策还有多少空间'。",
    "ppi_yoy": "PPI同比——工业品出厂价格。链条位置：直接映射中上游企业盈利(PPI回升=工业企业利润修复)。"
               "怎么读：PPI从负区间向0回升的阶段·历史上常对应顺周期/资源板块的盈利兑现期。"
               "陷阱：受基数与大宗商品影响大·同比读数要配环比。",
    "pmi_mfg": "制造业PMI——唯一在**当月末**就发布的月度景气调查(其余月度数据都滞后半个月)，50为荣枯线。"
               "怎么读：绝对值离50的距离+连续方向比单月读数重要；49.5→50.2这种穿线比52→51.5信息量大。"
               "陷阱：样本偏大中型企业·小企业景气看财新PMI对照。",
    "lpr_1y": "1年期LPR——企业短期贷款定价基准·每月20日报价。降=真金白银的宽松落地(不只是预期)。"
              "怎么读：LPR动一次的信号意义>幅度本身；连续数月不动+FDR007持续低位=宽松在观察期。",
    "lpr_5y": "5年期以上LPR——房贷定价基准。降5Y=直接指向地产·历史上对地产链/银行板块传导最直接。"
              "与1Y分开动时看结构意图(只降5Y=定向托地产)。",
    "sox": "费城半导体指数——全球半导体产业景气的定价基准。链条位置：全球半导体周期→A股半导体链"
           "(权重2倍·用户主战场)。怎么读：SOX与A股半导体的相关性在'产业逻辑驱动'阶段最强·"
           "在'国产替代逻辑'阶段会阶段性脱钩——背离本身就是信息。",
    "nasdaq": "纳斯达克综合指数——全球成长股风险偏好的锚。隔夜纳指大跌→A股科技开盘承压是最短的传导链，"
              "但持续性取决于A股自身流动性(L0/L1)。",
    "hk_tech": "恒生科技指数——离A股最近的'外资定价中国科技'样本(同一批公司·不同资金定价)。"
               "怎么读：与A股科技板块背离时·通常是内外资分歧——港股先走弱常预示外资情绪转冷。",
    "turnover_total": "沪深两市成交额(综指口径·不含北交所)——市场热度与流动性的直接体温。"
                      "链条位置：宏观流动性→场内活跃度→赚钱效应的中枢环节。怎么读：与自身分位比"
                      "(万亿在2024是天量·2026是常态)；持续放大+指数走平=分歧加大，缩量新高=谨慎信号。"
                      "陷阱：绝对值跨年不可比·必须看分位。",
    "margin_ratio": "融资余额/A股流通市值——杠杆资金的**相对**参与度(主指标)。比绝对余额科学：余额随市值"
                    "自然增长·比值才有均值回复(近三年约1.9%~2.9%区间摆动)。怎么读：高分位=杠杆情绪亢奋"
                    "(脆弱性积累)，低分位=杠杆出清。陷阱：两次保证金比例调整(20230911放宽/20260119收紧·"
                    "新老划断渐进4-8周)造成制度性台阶——断点附近的分位变化别当情绪变化读。",
    "margin_balance": "融资余额绝对值(亿)——只作规模参照·不参与评分：三年从1.5万亿单边涨到3万亿·"
                      "分位永远贴100%·没有可比信息量。看相对参与度请用上面的比值版。",
    "margin_buy_ratio": "融资买入额/两市成交额——杠杆资金的**当日**参与强度。余额是存量(慢)·买入占比是"
                        "流量(快)，情绪拐点上流量先动。怎么读：占比冲高=杠杆客在抢筹·常见于行情加速段"
                        "(也是脆弱段)。",
    "mainflow_total": "全市场主力净流入(超大单+大单·东财口径·沪深)。怎么读：**日内数值噪音大**·连续多日"
                      "同向才有信息；与指数背离时(指数涨+主力持续净流出)警惕兑现。陷阱：'主力'是按单笔"
                      "金额切的统计口径·不等于机构——大单也可能是散户市价单撮合的碎片。",
    "northbound_turnover": "北向**成交额**(≠净流入)。2024-08-19起交易所不再披露净买入·此列只能作"
                           "外资参与活跃度的代理。怎么读：成交额放大=外资在积极调仓(方向未知)；"
                           "本指标设为中性·不判多空。陷阱：任何声称'北向净流入'的日频数据在该日期后"
                           "都是口径错误或估算——本面板拒绝展示。",
    "etf_share_chg": "宽基ETF份额日变化(亿份)——场外资金借道入场的主通道·也是'国家队'惯用工具。"
                     "怎么读：大跌日份额大增=承接资金(历史上多次见于汇金增持窗口)；"
                     "连续净申购=场外在系统性加仓。陷阱：份额≠金额(需乘净值)·且T日份额T+1才结算完整。",
    "new_fund_share": "近4周新成立的股票+混合型基金发行份额——增量资金的先行指标·也是散户情绪温度计"
                      "(好发基金的时候往往是情绪高点)。怎么读：冰点(发不动)常对应市场底部区域·"
                      "天量发行反而要警惕——这是少数'高分位偏危险'的指标。",
    "float_release": "未来4周解禁市值(按现价估)——**前瞻供给压力**·不是已发生的抛售。怎么读：解禁≠减持"
                     "(大股东/战投解禁后多数不立刻卖)·真正冲击集中在小盘股占流通盘比例高的个案；"
                     "整体数值主要用于提示'供给窗口密集期'。无历史分布可比·不算分位不评分。",
    # ── IND 行业领先指标（通用陷阱见页面口径栏：主力连续·换月日涨跌幅已按新主力自身昨收）──
    "fut_lh": "生猪主力期货价——猪周期的日频镜子(现货口径的官方数据是周频/月频·期货最快)。"
              "怎么读：猪价决定养殖股盈利方向·但股价常领先猪价见底(定价的是产能去化预期)——"
              "低分位+能繁去化传闻=市场开始交易反转。陷阱：期货贴水/升水现货是常态·看方向别抠绝对值。",
    "fut_c": "玉米主力期货价——养殖的最大成本项(饲料约占养殖成本6成·玉米是饲料主粮)。"
             "怎么读：与生猪价对照成'利润剪刀'——猪涨玉米跌=养殖利润双击。",
    "fut_rb": "螺纹钢主力期货价——地产开工+基建需求的价格投票。怎么读：与铁矿对照看钢厂利润"
              "(螺纹涨矿不涨=利润走阔·利好钢铁股)；单边涨常伴随'稳增长预期'交易。",
    "fut_i": "铁矿石主力期货价——钢厂的成本端+全球黑色需求预期。怎么读：矿强钢弱=钢厂利润被挤压"
             "(利空钢铁股毛利)；与螺纹同涨=真实需求预期在改善。",
    "fut_fg": "玻璃主力期货价——地产**竣工端**景气(玻璃装在交房前)。怎么读：领先/同步于家电家居"
              "等竣工后周期板块的需求逻辑；涨价直接利好浮法玻璃企业盈利。",
    "fut_sa": "纯碱主力期货价——下游六成是玻璃(浮法+光伏压延)。怎么读：光伏装机预期升温常先打到"
              "纯碱/光伏玻璃链条；与玻璃期货对照分辨是地产逻辑还是光伏逻辑。",
    "fut_lc": "碳酸锂主力期货价——锂电产业链的定价锚。怎么读：**方向对上下游相反**——涨利好锂矿"
              "(资源端量价齐升)·压电池/整车成本；2023年以来的漫长下行=供给过剩出清期·"
              "低分位企稳+排产回暖才是链条整体的信号。",
    "fut_cu": "沪铜主力期货价——'铜博士'·全球制造业需求温度计+电网/新能源用铜。怎么读：涨利好铜矿股"
              "(盈利弹性直接)；与美元指数常反向·辨认是需求驱动还是货币驱动。",
    "fut_al": "沪铝主力期货价——地产/汽车/光伏边框需求+电解铝供给天花板。怎么读：涨利好电解铝"
              "(产能受限·价格弹性大部分落到利润)。",
    "fut_au": "沪金主力期货价——避险+实际利率的镜子(与美债实际收益率反向)。怎么读：涨利好黄金股·"
              "但黄金股弹性=金价×矿产量·牛市中股票通常放大金价涨幅。",
    "fut_ag": "沪银主力期货价——半贵金属半工业品(光伏浆料是重要工业需求)。怎么读：金银比走低"
              "(银补涨)常出现在贵金属行情后段·情绪偏亢奋。",
    "fut_sc": "INE原油主力期货价——能源与化工链的总成本锚。怎么读：涨利好油气开采/油服·"
              "利空航空(燃油成本)/物流/以油为原料的化工；急涨急跌都要看是供给事件还是需求预期。",
    "fut_ur": "尿素主力期货价——农需(用肥季)+出口政策的价格投票。怎么读：涨利好氮肥/煤化工企业；"
              "季节性强(春耕/秋播)·读分位比读绝对价重要。",
    "fut_v": "PVC主力期货价——地产开工链化工品(管材型材)。怎么读：与螺纹同向时=地产链共振信号；"
             "涨利好氯碱企业。",
    "fut_sp": "纸浆主力期货价——造纸的成本端。怎么读：**方向对纸企偏反向**——浆价涨压缩下游纸企毛利"
              "(除非浆纸一体化)；浆价见顶回落+纸价提价落地=纸企利润修复窗口。",
    "fut_ec": "集运指数期货(欧线)——SCFIS欧线运价的远期定价·航运景气最快的日频信号。"
              "怎么读：涨=运价预期走强·利好集运(中远海控等)；受红海绕行/欧洲需求/运力交付多重驱动·"
              "波动极大(单日±10%不罕见)·读趋势别读单日。",
    "fut_sr": "白糖主力期货价——**厄尔尼诺交易的主战场之一**(印度/泰国干旱→甘蔗减产→糖价涨)。"
              "怎么读：涨利好制糖(中粮糖业等)；国际糖价+配额政策共同定价·气候异常年份弹性最大。",
    "fut_p": "棕榈油主力期货价——东南亚天气的价格镜子(厄尔尼诺→马来/印尼干旱→减产)。"
             "怎么读：涨利好棕榈油贸易/油脂加工；与豆油价差决定替代需求·气候是最大供给变量。",
    "fut_m": "豆粕主力期货价——南美天气(巴西/阿根廷大豆产区)+饲料需求的价格投票。"
             "怎么读：涨=饲料成本升·利空养殖利润·利好饲料贸易环节；美豆种植季的天气市波动最大。",
    "fut_cf": "棉花主力期货价——新疆天气+全球纺织需求。怎么读：涨利好棉花种植/贸易(新农开发等)·"
              "压纺织服装成本端；收储/滑准税政策扰动大·读分位配政策背景。",
    "fut_ap": "苹果主力期货价——最纯粹的'天气期货'(花期冻害直接定价)。怎么读：对A股映射窄"
              "(果链贸易企业少)·主要价值是**验证天气炒作的真实性**——天气题材热而苹果糖不动=炒作虚。",
}


def validate() -> list[str]:
    """自检注册表一致性，返回问题列表（空=通过）。CI/单测与 `macro-sync` 启动时都会跑。"""
    from app.macro.store import ALL_LAYERS
    problems: list[str] = []
    seen: set[str] = set()
    for m in METRICS:
        if m.code in seen:
            problems.append(f"{m.code}: 重复定义")
        seen.add(m.code)
        if m.layer not in ALL_LAYERS:
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
        if m.no_dist and m.direction != 0:
            problems.append(f"{m.code}: no_dist=1(无历史分布·不算分位) 必须 direction=0"
                            "(分位都没有·评分翻转无从谈起)")
        if m.enabled and not EXPLAIN.get(m.code):
            problems.append(f"{m.code}: 启用指标必须有 explain(卡片级『这是什么』·教学内容)")
    return problems
