"""
板块热度评分模块（Phase 2 纯量化，无LLM）。

热度公式（吴川体系反向还原）：
    heat = 5日资金净流入(40%) + 涨停结构强度(30%) + 20日广度(20%) + 新闻权威性(10%，Phase3)

新增指标（对标吴川LLM分析页）：
    pop_concentration      人气集中度 = 板块前3股成交额 / 板块总成交额
    heat_score_delta_3d    3日热度变化量（需缓存历史热度分）
    nextday_risk_penalty   次日风险惩罚分（退潮+拥挤+资金外流综合）
    decision / decision_score  分层决策 avoid/watch/buy + 0-100评分

阶段判断：
    升温(new)：5日资金 > 3日资金（加速）+ 广度扩张 + 连板抬升
    趋势(trend)：资金稳定 + MA20广度>30% + 最高连板>4
    退潮(decay)：5日资金净流出 + 连板缩减 + 广度下滑
    中性(neutral)：不满足以上条件
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import get_settings
from app.data.provider import DataProvider
from app.state import SectorStat

logger = logging.getLogger(__name__)

_LIMIT_UP_PCT = 9.5
_SECTOR_STATS_CACHE_DIR = "sector_stats"   # 相对于 data_cache/


# ──────────────────────────────────────────────
# 公共入口
# ──────────────────────────────────────────────

def calc_sector_stats(
    trade_date: str,
    provider: DataProvider,
    close_m: pd.DataFrame,
) -> list[SectorStat]:
    """
    计算各行业板块热度评分、人气集中度、次日风险、分层决策。

    Args:
        trade_date: 交易日 YYYYMMDD
        provider:   数据接口（CompositeProvider）
        close_m:    历史收盘价矩阵（index=日期str, columns=ts_code），至少含25日

    Returns:
        SectorStat 列表，按决策评分降序排列
    """
    industry_map = _load_industry_map(provider)
    if industry_map.empty:
        logger.warning("[板块分析] 无法获取行业映射，跳过")
        return []

    prior_dates = _get_prior_dates_from_matrix(close_m, trade_date, n=5)
    if len(prior_dates) < 3:
        logger.warning("[板块分析] 历史数据不足3日，跳过")
        return []

    # ---- 1. 核心量化指标 ----
    flow_by_industry = _calc_fund_flow_by_industry(prior_dates, provider, industry_map)
    limit_up_by_industry = _calc_limit_up_by_industry(trade_date, provider, industry_map)
    consec_by_industry = _calc_consecutive_high(prior_dates, provider, industry_map)
    breadth_by_industry = _calc_ma20_breadth(close_m, trade_date, industry_map)
    pop_by_industry = _calc_pop_concentration(trade_date, provider, industry_map)
    stock_count = industry_map["industry"].value_counts().to_dict()

    # ---- 2. 汇总行 ----
    industries = industry_map["industry"].dropna().unique()
    rows = []
    for ind in industries:
        flow = flow_by_industry.get(ind, {"flow_5d": 0.0, "flow_3d": 0.0})
        rows.append({
            "industry": ind,
            "stock_count": stock_count.get(ind, 0),
            "flow_5d": flow["flow_5d"],
            "flow_3d": flow["flow_3d"],
            "pct_above_ma20": breadth_by_industry.get(ind, 0.0),
            "limit_up_count": limit_up_by_industry.get(ind, 0),
            "consecutive_limit_high": consec_by_industry.get(ind, 0),
            "pop_concentration": pop_by_industry.get(ind, 0.0),
        })

    if not rows:
        return []

    df = pd.DataFrame(rows)
    df = _score_and_classify(df)

    # ---- 3. 加载3日前历史热度（用于 delta 计算）----
    prior_heat = _load_prior_heat_scores(trade_date)

    # ---- 4. 构建结果对象 ----
    result: list[SectorStat] = []
    for _, row in df.iterrows():
        ind = row["industry"]
        heat = round(row["heat_score"], 1)
        delta = round(heat - prior_heat.get(ind, heat), 1)  # 无历史则delta=0
        nextday_risk = _calc_nextday_risk(row, delta)
        decision, decision_score = _make_decision(heat, delta, nextday_risk, row["phase"])

        result.append(SectorStat(
            industry=ind,
            stock_count=int(row["stock_count"]),
            flow_5d_100m=round(row["flow_5d"] / 1e8, 2),
            flow_3d_100m=round(row["flow_3d"] / 1e8, 2),
            pct_above_ma20=round(row["pct_above_ma20"], 4),
            limit_up_count=int(row["limit_up_count"]),
            consecutive_limit_high=int(row["consecutive_limit_high"]),
            heat_score=heat,
            heat_score_delta_3d=delta,
            pop_concentration=round(row["pop_concentration"], 3),
            nextday_risk_penalty=round(nextday_risk, 1),
            decision=decision,
            decision_score=decision_score,
            phase=row["phase"],
            signal=row["signal"],
            trend_score=round(float(row.get("trend_score", 0.0)), 1),
        ))

    # ---- 5. 缓存今日热度分（供后续 delta 计算使用）----
    _save_heat_scores(trade_date, {r.industry: r.heat_score for r in result})

    result.sort(key=lambda x: x.decision_score, reverse=True)
    return result


# ──────────────────────────────────────────────
# 核心量化指标计算
# ──────────────────────────────────────────────

def _load_industry_map(provider: DataProvider) -> pd.DataFrame:
    try:
        basic = provider.get_stock_basic()
        if basic is None or basic.empty:
            return pd.DataFrame()
        return basic[["ts_code", "industry"]].dropna(subset=["industry"])
    except Exception as e:
        logger.error("加载行业映射失败: %s", e)
        return pd.DataFrame()


def _get_prior_dates_from_matrix(
    close_m: pd.DataFrame, trade_date: str, n: int
) -> list[str]:
    all_dates = sorted(d for d in close_m.index if d <= trade_date)
    return all_dates[-n:]


def _calc_fund_flow_by_industry(
    dates: list[str],
    provider: DataProvider,
    industry_map: pd.DataFrame,
) -> dict[str, dict[str, float]]:
    """按行业聚合个股资金净流入，返回各行业 5日/3日 资金净流入（元）。"""
    frames = []
    for d in dates:
        try:
            mf = provider.get_money_flow(d)
            if mf is None or mf.empty:
                continue
            mf["_date"] = d
            frames.append(mf[["ts_code", "net_mf_amount", "_date"]])
        except Exception as e:
            logger.debug("获取 %s 资金流失败: %s", d, e)

    if not frames:
        return {}

    all_flow = pd.concat(frames, ignore_index=True)
    all_flow = all_flow.merge(industry_map, on="ts_code", how="left").dropna(subset=["industry"])
    # net_mf_amount 单位万元 → 元
    all_flow["net_yuan"] = all_flow["net_mf_amount"] * 1e4

    dates_sorted = sorted(dates)
    result: dict[str, dict[str, float]] = {}
    for ind, grp in all_flow.groupby("industry"):
        flow_5d = grp["net_yuan"].sum()
        last_3 = dates_sorted[-3:]
        flow_3d = grp[grp["_date"].isin(last_3)]["net_yuan"].sum()
        result[str(ind)] = {"flow_5d": float(flow_5d), "flow_3d": float(flow_3d)}
    return result


def _calc_limit_up_by_industry(
    trade_date: str,
    provider: DataProvider,
    industry_map: pd.DataFrame,
) -> dict[str, int]:
    try:
        daily = provider.get_daily(trade_date)
        if daily is None or daily.empty:
            return {}
        limit_up = daily[daily["pct_chg"] >= _LIMIT_UP_PCT][["ts_code"]]
        merged = limit_up.merge(industry_map, on="ts_code", how="left").dropna(subset=["industry"])
        return merged.groupby("industry").size().to_dict()
    except Exception as e:
        logger.warning("计算行业涨停数失败: %s", e)
        return {}


def _calc_consecutive_high(
    dates: list[str],
    provider: DataProvider,
    industry_map: pd.DataFrame,
) -> dict[str, int]:
    """计算各行业内最高连板高度（连续N天涨幅≥9.5%的最大N值）。"""
    try:
        frames = []
        for d in dates:
            df = provider.get_daily(d)
            if df is None or df.empty:
                continue
            df["_date"] = d
            frames.append(df[["ts_code", "pct_chg", "_date"]])

        if not frames:
            return {}

        all_daily = pd.concat(frames, ignore_index=True)
        all_daily = all_daily.merge(industry_map, on="ts_code", how="left")
        dates_sorted = sorted(dates)

        consec: dict[str, int] = {}
        for ts_code, grp in all_daily.groupby("ts_code"):
            pct_by_date = grp.set_index("_date")["pct_chg"].reindex(dates_sorted).fillna(0)
            count = 0
            for d in reversed(dates_sorted):
                if pct_by_date.get(d, 0) >= _LIMIT_UP_PCT:
                    count += 1
                else:
                    break
            consec[str(ts_code)] = count

        consec_s = pd.Series(consec).rename("consec").reset_index()
        consec_s.columns = ["ts_code", "consec"]
        merged = consec_s.merge(industry_map, on="ts_code", how="left").dropna(subset=["industry"])
        return merged.groupby("industry")["consec"].max().to_dict()
    except Exception as e:
        logger.warning("计算连板高度失败: %s", e)
        return {}


def _calc_ma20_breadth(
    close_m: pd.DataFrame,
    trade_date: str,
    industry_map: pd.DataFrame,
) -> dict[str, float]:
    """计算各行业内站上MA20的股票比例（广度）。"""
    try:
        available = sorted(d for d in close_m.index if d <= trade_date)
        if len(available) < 21:
            return {}
        recent = close_m.loc[available[-21:]]
        ma20 = recent.iloc[:-1].mean()
        last_close = recent.iloc[-1]
        above_ma20 = (last_close > ma20).reset_index()
        above_ma20.columns = ["ts_code", "above"]
        merged = above_ma20.merge(industry_map, on="ts_code", how="left").dropna(subset=["industry"])
        return {
            str(ind): float(grp["above"].mean())
            for ind, grp in merged.groupby("industry")
        }
    except Exception as e:
        logger.warning("计算MA20广度失败: %s", e)
        return {}


def _calc_pop_concentration(
    trade_date: str,
    provider: DataProvider,
    industry_map: pd.DataFrame,
) -> dict[str, float]:
    """
    人气集中度 = 板块前3只股票成交额 / 板块总成交额。
    值越高说明板块热度集中在少数龙头，散户跟风风险越大。
    daily.amount 单位：千元。
    """
    try:
        daily = provider.get_daily(trade_date)
        if daily is None or daily.empty:
            return {}

        merged = daily[["ts_code", "amount"]].merge(industry_map, on="ts_code", how="left")
        merged = merged.dropna(subset=["industry"])
        merged["amount"] = merged["amount"].fillna(0)

        result: dict[str, float] = {}
        for ind, grp in merged.groupby("industry"):
            total = grp["amount"].sum()
            if total <= 0:
                result[str(ind)] = 0.0
                continue
            top3 = grp.nlargest(3, "amount")["amount"].sum()
            result[str(ind)] = round(float(top3 / total), 4)
        return result
    except Exception as e:
        logger.warning("计算人气集中度失败: %s", e)
        return {}


# ──────────────────────────────────────────────
# 热度评分与阶段分类
# ──────────────────────────────────────────────

def _score_and_classify(df: pd.DataFrame) -> pd.DataFrame:
    """对各行业打热度分（0~100）并分类阶段。"""
    df["flow_score"] = _percentile_rank(df["flow_5d"]) * 100

    df["limitup_raw"] = df["limit_up_count"] * (1 + df["consecutive_limit_high"] * 0.5)
    df["limitup_score"] = _percentile_rank(df["limitup_raw"]) * 100

    df["breadth_score"] = df["pct_above_ma20"] * 100

    # 新闻权威性（Phase 3 留空，中性占位）
    df["news_score"] = 50.0

    df["heat_score"] = (
        df["flow_score"] * 0.40
        + df["limitup_score"] * 0.30
        + df["breadth_score"] * 0.20
        + df["news_score"] * 0.10
    )

    df["phase"] = df.apply(_classify_phase, axis=1)
    df["signal"] = df.apply(_build_signal, axis=1)
    # O9: 连续趋势评分 0~100（资金动量60% + 广度20% + 连板强度20%）
    df["trend_score"] = _calc_trend_score(df)
    return df


def _classify_phase(row: pd.Series) -> str:
    """根据资金加速、广度、连板高度判断板块阶段。"""
    flow_5d = row["flow_5d"]
    flow_3d = row["flow_3d"]
    breadth = row["pct_above_ma20"]
    consec = row["consecutive_limit_high"]

    if flow_5d < 0:
        return "退潮"

    is_accelerating = (
        flow_3d > 0
        and flow_5d > 0
        and (flow_3d / 3) > (flow_5d / 5) * 1.2
    )
    if is_accelerating and breadth > 0.15:
        return "升温"

    if flow_5d > 0 and breadth > 0.30 and consec > 4:
        return "趋势"

    return "中性"


def _calc_trend_score(df: pd.DataFrame) -> pd.Series:
    """
    O9: 连续趋势评分 0~100。
    吴川体系中 trend_score=21.41 是比阶段标签更精细的连续指标。

    公式：资金动量(60%) + 广度(20%) + 连板强度(20%)
    - 资金动量：5日净流入百分位（带加速因子）
    - 广度：MA20广度直接映射
    - 连板强度：连板高度百分位
    """
    flow_pct = _percentile_rank(df["flow_5d"]) * 100
    # 加速因子：近3日资金 > 5日均速则加成
    accel = (df["flow_3d"] / 3).clip(lower=-1e9) - (df["flow_5d"] / 5).clip(lower=-1e9)
    accel_score = (accel > 0).astype(float) * 10
    momentum_score = (flow_pct * 0.85 + accel_score).clip(0, 100)

    breadth_score = df["pct_above_ma20"] * 100
    limitup_score = _percentile_rank(df["consecutive_limit_high"]) * 100

    trend = (
        momentum_score * 0.60
        + breadth_score * 0.20
        + limitup_score * 0.20
    ).clip(0, 100)
    return trend.round(1)


def _build_signal(row: pd.Series) -> str:
    phase = row["phase"]
    if phase == "升温":
        return "🔥 加速升温"
    elif phase == "趋势":
        return "↗ 趋势延续"
    elif phase == "退潮":
        return "📉 回避"
    return "— 中性观察"


# ──────────────────────────────────────────────
# 次日风险评分
# ──────────────────────────────────────────────

def _calc_nextday_risk(row: pd.Series, delta_3d: float) -> float:
    """
    次日风险惩罚分 0~100（越高越危险）。

    三个维度：
    1. 板块阶段退潮（40分）
    2. 人气集中度偏高（30分）—— 拥挤度高的板块容易踩踏
    3. 资金净流出（30分）
    """
    risk = 0.0

    # 退潮惩罚
    if row["phase"] == "退潮":
        risk += 40.0
    elif row["phase"] == "中性" and delta_3d < -5:
        risk += 20.0  # 快速降温的中性板块也有风险

    # 人气集中度惩罚（>0.3=高度拥挤，>0.5=极度拥挤）
    pop = row["pop_concentration"]
    if pop > 0.5:
        risk += 30.0
    elif pop > 0.3:
        risk += 15.0

    # 资金流出惩罚
    if row["flow_5d"] < 0:
        risk += 30.0
    elif row["flow_3d"] < 0 and row["flow_5d"] > 0:
        # 5日整体流入但近3日转向流出——动能衰减
        risk += 15.0

    return min(risk, 100.0)


# ──────────────────────────────────────────────
# 分层决策
# ──────────────────────────────────────────────

def _make_decision(
    heat_score: float,
    delta_3d: float,
    nextday_risk: float,
    phase: str,
) -> tuple[str, int]:
    """
    综合热度、趋势动量、次日风险输出分层决策与评分。

    decision_score 公式：
        热度贡献(50%) + 趋势动量(30%) + 次日安全边际(20%)

    决策阈值：
        ≥ 60 → buy  🟢
        ≥ 40 → watch 🟡
        <  40 → avoid 🔴
    """
    # 趋势动量分（0-100）: delta > 0 加分，< 0 减分，每单位变化3分
    trend_momentum = max(0.0, min(100.0, 50.0 + delta_3d * 3))

    # 次日安全边际（风险的反面）
    safety = max(0.0, 100.0 - nextday_risk)

    raw = (
        heat_score * 0.50
        + trend_momentum * 0.30
        + safety * 0.20
    )

    # 退潮板块硬性惩罚
    if phase == "退潮":
        raw -= 20.0

    score = max(0, min(100, int(raw)))

    if score >= 60:
        decision = "buy"
    elif score >= 40:
        decision = "watch"
    else:
        decision = "avoid"

    return decision, score


# ──────────────────────────────────────────────
# 热度历史缓存（用于 delta 计算）
# ──────────────────────────────────────────────

def _get_cache_dir() -> Path:
    settings = get_settings()
    p = settings.cache_dir / _SECTOR_STATS_CACHE_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_heat_scores(trade_date: str, heat_map: dict[str, float]) -> None:
    """将今日各行业热度分存为 parquet，供后续计算 delta 使用。"""
    try:
        df = pd.DataFrame(list(heat_map.items()), columns=["industry", "heat_score"])
        path = _get_cache_dir() / f"{trade_date}.parquet"
        df.to_parquet(path, index=False)
        logger.debug("已缓存热度分: %s", path)
    except Exception as e:
        logger.warning("缓存热度分失败: %s", e)


def _load_prior_heat_scores(trade_date: str, days_back: int = 3) -> dict[str, float]:
    """
    加载 days_back 个交易日前的热度分。
    遍历缓存目录，找到在 trade_date 之前最近的文件。
    """
    try:
        cache_dir = _get_cache_dir()
        files = sorted(
            [f for f in cache_dir.glob("*.parquet") if f.stem < trade_date],
            reverse=True,
        )
        if len(files) < days_back:
            # 没有足够历史，返回空字典（delta 将显示为 0）
            return {}

        target_file = files[days_back - 1]
        df = pd.read_parquet(target_file)
        result = dict(zip(df["industry"], df["heat_score"]))
        logger.debug("加载 %d 日前热度分: %s", days_back, target_file.name)
        return result
    except Exception as e:
        logger.debug("加载历史热度分失败: %s", e)
        return {}


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _percentile_rank(series: pd.Series) -> pd.Series:
    """将 Series 归一化为百分位排名（0~1）。"""
    return series.rank(pct=True).fillna(0.5)


# ──────────────────────────────────────────────
# O8: 概念板块分析（代码已就绪，VPN/服务器后自动生效）
# ──────────────────────────────────────────────

def calc_concept_stats(
    trade_date: str,
    provider: DataProvider,
    close_m: pd.DataFrame,
    top_n: int = 30,
) -> list[SectorStat]:
    """
    O8: 计算概念板块热度（对标吴川"航天/低空经济/磷化工"等概念维度）。

    与行业板块互补：
    - 行业板块（已有）= Tushare stock_basic.industry，覆盖全市场
    - 概念板块（本函数）= Akshare 东方财富概念板，捕捉市场热点概念

    依赖 akshare 接口（本地VPN限制，服务器部署后生效）：
    - ak.stock_board_concept_name_em()  → 概念列表
    - ak.stock_board_concept_cons_em()  → 概念成分股

    Args:
        trade_date: 交易日 YYYYMMDD
        provider:   CompositeProvider
        close_m:    历史收盘价矩阵
        top_n:      只分析热度最高的前 N 个概念

    Returns:
        SectorStat 列表（board_type="concept"），按决策评分降序
    """
    try:
        # 1. 获取概念列表
        concept_list = provider.get_concept_list()
        if concept_list is None or concept_list.empty:
            logger.debug("[概念板块] 接口不可用（VPN或服务器问题），跳过")
            return []

        # 2. 过滤到活跃概念（按涨跌幅取 top_n）
        if "涨跌幅" in concept_list.columns:
            concept_list = concept_list.nlargest(top_n, "涨跌幅")
        else:
            concept_list = concept_list.head(top_n)

        # 3. 逐概念计算成分股指标
        daily = provider.get_daily(trade_date)
        if daily is None or daily.empty:
            return []

        stock_basic = provider.get_stock_basic()
        money_flow_df = provider.get_money_flow(trade_date)

        results: list[SectorStat] = []
        for _, row in concept_list.iterrows():
            concept_name = str(row.get("板块名称", row.get("name", "")))
            if not concept_name:
                continue

            try:
                # 获取成分股
                members = provider.get_concept_members(concept_name)
                if members is None or members.empty:
                    continue

                # 成分股代码（东方财富格式可能需要转换）
                member_codes = _normalize_concept_codes(members)
                if not member_codes:
                    continue

                # 计算板块指标
                stat = _calc_concept_single(
                    concept_name=concept_name,
                    member_codes=member_codes,
                    trade_date=trade_date,
                    daily=daily,
                    close_m=close_m,
                    money_flow_df=money_flow_df,
                )
                if stat:
                    results.append(stat)

            except Exception as e:
                logger.debug("概念[%s]计算失败: %s", concept_name, e)
                continue

        results.sort(key=lambda x: x.decision_score, reverse=True)
        logger.info("[概念板块] 共分析 %d 个概念", len(results))
        return results

    except Exception as e:
        logger.debug("[概念板块] 整体失败: %s", e)
        return []


def _normalize_concept_codes(members: pd.DataFrame) -> list[str]:
    """将东方财富概念成分股 DataFrame 转为 ts_code 列表。"""
    code_col = next(
        (c for c in ["代码", "ts_code", "code", "股票代码"] if c in members.columns),
        None
    )
    if not code_col:
        return []

    codes = []
    for code in members[code_col].astype(str):
        code = code.strip().lstrip("0").zfill(6) if len(code.lstrip("0")) < 6 else code[:6]
        # 转为 ts_code 格式（xxx.SH / xxx.SZ）
        if code.startswith(("6", "9")):
            codes.append(f"{code}.SH")
        elif code.startswith(("0", "3")):
            codes.append(f"{code}.SZ")
        elif code.startswith(("4", "8")):
            codes.append(f"{code}.BJ")
    return codes


def _calc_concept_single(
    concept_name: str,
    member_codes: list[str],
    trade_date: str,
    daily: pd.DataFrame,
    close_m: pd.DataFrame,
    money_flow_df: pd.DataFrame | None,
) -> SectorStat | None:
    """计算单个概念板块的热度指标。"""
    code_set = set(member_codes)

    # 当日行情
    day_stocks = daily[daily["ts_code"].isin(code_set)].copy()
    if day_stocks.empty:
        return None

    # 涨停家数
    limit_up = int((day_stocks["pct_chg"] >= 9.5).sum())

    # MA20广度
    breadth = 0.0
    available_dates = sorted(d for d in close_m.index if d <= trade_date)
    if len(available_dates) >= 21:
        recent = close_m.loc[available_dates[-21:]]
        ma20 = recent.iloc[:-1].mean()
        last_close = recent.iloc[-1]
        concept_close = last_close.reindex(list(code_set)).dropna()
        concept_ma20 = ma20.reindex(list(code_set)).dropna()
        common = concept_close.index.intersection(concept_ma20.index)
        if len(common) > 0:
            breadth = float((concept_close[common] > concept_ma20[common]).mean())

    # 资金流（汇总5日，这里简化为当日）
    flow_5d = 0.0
    flow_3d = 0.0
    if money_flow_df is not None and not money_flow_df.empty:
        concept_flow = money_flow_df[money_flow_df["ts_code"].isin(code_set)]
        if not concept_flow.empty:
            flow_5d = float(concept_flow["net_mf_amount"].sum() * 1e4)
            flow_3d = flow_5d  # 简化，单日作为3日代理

    # 人气集中度
    total_amount = day_stocks["amount"].sum()
    top3_amount = day_stocks.nlargest(3, "amount")["amount"].sum() if len(day_stocks) >= 3 else total_amount
    pop_conc = float(top3_amount / total_amount) if total_amount > 0 else 0.0

    # 简化版热度评分（单日数据）
    heat = min(
        (limit_up / max(len(code_set), 1)) * 100 * 0.4
        + breadth * 100 * 0.3
        + min(abs(flow_5d) / 1e9, 1.0) * 100 * 0.3,
        100.0,
    )

    # 阶段判断（简化）
    phase = "升温" if flow_5d > 0 and breadth > 0.2 else ("退潮" if flow_5d < 0 else "中性")
    nextday_risk = _calc_nextday_risk(
        pd.Series({"phase": phase, "flow_5d": flow_5d, "flow_3d": flow_3d, "pop_concentration": pop_conc}),
        delta_3d=0.0,
    )
    decision, decision_score = _make_decision(heat, delta_3d=0.0, nextday_risk=nextday_risk, phase=phase)

    return SectorStat(
        industry=concept_name,
        board_type="concept",
        stock_count=len(code_set),
        flow_5d_100m=round(flow_5d / 1e8, 2),
        flow_3d_100m=round(flow_3d / 1e8, 2),
        pct_above_ma20=round(breadth, 4),
        limit_up_count=limit_up,
        heat_score=round(heat, 1),
        pop_concentration=round(pop_conc, 3),
        nextday_risk_penalty=round(nextday_risk, 1),
        decision=decision,
        decision_score=decision_score,
        phase=phase,
        signal="🔥 加速升温" if phase == "升温" else ("📉 回避" if phase == "退潮" else "— 中性"),
    )
