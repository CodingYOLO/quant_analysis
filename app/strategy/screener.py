"""
量化因子选股引擎（收盘后完整因子版）。

职责：
  - build_factor_table(date)：基于当日收盘全量数据计算每只股票的因子，缓存 parquet
  - FACTOR_GROUPS：可选因子定义（分组 + 筛选逻辑），供前端渲染按钮
  - screen(date, selected, custom)：按选中因子组合筛选，返回结果表

设计：
  - 因子表一天只算一次（重，~30-60s），缓存到 data_cache/factor_table/{date}.parquet
  - 筛选在缓存表上完成（毫秒级）
  - 所有数据走 CompositeProvider，禁止直接调 akshare/tushare
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from app import factors as F
from app.config import get_settings
from app.data.composite_provider import CompositeProvider
from app.data.history_loader import load_price_matrix
from app.factors.patterns import price_volume as _pv  # noqa: F401  触发形态注册
from app.factors.patterns.base import PATTERN_REGISTRY, detect_all

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 因子定义（分组）— key 对应因子表里的派生布尔/数值列
# 每个因子：label 显示名，col 依赖列，op 与 val 构成过滤条件
# ──────────────────────────────────────────────

FACTOR_GROUPS = [
    {
        "group": "估值与市值",
        "factors": [
            {"key": "pe_le30", "label": "市盈率0~30(剔亏损)", "col": "pe_ttm", "op": "between", "val": [0.01, 30], "pos": True},
            {"key": "pb_le3", "label": "市净率0~3", "col": "pb", "op": "between", "val": [0.01, 3], "pos": True},
            {"key": "mv_ge500", "label": "总市值≥500亿", "col": "total_mv_100m", "op": "ge", "val": 500},
            {"key": "mv_50_200", "label": "总市值50-200亿", "col": "total_mv_100m", "op": "between", "val": [50, 200]},
            {"key": "circ_ge100", "label": "流通市值≥100亿", "col": "circ_mv_100m", "op": "ge", "val": 100},
            {"key": "circ_le50", "label": "流通市值≤50亿", "col": "circ_mv_100m", "op": "le", "val": 50, "pos": True},
            {"key": "only_leader", "label": "🏆只看板块龙头(行业内前2)", "col": "is_leader", "op": "true"},
        ],
    },
    {
        "group": "量能与活跃度",
        "factors": [
            {"key": "limit_up", "label": "今日涨停", "col": "is_limit_up", "op": "true"},
            {"key": "turnover_ge3", "label": "换手率≥3%", "col": "turnover_rate", "op": "ge", "val": 3},
            {"key": "turnover_lt1", "label": "换手率<1%(低)", "col": "turnover_rate", "op": "lt", "val": 1},
            {"key": "vol_ratio_ge15", "label": "量比≥1.5", "col": "volume_ratio", "op": "ge", "val": 1.5},
            {"key": "amount_ge1", "label": "成交额≥1亿", "col": "amount_100m", "op": "ge", "val": 1},
        ],
    },
    {
        "group": "趋势与均线",
        "factors": [
            {"key": "above_ma5", "label": "站上MA5", "col": "above_ma5", "op": "true"},
            {"key": "above_ma10", "label": "站上MA10", "col": "above_ma10", "op": "true"},
            {"key": "above_ma20", "label": "站上MA20(机构生命线)", "col": "above_ma20", "op": "true"},
            {"key": "stable_above_ma20", "label": "稳站20日线(近5日·机构生命线)", "col": "stable_above_ma20", "op": "true"},
            {"key": "above_ma60", "label": "站上MA60", "col": "above_ma60", "op": "true"},
            {"key": "above_ma90", "label": "站上MA90", "col": "above_ma90", "op": "true"},
            {"key": "above_ma120", "label": "站上半年线MA120", "col": "above_ma120", "op": "true"},
            {"key": "above_ma144", "label": "站上MA144", "col": "above_ma144", "op": "true"},
            {"key": "above_ma250", "label": "站上年线MA250⭐", "col": "above_ma250", "op": "true"},
            {"key": "ma_bull_full", "label": "均线多头排列(5>10>20>60)", "col": "ma_bull_full", "op": "true"},
            {"key": "ma250_up", "label": "年线向上(牛熊确认)", "col": "ma250_up", "op": "true"},
            {"key": "ma20_up", "label": "MA20拐头向上", "col": "ma20_up", "op": "true"},
            {"key": "ema_bull", "label": "EMA14>EMA26(多头)", "col": "ema_bull", "op": "true"},
            {"key": "rps50_ge70", "label": "RPS50≥70", "col": "rps50", "op": "ge", "val": 70},
            {"key": "rps120_ge70", "label": "RPS120≥70", "col": "rps120", "op": "ge", "val": 70},
            {"key": "rps_ge80", "label": "RPS综合≥80", "col": "rps_combo", "op": "ge", "val": 80},
            {"key": "sector_strong", "label": "板块走强(行业RPS中位≥55)", "col": "sector_strong", "op": "true"},
        ],
    },
    {
        "group": "技术与资金",
        "factors": [
            {"key": "macd_gold", "label": "MACD金叉", "col": "macd_gold", "op": "true"},
            {"key": "kdj_gold", "label": "KDJ金叉(低位)", "col": "kdj_gold", "op": "true"},
            {"key": "td_buy9", "label": "TD神奇九转(买入9)", "col": "td_buy9", "op": "true"},
            {"key": "long_lower", "label": "长下影线(承接)", "col": "long_lower", "op": "true"},
            {"key": "long_upper", "label": "长上影线(抛压)", "col": "long_upper", "op": "true"},
            {"key": "rsi_oversold", "label": "RSI超卖(<30)", "col": "rsi14", "op": "lt", "val": 30, "pos": True},
            {"key": "rsi_strong", "label": "RSI强势(50-70)", "col": "rsi14", "op": "between", "val": [50, 70]},
            {"key": "main_inflow", "label": "主力净流入>0", "col": "main_net_amount", "op": "gt", "val": 0},
            {"key": "elg_inflow", "label": "超大单净流入>0", "col": "elg_net", "op": "gt", "val": 0},
            {"key": "vwap_low", "label": "VWAP低吸回踩(±3%)", "col": "vwap_dev", "op": "between", "val": [-3, 3], "pos": True},
        ],
    },
    {
        "group": "🐌 慢牛吸筹（多日·主力悄悄进）",
        "factors": [
            {"key": "accum_score60", "label": "吸筹评分≥60", "col": "accum_score", "op": "ge", "val": 60},
            {"key": "accum_quiet_vol", "label": "温和放量(5/20日量比1.2~2.5)", "col": "vol5_vol20", "op": "between", "val": [1.2, 2.5]},
            {"key": "accum_volprice", "label": "量价配合(涨放量·跌缩量)", "col": "up_down_vol", "op": "ge", "val": 1.1},
            {"key": "accum_ma20_up", "label": "MA20斜率向上", "col": "ma20_slope", "op": "gt", "val": 0},
            {"key": "accum_slow_rise", "label": "缓慢走高(近20日3~25%)", "col": "ret20", "op": "between", "val": [3, 25]},
            {"key": "accum_contract", "label": "振幅收敛(锁筹·近<前)", "col": "amp_contract", "op": "lt", "val": 1.0},
            {"key": "accum_low_amp", "label": "低波动(近20日均振幅≤5.5%)", "col": "amp20", "op": "le", "val": 5.5},
            {"key": "accum_hidden", "label": "隐蔽(近20日无大涨/涨停)", "col": "big_up_days_20", "op": "le", "val": 0},
            {"key": "accum_fund3d", "label": "主力近3日净流入(大单估算·弱)", "col": "main_net_3d", "op": "gt", "val": 0},
            {"key": "repeat_inflow6", "label": "主力反复净流入(近10日≥6天·估算)", "col": "inflow_days_10", "op": "ge", "val": 6},
            {"key": "consec_inflow3", "label": "主力连续净流入≥3天(估算·真持续)", "col": "consec_inflow", "op": "ge", "val": 3},
            {"key": "sector_inflow5", "label": "板块资金偏持续流入(近10日≥5天·估算)", "col": "sector_inflow_days", "op": "ge", "val": 5},
            {"key": "fund_sustained", "label": "真·持续吸筹(非脉冲·近期未退潮·估算)", "col": "fund_sustained", "op": "true"},
            {"key": "fund_no_pulse", "label": "剔除假流入(脉冲后退潮·估算)", "col": "fund_pulse_fade", "op": "false"},
        ],
    },
    {
        "group": "🔥 妖股/情绪启动（连板基因+启动+排雷）",
        "factors": [
            {"key": "demon_gene2", "label": "连板基因·近60日涨停≥2次", "col": "limit_ups_60d", "op": "ge", "val": 2},
            {"key": "demon_youzi_relay", "label": "🔥有游资接力(近20日≥2日)", "col": "youzi_relay_days", "op": "ge", "val": 2},
            {"key": "demon_youzi_any", "label": "🔥近期有游资进场(≥1日)", "col": "youzi_relay_days", "op": "ge", "val": 1},
            {"key": "demon_board2", "label": "有过连板(≥2连板)", "col": "max_consec_limit", "op": "ge", "val": 2},
            {"key": "demon_lu_today", "label": "今日涨停(点火)", "col": "is_limit_up", "op": "true"},
            {"key": "demon_hot_turn", "label": "高换手≥7%(情绪活跃)", "col": "turnover_rate", "op": "ge", "val": 7},
            {"key": "demon_volup", "label": "放量·量比≥2", "col": "volume_ratio", "op": "ge", "val": 2},
            {"key": "demon_lowprice", "label": "低价≤15元(可选偏好·非必须)", "col": "close", "op": "le", "val": 15},
            {"key": "demon_small_float", "label": "小流通≤50亿(可选·偏好微盘妖)", "col": "circ_mv_100m", "op": "le", "val": 50},
            {"key": "demon_midfloat", "label": "中小流通≤150亿(可选·剔超大盘)", "col": "circ_mv_100m", "op": "le", "val": 150},
            {"key": "risk_not_st", "label": "🛡排雷·排除ST/*ST", "col": "is_st", "op": "false"},
            {"key": "risk_pos_equity", "label": "🛡排雷·净资产为正(PB>0)", "col": "pb", "op": "gt", "val": 0},
            {"key": "risk_debt70", "label": "🛡排雷·资产负债率≤70%", "col": "debt_to_assets", "op": "le", "val": 70},
            {"key": "risk_not_huge_loss", "label": "🛡排雷·净利同比≥-50%(非断崖)", "col": "netprofit_yoy", "op": "ge", "val": -50},
        ],
    },
    {
        "group": "📈 业绩催化（二季度中报预告·7-8月旺季）",
        "factors": [
            {"key": "earn_h1", "label": "有中报预告(二季度催化)", "col": "is_h1_forecast", "op": "true"},
            {"key": "earn_good", "label": "业绩预喜(预增/扭亏/略增)", "col": "earn_good", "op": "true"},
            {"key": "earn_chg30", "label": "预告净利增≥30%", "col": "forecast_chg", "op": "ge", "val": 30},
            {"key": "earn_chg100", "label": "预告净利翻倍(≥100%)", "col": "forecast_chg", "op": "ge", "val": 100},
        ],
    },
    {
        "group": "💎 强势龙头·超跌低吸（错杀的好票）",
        "factors": [
            {"key": "dip_strong", "label": "前期强势(RPS120≥70)", "col": "rps120", "op": "ge", "val": 70},
            {"key": "dip_oversold5", "label": "近5日超跌(≤-5%)", "col": "ret5", "op": "le", "val": -5},
            {"key": "dip_rsi", "label": "RSI回落(≤45)", "col": "rsi14", "op": "le", "val": 45},
            {"key": "dip_trend_ok", "label": "趋势未破·站上MA60(防接刀)", "col": "above_ma60", "op": "true"},
            {"key": "dip_leader", "label": "龙头体格·流通≥100亿", "col": "circ_mv_100m", "op": "ge", "val": 100},
            {"key": "dip_liquid", "label": "成交活跃·额≥3亿", "col": "amount_100m", "op": "ge", "val": 3},
        ],
    },
    {
        "group": "情绪与人气",
        "factors": [
            {"key": "comment_ge80", "label": "千评得分≥80", "col": "comment_score", "op": "ge", "val": 80},
            {"key": "inst_ge50", "label": "机构参与度≥50%", "col": "institution_pct", "op": "ge", "val": 50},
            {"key": "popular_top500", "label": "人气排名前500", "col": "popularity_rank", "op": "le", "val": 500, "pos": True},
        ],
    },
    {
        # K线/量价形态（由形态注册表自动生成，新增形态零侵入）
        "group": "K线形态/量价",
        "factors": [
            {"key": f"pat_{k}", "label": p.label, "col": f"pat_{k}", "op": "true"}
            for k, p in PATTERN_REGISTRY.items()
        ],
    },
]

# 自定义条件可选字段（任意数值列 + 操作符 + 值），供前端「自定义条件」下拉
CUSTOM_FIELDS = [
    {"col": "pe_ttm", "label": "市盈率PE"}, {"col": "pb", "label": "市净率PB"},
    {"col": "total_mv_100m", "label": "总市值(亿)"}, {"col": "circ_mv_100m", "label": "流通市值(亿)"},
    {"col": "turnover_rate", "label": "换手率%"}, {"col": "volume_ratio", "label": "量比"},
    {"col": "amount_100m", "label": "成交额(亿)"}, {"col": "amplitude", "label": "振幅%"},
    {"col": "pct_chg", "label": "当日涨跌%"}, {"col": "rps50", "label": "RPS50"},
    {"col": "rps120", "label": "RPS120"}, {"col": "rps_combo", "label": "RPS综合"},
    {"col": "rsi14", "label": "RSI"}, {"col": "vwap_dev", "label": "VWAP偏离%"},
    {"col": "main_net_amount", "label": "主力净流入(亿)"}, {"col": "elg_net", "label": "超大单(亿)"},
    {"col": "comment_score", "label": "千评得分"}, {"col": "institution_pct", "label": "机构参与度%"},
    {"col": "popularity_rank", "label": "人气排名"},
    {"col": "accum_score", "label": "🐌吸筹评分"}, {"col": "ret20", "label": "近20日涨幅%"},
    {"col": "vol5_vol20", "label": "量能比5/20日"}, {"col": "ma20_slope", "label": "MA20斜率%"},
    {"col": "amp20", "label": "近20日均振幅%"}, {"col": "main_net_3d", "label": "主力近3日(亿)"},
    {"col": "inflow_days_10", "label": "主力净流入天数(近10)"}, {"col": "consec_inflow", "label": "连续净流入天数"},
    {"col": "sector_inflow_days", "label": "板块流入天数(近10)"},
    {"col": "fund_pulse_ratio", "label": "资金脉冲集中度"}, {"col": "fund_recent2d", "label": "近2日资金(亿)"},
    {"col": "up_down_vol", "label": "量价配合(涨量/跌量)"}, {"col": "amp_contract", "label": "振幅收敛比"},
    {"col": "ret5", "label": "近5日涨幅%"},
    {"col": "limit_ups_60d", "label": "近60日涨停数"}, {"col": "max_consec_limit", "label": "最高连板"},
    {"col": "youzi_relay_days", "label": "游资接力天数"}, {"col": "forecast_chg", "label": "预告净利变动%"},
    {"col": "debt_to_assets", "label": "资产负债率%"}, {"col": "netprofit_yoy", "label": "净利同比%"},
    {"col": "roe", "label": "ROE%"},
]
_CUSTOM_COLS = {f["col"] for f in CUSTOM_FIELDS}
_CUSTOM_OPS = {"ge", "gt", "le", "lt", "eq"}

# 结果表展示列（顺序）
DISPLAY_COLS = [
    ("ts_code", "代码"), ("name", "名称"), ("is_leader", "🏆龙头"), ("accum_score", "🐌吸筹分"), ("industry", "行业"),
    ("close", "最新价"), ("pct_chg", "涨跌幅%"), ("amplitude", "振幅%"),
    ("ret5", "近5日涨%"), ("ret20", "近20日涨%"), ("vol5_vol20", "量能比5/20"),
    ("up_down_vol", "量价配合"),
    ("limit_ups_60d", "60日涨停"), ("max_consec_limit", "最高连板"),
    ("youzi_relay_days", "🔥游资接力日"),
    ("forecast_type", "业绩预告"), ("forecast_chg", "预告净利%"),
    ("debt_to_assets", "负债率%"), ("netprofit_yoy", "净利同比%"),
    ("turnover_rate", "换手%"), ("volume_ratio", "量比"), ("circ_mv_100m", "流通市值(亿)"),
    ("main_net_amount", "主力净流入(亿)"), ("main_net_3d", "主力3日(亿)"), ("elg_net", "超大单(亿)"),
    ("inflow_days_10", "💰流入天数(近10)"), ("consec_inflow", "连续流入天"), ("sector_inflow_days", "板块流入天"),
    ("rps50", "RPS50"), ("rps120", "RPS120"), ("rsi14", "RSI"),
    ("vwap_dev", "VWAP偏离%"), ("comment_score", "千评分"), ("popularity_rank", "人气排名"),
]


# ──────────────────────────────────────────────
# 因子表构建（重，按日缓存）
# ──────────────────────────────────────────────

# 因子表结构版本：新增因子列时 +1，使旧缓存自动失效重算（避免读到缺列的旧表）
_FACTOR_TABLE_VERSION = "v16"  # v16: 加 consec_limit_now(截至昨收当前连板·情绪温度计连板梯队/晋级率用)；v15 关键位数值


def _factor_cache_path(date: str) -> Path:
    settings = get_settings()
    p = settings.cache_dir / "factor_table"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{date}_{_FACTOR_TABLE_VERSION}.parquet"


def build_factor_table(date: str, provider: CompositeProvider | None = None,
                       force: bool = False) -> pd.DataFrame:
    """
    计算指定交易日的全市场因子表，缓存到 parquet。
    force=True 时强制重算。
    """
    path = _factor_cache_path(date)
    if path.exists() and not force:
        return pd.read_parquet(path)

    provider = provider or CompositeProvider()
    logger.info("构建因子表 %s ...", date)

    daily = provider.get_daily(date)
    if daily is None or daily.empty:
        raise ValueError(f"{date} 日线数据为空（收盘后约15-30分钟入库）")
    daily_basic = provider.get_daily_basic(date)
    money_flow = provider.get_money_flow(date)
    stock_basic = provider.get_stock_basic()
    try:
        comment = provider.get_stock_comment(date)
    except Exception:
        comment = None

    df = _merge_base(daily, daily_basic, money_flow, stock_basic, comment)
    df = _add_technical_factors(df, date, provider)
    df = _add_fund_persistence(df, date, provider)   # 近3日主力净流入(持续性)
    df = _add_fund_repeat_inflow(df, date, provider)  # 近10日反复净流入(天数/连续)+板块持续流入
    df = _add_fundamentals(df, provider)              # 批量财务(负债率/净利同比/ROE)·妖股排雷
    df = _add_youzi_relay(df, date, provider)         # 近20日游资接力天数(批量top_inst)
    df = _add_earnings(df, provider)                  # 业绩预告(中报+一季报·二季度催化)
    df = _add_leader_flags(df)                         # 板块龙头标记(行业内强+大+活排名)
    df = _add_sector_strength_flag(df)                 # 板块走强标记(所属行业 RPS 中位偏强)
    df = _add_accum_score(df)                          # 慢牛吸筹评分(合成上面多日因子)

    df.to_parquet(path, index=False)
    logger.info("因子表完成 %s：%d 只股票", date, len(df))
    return df


def _merge_base(daily, daily_basic, money_flow, stock_basic, comment) -> pd.DataFrame:
    """合并行情/基础/资金/名称/千股千评，得到基础因子列。"""
    uni = daily[["ts_code", "close", "pct_chg", "vol", "amount", "high", "low", "pre_close"]].copy()
    uni["pct_chg"] = pd.to_numeric(uni["pct_chg"], errors="coerce")
    uni["amount_100m"] = pd.to_numeric(uni["amount"], errors="coerce") / 100000  # 千元→亿元
    # 振幅 = (最高 - 最低) / 昨收 × 100
    pre = pd.to_numeric(uni["pre_close"], errors="coerce")
    uni["amplitude"] = (
        (pd.to_numeric(uni["high"], errors="coerce") - pd.to_numeric(uni["low"], errors="coerce"))
        / pre.replace(0, pd.NA) * 100
    )

    if daily_basic is not None and not daily_basic.empty:
        # 注意：daily_basic.volume_ratio 当日常为空，量比改由成交量矩阵自算（见 _add_technical_factors）
        cols = ["ts_code", "circ_mv", "total_mv", "turnover_rate", "pe_ttm", "pb"]
        uni = uni.merge(daily_basic[cols], on="ts_code", how="left")
        uni["circ_mv_100m"] = pd.to_numeric(uni["circ_mv"], errors="coerce") / 10000
        uni["total_mv_100m"] = pd.to_numeric(uni["total_mv"], errors="coerce") / 10000

    if money_flow is not None and not money_flow.empty:
        mf = money_flow.copy()
        mf["main_net_amount"] = (
            (mf["buy_elg_amount"] - mf["sell_elg_amount"]) +
            (mf["buy_lg_amount"] - mf["sell_lg_amount"])
        ) / 10000  # 万元→亿元
        mf["elg_net"] = (mf["buy_elg_amount"] - mf["sell_elg_amount"]) / 10000
        uni = uni.merge(mf[["ts_code", "main_net_amount", "elg_net"]], on="ts_code", how="left")

    if stock_basic is not None and not stock_basic.empty:
        uni = uni.merge(stock_basic[["ts_code", "name", "industry"]], on="ts_code", how="left")
        uni["is_st"] = uni["name"].fillna("").str.upper().str.contains("ST")   # ST/*ST 退市风险·排雷用

    if comment is not None and not comment.empty:
        cmt = comment.copy()
        # 千股千评原始列：代码/综合得分/机构参与度/目前排名 → 标准列
        rename = {"代码": "symbol", "综合得分": "comment_score",
                  "机构参与度": "institution_pct", "目前排名": "popularity_rank"}
        cmt = cmt.rename(columns={k: v for k, v in rename.items() if k in cmt.columns})
        if "symbol" in cmt.columns:
            cmt["symbol"] = cmt["symbol"].astype(str).str.zfill(6)
            uni["symbol"] = uni["ts_code"].str[:6]
            keep = [c for c in ["symbol", "comment_score", "institution_pct", "popularity_rank"] if c in cmt.columns]
            uni = uni.merge(cmt[keep], on="symbol", how="left")
            if "institution_pct" in uni.columns:  # 0-1 → 百分比
                uni["institution_pct"] = pd.to_numeric(uni["institution_pct"], errors="coerce") * 100
            uni = uni.drop(columns=["symbol"], errors="ignore")

    # 涨停标记（板块感知）
    from app.nodes.quick_report import _board_limit_pct
    name_map = dict(zip(uni["ts_code"], uni.get("name", pd.Series("", index=uni.index)).fillna("")))
    limits = uni["ts_code"].map(lambda c: _board_limit_pct(c, name_map.get(c, "")))
    uni["is_limit_up"] = uni["pct_chg"] >= (limits - 0.3)
    return uni


def _add_technical_factors(df: pd.DataFrame, date: str, provider) -> pd.DataFrame:
    """基于历史价格矩阵计算 MA站上/RPS/MACD金叉/RSI/VWAP偏离。"""
    # 取 265 个交易日：覆盖年线MA250(+斜率)。原 130 日导致 MA144/MA250 永远不足数据→恒 False
    close_m, open_m, high_m, low_m, vol_m = load_price_matrix(date, provider, n_days=265)

    # 站上各均线（向量化；要求窗口内真有 n 根有效K线，避免新股/次新用不足数据误判年线）
    last_close = close_m.iloc[-1]
    ma_latest = {}                                  # n -> (MA值Series, 有效掩码Series)
    for n in (5, 10, 20, 60, 90, 120, 144, 250):
        if len(close_m) >= n:
            w = close_m.tail(n)
            ma_n, ok_n = w.mean(), (w.count() >= n)
            ma_latest[n] = (ma_n, ok_n)
            df[f"above_ma{n}"] = df["ts_code"].map(((last_close > ma_n) & ok_n).to_dict()).fillna(False)
        else:
            df[f"above_ma{n}"] = False

    # 均线多头排列（完整）：MA5>MA10>MA20>MA60 且各线均有效——强趋势的经典确认
    if all(n in ma_latest for n in (5, 10, 20, 60)):
        (m5, o5), (m10, o10), (m20, o20), (m60, o60) = (ma_latest[5], ma_latest[10], ma_latest[20], ma_latest[60])
        bull = (m5 > m10) & (m10 > m20) & (m20 > m60) & o5 & o10 & o20 & o60
        df["ma_bull_full"] = df["ts_code"].map(bull.to_dict()).fillna(False)
    else:
        df["ma_bull_full"] = False

    # 均线拐头/趋势向上：MA 当前值 vs k 日前（首尾窗口都要够数据）；年线向上=牛熊趋势确认
    def _slope_up(n: int, k: int):
        if len(close_m) < n + k:
            return None
        w_now, w_prev = close_m.tail(n), close_m.iloc[-(n + k):-k]
        return (w_now.mean() > w_prev.mean()) & (w_now.count() >= n) & (w_prev.count() >= n)
    for col, n, k in (("ma20_up", 20, 3), ("ma250_up", 250, 5)):
        up = _slope_up(n, k)
        df[col] = df["ts_code"].map(up.to_dict()).fillna(False) if up is not None else False

    # 稳站MA20：最近 5 日每天收盘≥MA20（"稳稳在20日线上方"·比当日站上更强、更像机构持仓）
    # 破五反五已作为回测形态信号 break5_recover/break5_recover_vol 提供（自动出现在「K线形态」组），此处不重复。
    if len(close_m) >= 24:
        ma20_s = close_m.rolling(20).mean()
        stable = (close_m.iloc[-5:] >= ma20_s.iloc[-5:]).all() & ma20_s.iloc[-1].notna()
        df["stable_above_ma20"] = df["ts_code"].map(stable.to_dict()).fillna(False)
    else:
        df["stable_above_ma20"] = False

    # 关键均线 / 前高前低【数值】（供盘中"现价实时突破/跌破关键位"判定）。
    # 与上方 above_ma 同一口径：close_m/high_m/low_m = Tushare daily 不复权原始价，与全推实时价同尺度；
    # 无效（窗口不足）则 NaN，实时层只在数值有效且价格尺度对齐时使用。
    for n in (5, 10, 20, 60):
        if n in ma_latest:
            ma_n, ok_n = ma_latest[n]
            df[f"ma{n}"] = df["ts_code"].map(ma_n.where(ok_n).to_dict())
        else:
            df[f"ma{n}"] = np.nan
    for col, mat, agg, win in (("high20", high_m, "max", 20), ("low20", low_m, "min", 20),
                               ("high60", high_m, "max", 60)):
        df[col] = (df["ts_code"].map(getattr(mat.tail(win), agg)().to_dict())
                   if len(mat) >= win else np.nan)

    # RPS（向量化）：calc_rps 内部按价格算 N 日涨幅的全市场百分位
    rps50 = F.calc_rps(close_m, 50) if len(close_m) > 50 else pd.Series(dtype=float)
    rps120 = F.calc_rps(close_m, 120) if len(close_m) > 120 else pd.Series(dtype=float)
    df["rps50"] = df["ts_code"].map(rps50.to_dict()) if not rps50.empty else np.nan
    df["rps120"] = df["ts_code"].map(rps120.to_dict()) if not rps120.empty else np.nan
    df["rps_combo"] = df[["rps50", "rps120"]].mean(axis=1)

    # MACD/KDJ金叉 / RSI / VWAP / EMA多头 / 影线 / TD九转 / K线形态（逐股一次性算）
    from app.nodes.quick_report import _board_limit_pct
    name_map = dict(zip(df["ts_code"], df.get("name", pd.Series("", index=df.index)).fillna("")))
    macd_gold, rsi14, vwap_dev, vol_ratio = {}, {}, {}, {}
    kdj_gold, ema_bull, td9, long_up, long_dn = {}, {}, {}, {}, {}
    limit60, maxboard, consecnow = {}, {}, {}      # 近60日涨停天数 / 近120日最高连板 / 截至昨收当前连板
    pat_hits: dict[str, dict[str, bool]] = {}
    for ts in close_m.columns:
        s = close_m[ts].dropna()
        if len(s) < 35:
            continue
        try:
            hi, lo = high_m[ts].dropna(), low_m[ts].dropna()
            limit60[ts], maxboard[ts], consecnow[ts] = _limit_stats(s, _board_limit_pct(ts, name_map.get(ts, "")))
            macd_gold[ts] = F.macd_golden_cross(s)
            rsi14[ts] = float(F.rsi(s, 14).iloc[-1])
            kdj_gold[ts] = F.kdj_golden_cross(s, hi, lo)
            ema_bull[ts] = F.ema_bull(s)
            td9[ts] = F.td_buy_setup_count(s) >= 9
            up_r, dn_r = F.shadow_ratio(float(open_m[ts].iloc[-1]), float(hi.iloc[-1]),
                                        float(lo.iloc[-1]), float(s.iloc[-1]))
            long_up[ts], long_dn[ts] = up_r >= 0.5, dn_r >= 0.5   # 影线占全幅一半以上
            v = vol_m[ts].dropna()
            if len(v) >= 6:
                # 量比 = 今日量 / 近5日均量（自算，因 daily_basic.volume_ratio 当日常缺失）
                vol_ratio[ts] = F.volume_ratio(v, n=5)
            if len(v) >= 20:
                vwap_dev[ts] = F.vwap_position(s.tail(20), v.tail(20), 20) * 100
            pat_hits[ts] = _detect_patterns(ts, close_m, open_m, high_m, low_m, vol_m)
        except Exception:
            continue
    df["macd_gold"] = df["ts_code"].map(macd_gold).fillna(False)
    df["rsi14"] = df["ts_code"].map(rsi14)
    df["vwap_dev"] = df["ts_code"].map(vwap_dev)
    df["volume_ratio"] = df["ts_code"].map(vol_ratio)
    df["kdj_gold"] = df["ts_code"].map(kdj_gold).fillna(False)
    df["ema_bull"] = df["ts_code"].map(ema_bull).fillna(False)
    df["td_buy9"] = df["ts_code"].map(td9).fillna(False)
    df["long_upper"] = df["ts_code"].map(long_up).fillna(False)
    df["long_lower"] = df["ts_code"].map(long_dn).fillna(False)
    df["limit_ups_60d"] = df["ts_code"].map(limit60)          # 妖股：近60日涨停天数
    df["max_consec_limit"] = df["ts_code"].map(maxboard)      # 妖股：近120日最高连板
    df["consec_limit_now"] = df["ts_code"].map(consecnow)     # 截至昨收当前连板数(情绪温度计连板梯队用)
    # K线形态布尔列 pat_<key>
    for key in PATTERN_REGISTRY:
        col = f"pat_{key}"
        df[col] = df["ts_code"].map(lambda ts: pat_hits.get(ts, {}).get(key, False)).fillna(False)

    # 慢牛吸筹·多日因子（复用已加载的130日矩阵·向量化）
    for col, series in _accum_factor_columns(close_m, high_m, low_m, vol_m).items():
        df[col] = df["ts_code"].map(series.to_dict())
    return df


def _limit_stats(close: pd.Series, board_limit_pct: float) -> tuple[int, int, int]:
    """单股涨停统计（纯函数·可单测）：近60日涨停天数 + 近120日最高连板 + 当前连板。

    涨停判定：当日涨幅 ≥ 板块涨停幅 -0.3%（消化四舍五入/盘中价差）。
    Returns: (近60日涨停天数, 近120日最高连续涨停天数, 截至最后一日的当前连续涨停天数)。
    """
    ret = close.pct_change() * 100
    thr = board_limit_pct - 0.3
    ups60 = int((ret.tail(60) >= thr).sum())
    seq = (ret.tail(120) >= thr).astype(int).tolist()
    mx = run = 0
    for b in seq:
        run = run + 1 if b else 0
        mx = max(mx, run)
    cur = 0                                          # 当前连板=从最后一日往前数连续涨停
    for b in reversed(seq):
        if b:
            cur += 1
        else:
            break
    return ups60, mx, cur


def _accum_factor_columns(close_m, high_m, low_m, vol_m) -> dict[str, pd.Series]:
    """从130日价量矩阵算"悄悄放量·缓慢走高"多日因子（每列为 {ts_code: 值} 的 Series）。

    全部向量化（按矩阵列=个股一次算完）：温和放量比 / MA20斜率 / 近20日涨幅 /
    近20日均振幅 / 近20日大涨天数（隐蔽性）。历史不足的窗口自动跳过该列。
    """
    out: dict[str, pd.Series] = {}
    n = len(close_m)
    if n >= 20 and vol_m is not None:                         # 温和放量：5日均量 / 20日均量
        v20 = vol_m.tail(20).mean()
        out["vol5_vol20"] = vol_m.tail(5).mean() / v20.replace(0, np.nan)
    if n >= 25:                                               # MA20斜率%：今日MA20 vs 5日前MA20
        ma20_now, ma20_prev = close_m.tail(20).mean(), close_m.iloc[-25:-5].mean()
        out["ma20_slope"] = (ma20_now / ma20_prev.replace(0, np.nan) - 1) * 100
    if n >= 6:                                                # 近5日涨幅%(超跌低吸判定)
        out["ret5"] = (close_m.iloc[-1] / close_m.iloc[-6].replace(0, np.nan) - 1) * 100
    if n >= 21:                                               # 近20日涨幅%
        ret = close_m.pct_change()
        out["ret20"] = (close_m.iloc[-1] / close_m.iloc[-21].replace(0, np.nan) - 1) * 100
        out["big_up_days_20"] = (ret.tail(20) >= 0.095).sum()                   # 近20日大涨(≥9.5%)天数
        if vol_m is not None:                                 # 量价配合：上涨日均量 / 下跌日均量
            r20, v20d = ret.tail(20), vol_m.tail(20)
            out["up_down_vol"] = v20d.where(r20 > 0).mean() / v20d.where(r20 < 0).mean().replace(0, np.nan)
    if n >= 20 and high_m is not None and low_m is not None:  # 振幅：均值 + 收敛比(近10日/前10日)
        amp = (high_m - low_m) / close_m.shift(1).replace(0, np.nan) * 100
        out["amp20"] = amp.tail(20).mean()
        out["amp_contract"] = amp.tail(10).mean() / amp.iloc[-20:-10].mean().replace(0, np.nan)
    return out


def _add_fund_persistence(df: pd.DataFrame, date: str, provider) -> pd.DataFrame:
    """近3日主力净流入(亿)·复用 signals._main_flow_3d（与资金三角同口径·已缓存）。"""
    try:
        from app.strategy.signals import _main_flow_3d
        df["main_net_3d"] = df["ts_code"].map(_main_flow_3d(provider, date))
    except Exception as e:
        logger.debug("3日主力净流入计算失败: %s", e)
    return df


def _inflow_counts(nets: list) -> tuple[int, int]:
    """逐日净流入列表(升序·可含 None=缺数据) → (净流入天数, 从最新往回的连续净流入天数)。

    连续从末尾(最新)往回数，遇净流出(≤0)或缺数据(None)即断。纯函数·便于单测。
    """
    days = sum(1 for x in nets if x is not None and x > 0)
    consec = 0
    for x in reversed(nets):
        if x is None or x <= 0:
            break
        consec += 1
    return days, consec


def _flow_quality(nets: list, inflow_days: int) -> tuple:
    """资金脉冲质量（借鉴"持续流入 vs 脉冲后退潮"）：(脉冲集中度, 近2日净亿, 真持续, 脉冲退潮假流入)。

    - 脉冲集中度 = 最大单日流入 / 累计流入（越高=越靠一两天脉冲·越假）；
    - 近2日净 < 0 = 近期在退潮；
    - **真持续** = 流入天数≥6 且 非脉冲主导(集中度<0.5) 且 近期未退潮（近2日≥0）；
    - **脉冲退潮(假流入)** = 有过流入但一两天主导(集中度≥0.6) 且 近期已转流出（近2日<0）。
    """
    positives = [x for x in nets if x is not None and x > 0]
    total_pos = sum(positives)
    pulse = round(max(positives) / total_pos, 2) if total_pos > 0 else 0.0
    valid = [x for x in nets if x is not None]
    recent2 = round(sum(valid[-2:]), 2) if valid else 0.0
    sustained = inflow_days >= 6 and pulse < 0.5 and recent2 >= 0
    fade = inflow_days >= 2 and pulse >= 0.6 and recent2 < -0.01
    return pulse, recent2, sustained, fade


def _add_fund_repeat_inflow(df: pd.DataFrame, date: str, provider, window: int = 10) -> pd.DataFrame:
    """主力(估算)资金「反复净流入」：近 window 日 净流入天数 + 连续净流入天数 + 板块持续流入天数。

    对应"反复小幅净流入悄悄吸筹"这套打法——比"近3日累计"更能抓持续性（一根大单撑不出天数）。
    ⚠️ 口径=超大单+大单的**代理估算**（与资金三角一致），**非真机构钱**（真机构看龙虎榜）。
    """
    df["inflow_days_10"] = 0
    df["consec_inflow"] = 0
    df["sector_inflow_days"] = 0
    df["fund_pulse_ratio"] = np.nan
    df["fund_recent2d"] = np.nan
    df["fund_sustained"] = False
    df["fund_pulse_fade"] = False
    try:
        from app.factors.breadth_qfq import _recent_trade_dates
        dates = _recent_trade_dates(provider, date, window)        # 升序·末=最新
    except Exception as e:
        logger.debug("反复净流入: 取交易日失败 %s", e)
        return df
    if not dates:
        return df

    day_nets: list[dict] = []                                      # 逐日 {ts_code: 净流入(亿)}
    for d in dates:
        m: dict = {}
        try:
            mf = provider.get_money_flow(d)
        except Exception:
            mf = None
        if mf is not None and not mf.empty and "net_mf_amount" in mf.columns:
            net = pd.to_numeric(mf["net_mf_amount"], errors="coerce")
            for ts, v in zip(mf["ts_code"], net):
                if pd.notna(v):
                    m[ts] = float(v) / 1e4
        day_nets.append(m)

    # 个股：净流入天数/连续 + 脉冲质量(真持续 vs 脉冲后退潮假流入)
    inflow_days, consec, pulse_r, recent2, sustained, fade = {}, {}, {}, {}, {}, {}
    for ts in df["ts_code"]:
        nets = [m.get(ts) for m in day_nets]
        if not any(x is not None for x in nets):
            continue
        d, c = _inflow_counts(nets)
        inflow_days[ts], consec[ts] = d, c
        pr, r2, sus, fd = _flow_quality(nets, d)
        pulse_r[ts], recent2[ts], sustained[ts], fade[ts] = pr, r2, sus, fd
    df["inflow_days_10"] = df["ts_code"].map(inflow_days).fillna(0).astype(int)
    df["consec_inflow"] = df["ts_code"].map(consec).fillna(0).astype(int)
    df["fund_pulse_ratio"] = df["ts_code"].map(pulse_r)
    df["fund_recent2d"] = df["ts_code"].map(recent2)
    df["fund_sustained"] = df["ts_code"].map(sustained).fillna(False).astype(bool)
    df["fund_pulse_fade"] = df["ts_code"].map(fade).fillna(False).astype(bool)

    # 板块：逐日按行业汇总净流入 → 行业净流入天数（先选对持续流入的板块）
    ind_map = dict(zip(df["ts_code"], df.get("industry", pd.Series("", index=df.index)).fillna("")))
    sec_days: dict[str, int] = {}
    for m in day_nets:
        agg: dict[str, float] = {}
        for ts, v in m.items():
            ind = ind_map.get(ts)
            if ind:
                agg[ind] = agg.get(ind, 0.0) + v
        for ind, tot in agg.items():
            if tot > 0:
                sec_days[ind] = sec_days.get(ind, 0) + 1
    df["sector_inflow_days"] = df["industry"].map(sec_days).fillna(0).astype(int)
    return df


def _latest_fina_period(now=None) -> str:
    """最近一个『已披露』的报告期 YYYYMMDD（季报约披露后45-60天可得，留50天余量）。"""
    import datetime
    now = now or datetime.date.today()
    cutoff = (now - datetime.timedelta(days=50)).strftime("%Y%m%d")
    ends = sorted((f"{y}{md}" for y in (now.year, now.year - 1)
                   for md in ("1231", "0930", "0630", "0331")), reverse=True)
    for e in ends:
        if e <= cutoff:
            return e
    return ends[-1]


def _youzi_relay_map(provider, date: str, lookback: int = 20) -> dict[str, tuple[int, float]]:
    """近 N 日全市场游资接力：{ts_code: (有游资净买的天数, 游资净买合计亿)}。

    批量按日拉 top_inst（全市场·1次/日·缓存），按席位名识别游资(type=hot)并只取净买>0，
    比逐股复盘省百倍取数。供选股结果一眼看"哪些连板票背后有游资在接力"。
    """
    from app.factors.breadth_qfq import _recent_trade_dates
    from app.strategy.lhb_seats import classify_seat
    try:
        dates = _recent_trade_dates(provider, date, lookback)
    except Exception:
        dates = [date]
    days_cnt: dict[str, int] = {}
    net_sum: dict[str, float] = {}
    seat_cls: dict[str, str] = {}                          # 席位分类缓存(同名席位复用)
    for d in dates:
        try:
            df = provider.get_lhb_inst(d)
        except Exception:
            continue
        if df is None or df.empty or "exalter" not in df.columns or "net_buy" not in df.columns:
            continue
        nb = pd.to_numeric(df["net_buy"], errors="coerce")
        today_ts: set[str] = set()
        for ts, ex, v in zip(df["ts_code"], df["exalter"], nb):
            if pd.isna(v) or v <= 0:
                continue
            ex = str(ex)
            typ = seat_cls.get(ex) or seat_cls.setdefault(ex, classify_seat(ex).get("type", ""))
            if typ == "hot":
                net_sum[ts] = net_sum.get(ts, 0.0) + float(v) / 1e8
                today_ts.add(ts)
        for ts in today_ts:
            days_cnt[ts] = days_cnt.get(ts, 0) + 1
    return {ts: (days_cnt[ts], round(net_sum.get(ts, 0.0), 2)) for ts in days_cnt}


def _add_youzi_relay(df: pd.DataFrame, date: str, provider) -> pd.DataFrame:
    """加"近20日游资接力天数 youzi_relay_days"列（无龙虎榜的票为0）。失败优雅跳过。"""
    try:
        m = _youzi_relay_map(provider, date)
        df["youzi_relay_days"] = df["ts_code"].map({t: v[0] for t, v in m.items()}).fillna(0).astype(int)
        df["youzi_net_yi"] = df["ts_code"].map({t: v[1] for t, v in m.items()})
    except Exception as e:
        logger.debug("游资接力计算失败: %s", e)
    return df


def _add_sector_strength_flag(df: pd.DataFrame, min_n: int = 3, rps_thresh: float = 55.0) -> pd.DataFrame:
    """标记「板块走强」：所属行业 RPS120 中位数≥阈值 且 行业内有效股票数≥min_n。

    用作"趋势没破·身处强势板块"的过滤层（如配合破五反五·放量，避开弱板块假反抽）。
    """
    if "industry" not in df or "rps120" not in df:
        df["sector_strong"] = False
        return df
    rps = pd.to_numeric(df["rps120"], errors="coerce")
    grp = rps.groupby(df["industry"])
    med, cnt = grp.transform("median"), grp.transform("count")
    df["sector_strong"] = ((med >= rps_thresh) & (cnt >= min_n)).fillna(False)
    return df


def _leader_score_series(df: pd.DataFrame) -> pd.Series:
    """龙头分（向量化）：相对强度(RPS) + 规模(市值百分位) + 流动性(成交百分位）。
    龙头=板块里"既强、又大、又活"的领头羊，区别于沉睡大白马或小弱票。"""
    rps = pd.to_numeric(df.get("rps120"), errors="coerce").fillna(0.0)
    circ_pct = pd.to_numeric(df.get("circ_mv_100m"), errors="coerce").rank(pct=True).fillna(0.0)
    amt_pct = pd.to_numeric(df.get("amount_100m"), errors="coerce").rank(pct=True).fillna(0.0)
    return (0.45 * rps + 35 * circ_pct + 20 * amt_pct).round(1)


def _add_leader_flags(df: pd.DataFrame, top_n: int = 2) -> pd.DataFrame:
    """标记板块龙头：行业内按龙头分排名，前 top_n 名为龙头(is_leader)。"""
    df["leader_score"] = _leader_score_series(df)
    df["leader_rank"] = df.groupby("industry")["leader_score"].rank(ascending=False, method="first")
    df["is_leader"] = (df["leader_rank"] <= top_n).fillna(False)
    return df


def _add_fundamentals(df: pd.DataFrame, provider) -> pd.DataFrame:
    """全市场批量财务(1次 vip 调用)：资产负债率/净利同比/ROE，供妖股排雷。失败优雅跳过。"""
    try:
        fin = provider.get_fina_indicator_by_period(_latest_fina_period())
        if fin is not None and not fin.empty:
            fin = fin.drop_duplicates("ts_code", keep="first")
            for col in ("debt_to_assets", "netprofit_yoy", "roe"):
                if col in fin.columns:
                    m = dict(zip(fin["ts_code"], pd.to_numeric(fin[col], errors="coerce")))
                    df[col] = df["ts_code"].map(m)
    except Exception as e:
        logger.debug("批量财务获取失败: %s", e)
    return df


# 业绩预告"预喜"类型（正面催化）
_EARN_GOOD = {"预增", "略增", "扭亏", "续盈", "预盈", "减亏", "扭亏为盈"}


def _forecast_periods(now=None) -> list[str]:
    """当前正密集预告的报告期 + 上一期（YYYYMMDD·新→旧）。

    季度末会提前预告（如6月底已出中报预告），故允许未来15天内的季度末。
    """
    import datetime
    now = now or datetime.date.today()
    cutoff = (now + datetime.timedelta(days=15)).strftime("%Y%m%d")
    ends = sorted((f"{y}{md}" for y in (now.year, now.year - 1)
                   for md in ("0331", "0630", "0930", "1231")), reverse=True)
    recent = [e for e in ends if e <= cutoff][:2]
    return recent or [ends[0]]


def _add_earnings(df: pd.DataFrame, provider) -> pd.DataFrame:
    """业绩预告催化：取最近两期预告(中报优先)，每股留最新一条 → 预告类型/净利变动%/是否预喜/有中报预告。"""
    try:
        periods = _forecast_periods()
        frames = []
        for pri, p in enumerate(periods):          # pri=0 为最新期(中报)
            fc = provider.get_forecast_by_period(p)
            if fc is not None and not fc.empty:
                fc = fc.copy(); fc["_period"] = p; fc["_pri"] = pri
                frames.append(fc)
        if not frames:
            return df
        allfc = pd.concat(frames, ignore_index=True)
        allfc["ann_date"] = allfc.get("ann_date", "").astype(str)
        allfc = (allfc.sort_values(["_pri", "ann_date"], ascending=[True, False])
                      .drop_duplicates("ts_code", keep="first"))   # 每股留最新期最新公告
        chg = (pd.to_numeric(allfc["p_change_min"], errors="coerce")
               + pd.to_numeric(allfc["p_change_max"], errors="coerce")) / 2
        allfc = allfc.assign(forecast_chg=chg.round(1))
        h1 = periods[0]                                            # 最新期=中报视为二季度催化
        m_type = dict(zip(allfc["ts_code"], allfc["type"].astype(str)))
        df["forecast_type"] = df["ts_code"].map(m_type)
        df["forecast_chg"] = df["ts_code"].map(dict(zip(allfc["ts_code"], allfc["forecast_chg"])))
        df["earn_good"] = df["forecast_type"].isin(_EARN_GOOD).fillna(False)
        h1_codes = set(allfc.loc[allfc["_period"] == h1, "ts_code"])
        df["is_h1_forecast"] = df["ts_code"].isin(h1_codes)
    except Exception as e:
        logger.debug("业绩预告获取失败: %s", e)
    return df


def _add_accum_score(df: pd.DataFrame) -> pd.DataFrame:
    """逐股算慢牛吸筹评分 accum_score（缺列以 NaN 占位·评分函数自身做 NaN 兜底）。"""
    for c in ("vol5_vol20", "ma20_slope", "ret20", "amp20", "amp_contract",
              "up_down_vol", "big_up_days_20", "main_net_3d"):
        if c not in df.columns:
            df[c] = np.nan
    df["accum_score"] = df.apply(
        lambda r: _accumulation_score(
            r["vol5_vol20"], r["ma20_slope"], r["ret20"], r["amp20"],
            r["big_up_days_20"], r["main_net_3d"],
            up_down_vol=r["up_down_vol"], amp_contract=r["amp_contract"]),
        axis=1,
    )
    return df


# 吸筹评分权重（满分100·配置化·可后续回测校准）。
# 设计取向：把更可靠的"量价行为"(温和放量18+量价配合18)权重做高，把易失真的
# "大单资金估算"压到弱信号档(8)。量价配合/振幅收敛是 2026-06 据策略复盘新增的更硬信号。
_ACC_W = {
    "vol": 18,        # 温和放量(5/20日量比甜区)
    "volprice": 18,   # 量价配合(上涨放量·回调缩量)——比大单口径更可靠
    "slope": 14,      # MA20斜率向上
    "rise": 14,       # 缓慢走高(近20日涨幅甜区)
    "contract": 10,   # 振幅收敛(近10日振幅 < 前10日·锁筹痕迹)
    "amp": 6,         # 低波动(振幅绝对水平低)
    "hidden": 12,     # 隐蔽(近20日无大涨/涨停)
    "fund": 8,        # 主力近3日净流入(大单估算·弱信号·仅作锦上添花)
}


def _num(x, default=None):
    """安全转 float，NaN/非数返回 default（纯工具）。"""
    import math
    try:
        f = float(x)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _accumulation_score(vol_ratio, ma20_slope, ret20, amp20, big_up_days, main_net_3d,
                        up_down_vol=None, amp_contract=None) -> float:
    """慢牛吸筹评分 0~100（纯函数·零依赖·可单测）。

    刻画"主力悄悄吸筹、缓慢走高"：温和放量(非爆量) + 量价配合(涨放量/跌缩量) +
    MA20向上 + 小步上涨 + 振幅收敛 + 低波动 + 近期没大涨涨停(隐蔽) +
    主力近3日净流入(弱信号)。各项甜区给分，越偏离越低，缺数据则该项不计分。
    **评分只刻画吸筹形态强弱，不预测涨跌；真钱确认请到个股360看龙虎榜机构席位。**
    """
    s = 0.0
    v = _num(vol_ratio)                                  # 温和放量：甜区 1.2~2.5
    if v is not None:
        if 1.2 <= v <= 2.5:
            s += _ACC_W["vol"]
        elif 1.0 <= v < 1.2 or 2.5 < v <= 3.2:
            s += _ACC_W["vol"] * 0.5
    udv = _num(up_down_vol)                              # 量价配合：上涨日均量 > 下跌日均量
    if udv is not None:
        if udv >= 1.3:
            s += _ACC_W["volprice"]
        elif udv >= 1.05:
            s += _ACC_W["volprice"] * 0.6
        elif udv >= 0.9:
            s += _ACC_W["volprice"] * 0.25
    sl = _num(ma20_slope)                                # MA20向上：缓升最佳，过陡略降
    if sl is not None:
        if 0 < sl <= 12:
            s += _ACC_W["slope"]
        elif sl > 12:
            s += _ACC_W["slope"] * 0.5
    r = _num(ret20)                                      # 缓慢走高：甜区 3~25%
    if r is not None:
        if 3 <= r <= 25:
            s += _ACC_W["rise"]
        elif 0 <= r < 3:
            s += _ACC_W["rise"] * 0.4
        elif 25 < r <= 35:
            s += _ACC_W["rise"] * 0.3
    ac = _num(amp_contract)                              # 振幅收敛：近10日 / 前10日 < 1
    if ac is not None:
        if ac <= 0.85:
            s += _ACC_W["contract"]
        elif ac <= 1.0:
            s += _ACC_W["contract"] * 0.5
    a = _num(amp20)                                      # 低波动：振幅绝对水平越低越好
    if a is not None:
        if a <= 3.5:
            s += _ACC_W["amp"]
        elif a <= 5.5:
            s += _ACC_W["amp"] * 0.6
        elif a <= 7.5:
            s += _ACC_W["amp"] * 0.27
    bd = _num(big_up_days)                               # 隐蔽：近20日无大涨/涨停
    if bd is not None:
        s += max(0.0, _ACC_W["hidden"] - bd * 4)
    if (_num(main_net_3d, 0) or 0) > 0:                  # 主力近3日净流入(弱信号)
        s += _ACC_W["fund"]
    return round(min(100.0, s), 1)


def _detect_patterns(ts, close_m, open_m, high_m, low_m, vol_m) -> dict[str, bool]:
    """构建单股 OHLCV 并跑全部已注册形态（不复权，与其余技术因子口径一致）。"""
    ohlcv = pd.DataFrame({
        "open": open_m.get(ts), "high": high_m.get(ts), "low": low_m.get(ts),
        "close": close_m.get(ts), "vol": vol_m.get(ts),
    }).dropna()
    if ohlcv.empty:
        return {}
    return detect_all(ohlcv)


# ──────────────────────────────────────────────
# 筛选
# ──────────────────────────────────────────────

_FACTOR_INDEX = {f["key"]: f for g in FACTOR_GROUPS for f in g["factors"]}


def _apply_condition(df: pd.DataFrame, f: dict) -> pd.Series:
    """单个因子 → 布尔掩码。"""
    col, op = f["col"], f["op"]
    if col not in df.columns:
        return pd.Series(True, index=df.index)
    s = df[col]
    if op == "true":
        return s.fillna(False).astype(bool)
    if op == "false":
        return ~s.fillna(False).astype(bool)
    if op == "ge":
        return s >= f["val"]
    if op == "gt":
        return s > f["val"]
    if op == "le":
        return s <= f["val"]
    if op == "lt":
        return s < f["val"]
    if op == "between":
        lo, hi = f["val"]
        return (s >= lo) & (s <= hi)
    if op == "eq":
        return s == f["val"]
    return pd.Series(True, index=df.index)


def _apply_customs(df: pd.DataFrame, customs: list[dict] | None) -> pd.Series:
    """应用自定义任意字段条件 [{col, op, val}]（白名单字段+操作符，防注入）。"""
    mask = pd.Series(True, index=df.index)
    for c in customs or []:
        col, op = c.get("col"), c.get("op")
        if col not in _CUSTOM_COLS or op not in _CUSTOM_OPS:
            continue
        try:
            val = float(c.get("val"))
        except (TypeError, ValueError):
            continue
        if col in df.columns:
            mask &= _apply_condition(df, {"col": col, "op": op, "val": val}).fillna(False)
    return mask


def screen(date: str, selected_keys: list[str],
           custom: dict | None = None, customs: list[dict] | None = None,
           sort_by: str = "rps120", limit: int = 100,
           provider: CompositeProvider | None = None) -> dict:
    """
    按选中因子筛选。
    custom:  {"n": 7, "op": "le", "val": 7}  近N日累计涨幅自定义
    customs: [{"col","op","val"}, ...]        任意字段自定义条件（白名单）
    返回 {"ok", "count", "columns", "rows"}。
    """
    df = build_factor_table(date, provider)
    mask = pd.Series(True, index=df.index)

    for key in selected_keys:
        f = _FACTOR_INDEX.get(key)
        if f:
            mask &= _apply_condition(df, f).fillna(False)

    # 自定义任意字段条件
    mask &= _apply_customs(df, customs)

    # 自定义：近N日累计涨跌幅
    if custom and custom.get("n"):
        ndf = _add_ndays_return(df, date, int(custom["n"]), provider or CompositeProvider())
        col = f"ret_{custom['n']}d"
        if col in ndf.columns:
            df = ndf
            cf = {"col": col, "op": custom.get("op", "le"), "val": float(custom.get("val", 0))}
            mask &= _apply_condition(df, cf).fillna(False)

    result = df[mask].copy()
    if sort_by in result.columns:
        result = result.sort_values(sort_by, ascending=False, na_position="last")
    result = result.head(limit)

    cols = [(c, lbl) for c, lbl in DISPLAY_COLS if c in result.columns]
    rows = []
    for _, r in result.iterrows():
        row = {}
        for c, _lbl in cols:
            v = r[c]
            if isinstance(v, (int, float, np.floating)) and pd.notna(v):
                row[c] = round(float(v), 2)
            elif isinstance(v, (bool, np.bool_)):
                row[c] = bool(v)
            else:
                row[c] = "" if pd.isna(v) else str(v)
        rows.append(row)

    return {
        "ok": True,
        "count": int(mask.sum()),
        "shown": len(rows),
        "columns": [{"key": c, "label": lbl} for c, lbl in cols],
        "rows": rows,
    }


def _add_ndays_return(df: pd.DataFrame, date: str, n: int, provider) -> pd.DataFrame:
    """补充近N日累计涨跌幅列 ret_{n}d。"""
    try:
        close_m, *_ = load_price_matrix(date, provider, n_days=n + 5)
        if len(close_m) > n:
            ret = (close_m.iloc[-1] / close_m.iloc[-(n + 1)] - 1) * 100
            df = df.copy()
            df[f"ret_{n}d"] = df["ts_code"].map(ret.to_dict())
    except Exception as e:
        logger.debug("近N日涨幅计算失败: %s", e)
    return df
