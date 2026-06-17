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


def _ctx(mf3=0.0, pct5=3.0, pct3=3.0, top100=10.0) -> dict:
    """直接给定分位阈值，隔离规则与分位计算。"""
    return {"mf3_cut": mf3, "pct5_cut": pct5, "pct3_cut": pct3, "top100_cut": top100}


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
    ctx = _ctx()
    # 中期资金在 + 当日资金流出 + 结构未破 → 命中（价格未跌也可）
    assert ss._is_dip(_row(money_flow_5d=20, money_flow_1d=-5, pct_chg_1d=1.0, breadth_ma20=50), ctx) is True
    # 价格回调亦算分歧
    assert ss._is_dip(_row(money_flow_5d=20, money_flow_1d=3, pct_chg_1d=-0.5, breadth_ma20=50), ctx) is True
    # 中期资金已流出 → 不是低吸（是离场）
    assert ss._is_dip(_row(money_flow_5d=-20, money_flow_1d=-5, pct_chg_1d=-1.0, breadth_ma20=50), ctx) is False
    # 无分歧（资金流入且价涨）→ 不命中
    assert ss._is_dip(_row(money_flow_5d=20, money_flow_1d=5, pct_chg_1d=1.0, breadth_ma20=50), ctx) is False
    # 结构已破（广度 < 45）→ 不低吸破位板块
    assert ss._is_dip(_row(money_flow_5d=20, money_flow_1d=-5, pct_chg_1d=-1.0, breadth_ma20=30), ctx) is False


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


def test_missing_data_never_falsely_hits() -> None:
    """全 None 行不得命中任何诊断（避免对缺数据板块误判）。"""
    ctx = _ctx()
    empty = _row()
    assert ss._is_rotate(empty, ctx) is False
    assert ss._is_dip(empty, ctx) is False
    assert ss._is_risk(empty, ctx) is False


def test_percentile_and_context() -> None:
    assert ss._percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.8) == 9
    assert ss._percentile([], 0.5) == float("inf")  # 空集合不命中任何 ≥ 判断
    rows = [_row(money_flow_3d=float(i), pct_chg_5d=float(i),
                 pct_chg_3d=float(i), top100_ratio=float(i)) for i in range(10)]
    ctx = ss._build_context(rows)
    for key in ("mf3_cut", "pct5_cut", "pct3_cut", "top100_cut"):
        assert key in ctx


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
