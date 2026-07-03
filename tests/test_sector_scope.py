"""
板块全景分类引擎（sector_scope）单元测试。

零依赖：用合成行覆盖三类诊断规则的边界，可直接运行：
    python -m tests.test_sector_scope
（环境装有 pytest 时亦可 `pytest tests/test_sector_scope.py`）
"""

from __future__ import annotations

from app.strategy import sector_scope as ss


def _row(**kw) -> dict:
    """构造一条最小宽表行（缺省字段为 None，模拟数据缺失）。"""
    base = {
        "theme_name": "测试板块", "theme_type": "industry", "sample_count": 30,
        "money_flow_1d": None, "money_flow_3d": None, "money_flow_5d": None,
        "pct_chg_1d": None, "pct_chg_3d": None, "pct_chg_5d": None, "pct_chg_7d": None,
        "breadth_ma20": None, "top100_ratio": None,
    }
    base.update(kw)
    return base


def _ctx(mf3=0.0, mf5=0.0, pct5=3.0, pct3=3.0, top100=10.0, pct1_median=1.5) -> dict:
    """直接给定阈值，隔离规则与分位计算。"""
    return {"mf3_cut": mf3, "mf5_cut": mf5, "pct5_cut": pct5, "pct3_cut": pct3,
            "top100_cut": top100, "pct1_median": pct1_median}


def test_rotate_hit_and_miss() -> None:
    ctx = _ctx(mf3=10.0)
    hit = _row(money_flow_3d=50, pct_chg_3d=2.0, breadth_ma20=60)
    assert ss._is_rotate(hit, ctx) is True
    # 资金未达分位 → 不命中
    assert ss._is_rotate(_row(money_flow_3d=5, pct_chg_3d=2.0, breadth_ma20=60), ctx) is False
    # 结构不健康（广度 < 50）→ 不命中
    assert ss._is_rotate(_row(money_flow_3d=50, pct_chg_3d=2.0, breadth_ma20=40), ctx) is False
    # 价格未涨 → 不命中
    assert ss._is_rotate(_row(money_flow_3d=50, pct_chg_3d=-1.0, breadth_ma20=60), ctx) is False


def test_dip_divergence_signal() -> None:
    ctx = _ctx(pct1_median=1.5)
    base = dict(money_flow_5d=20, pct_chg_5d=4.0, breadth_ma20=50)  # 中期趋势/资金在 + 结构未破
    # 当日资金流出 → 分歧命中（价格未跌也可）
    assert ss._is_dip(_row(**base, money_flow_1d=-5, pct_chg_1d=3.0), ctx) is True
    # 涨幅相对走弱（1日 < 当日中位 1.5）→ 分歧命中
    assert ss._is_dip(_row(**base, money_flow_1d=3, pct_chg_1d=0.5), ctx) is True
    # 中期趋势已走坏（5日涨 ≤ 0）→ 不是低吸
    assert ss._is_dip(_row(money_flow_5d=20, pct_chg_5d=-1.0, money_flow_1d=-5,
                           pct_chg_1d=0.5, breadth_ma20=50), ctx) is False
    # 中期资金已流出 → 不是低吸（是离场）
    assert ss._is_dip(_row(money_flow_5d=-20, pct_chg_5d=4.0, money_flow_1d=-5,
                           pct_chg_1d=0.5, breadth_ma20=50), ctx) is False
    # 无分歧（资金流入且涨幅强于中位）→ 不命中
    assert ss._is_dip(_row(**base, money_flow_1d=5, pct_chg_1d=3.0), ctx) is False
    # 结构已破（广度 < 45）→ 不低吸破位板块
    assert ss._is_dip(_row(money_flow_5d=20, pct_chg_5d=4.0, money_flow_1d=-5,
                           pct_chg_1d=0.5, breadth_ma20=30), ctx) is False


def test_classify_dip_excludes_overheated() -> None:
    """已过热(高位风险)的板块不应进低吸栏。"""
    # 构造一个既满足低吸又过热的板块：5日涨高+超买 → risk；同时今日资金流出
    rows = [_row(theme_name="过热票", money_flow_5d=20, pct_chg_5d=12.0,
                 pct_chg_3d=8.0, pct_chg_1d=0.2, money_flow_1d=-3,
                 breadth_ma20=85, top100_ratio=25)]
    rotate, dip, risk, ambush = ss._classify(rows, ss._build_context(rows))
    assert rows[0] in risk
    assert rows[0] not in dip  # 互斥：过热不进低吸


def test_risk_overbought_or_crowded() -> None:
    ctx = _ctx(pct5=5.0, pct3=5.0, top100=15.0)
    # 涨幅居前 + 极度超买（广度≥70）→ 命中
    assert ss._is_risk(_row(pct_chg_5d=8, breadth_ma20=80, top100_ratio=2), ctx) is True
    # 涨幅居前 + 拥挤居前（Top100≥分位）→ 命中
    assert ss._is_risk(_row(pct_chg_5d=8, breadth_ma20=40, top100_ratio=20), ctx) is True
    # 涨幅不够 → 不命中（即便拥挤）
    assert ss._is_risk(_row(pct_chg_5d=1, breadth_ma20=40, top100_ratio=20), ctx) is False
    # 涨幅够但既不超买也不拥挤 → 不命中
    assert ss._is_risk(_row(pct_chg_5d=8, breadth_ma20=40, top100_ratio=2), ctx) is False


def test_ambush_hit_and_miss() -> None:
    """资金暗流：净流入显著(≥mf5_cut) + 5日涨幅走平[-3%,3%) + 结构未破(≥45%) + 今日未净流出。"""
    ctx = _ctx(mf5=10.0)
    base = dict(money_flow_5d=50, pct_chg_5d=-0.5, money_flow_1d=5, breadth_ma20=70)
    assert ss._is_ambush(_row(**base), ctx) is True                    # 走平+显著+结构好=命中
    # 核心修正：大跌(5日-6%)不算"价没涨" → 不命中（承接下跌≠埋伏）
    assert ss._is_ambush(_row(money_flow_5d=50, pct_chg_5d=-6.0,
                              money_flow_1d=5, breadth_ma20=70), ctx) is False
    # 净流入不显著(+1亿噪音 < 分位) → 不命中
    assert ss._is_ambush(_row(money_flow_5d=1, pct_chg_5d=-0.5,
                              money_flow_1d=5, breadth_ma20=70), ctx) is False
    # 结构已破(广度<45) → 不命中（破位下跌"接刀"非埋伏）
    assert ss._is_ambush(_row(money_flow_5d=50, pct_chg_5d=-0.5,
                              money_flow_1d=5, breadth_ma20=30), ctx) is False
    # 已涨(5日≥3%) → 不算"没涨" → 不命中
    assert ss._is_ambush(_row(money_flow_5d=50, pct_chg_5d=5.0,
                              money_flow_1d=5, breadth_ma20=70), ctx) is False
    # 今日资金净流出 → 不命中
    assert ss._is_ambush(_row(money_flow_5d=50, pct_chg_5d=-0.5,
                              money_flow_1d=-5, breadth_ma20=70), ctx) is False


def test_missing_data_never_falsely_hits() -> None:
    """全 None 行不得命中任何诊断（避免对缺数据板块误判）。"""
    ctx = _ctx()
    empty = _row()
    assert ss._is_rotate(empty, ctx) is False
    assert ss._is_dip(empty, ctx) is False
    assert ss._is_risk(empty, ctx) is False
    assert ss._is_ambush(empty, ctx) is False


def test_percentile_and_context() -> None:
    assert ss._percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.8) == 9
    assert ss._percentile([], 0.5) == float("inf")  # 空集合不命中任何 ≥ 判断
    rows = [_row(money_flow_3d=float(i), pct_chg_5d=float(i),
                 pct_chg_3d=float(i), top100_ratio=float(i)) for i in range(10)]
    ctx = ss._build_context(rows)
    for key in ("mf3_cut", "mf5_cut", "pct5_cut", "pct3_cut", "top100_cut"):
        assert key in ctx


def test_stage_tops_ordering() -> None:
    """主升按强度降序；退潮按 Δ 升序（衰减最快在前）。"""
    rows = [
        _row(theme_name="强主升", phase="升温", heat_score=88, heat_score_delta_3d=10),
        _row(theme_name="次主升", phase="趋势", heat_score=80, heat_score_delta_3d=5),
        _row(theme_name="震荡票", phase="震荡", heat_score=50, heat_score_delta_3d=0),
        _row(theme_name="缓退", phase="退潮", heat_score=30, heat_score_delta_3d=-10),
        _row(theme_name="急退", phase="退潮", heat_score=25, heat_score_delta_3d=-40),
    ]
    surge, decay = ss._stage_tops(rows)
    assert [r["theme_name"] for r in surge] == ["强主升", "次主升"]   # 震荡不入主升
    assert [r["theme_name"] for r in decay] == ["急退", "缓退"]       # Δ最负在前


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
