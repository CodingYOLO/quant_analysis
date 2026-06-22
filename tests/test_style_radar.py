"""风格切换雷达单测：风格映射 / 轮动判定 / 聚合排名（纯函数·零网络）。"""

from __future__ import annotations

import app.strategy.style_radar as SR


# ---------------------------------------------------------------------------
# 1. 行业 → 风格映射 style_of
# ---------------------------------------------------------------------------

def test_style_of_known() -> None:
    assert SR.style_of("半导体") == "科技TMT"
    assert SR.style_of("工业金属") == "周期资源"
    assert SR.style_of("专用设备") == "高端制造"
    assert SR.style_of("白酒Ⅱ") == "医药消费"
    assert SR.style_of("国有大型银行Ⅱ") == "金融地产"
    assert SR.style_of("电力") == "公用基建"


def test_style_of_unknown_and_blank() -> None:
    assert SR.style_of("不存在的行业") == "综合其他"
    assert SR.style_of("") == "综合其他"
    assert SR.style_of("  半导体  ") == "科技TMT"      # 去空格


# ---------------------------------------------------------------------------
# 2. 轮动判定 _rotation（排名跃升=切入·下滑=切出）
# ---------------------------------------------------------------------------

def test_rotation_in_when_rank_jumps_up() -> None:
    # 5日排第6 → 今日排第2：资金正切入
    assert SR._rotation(rank_1d=2, rank_5d=6) == "切入↑"


def test_rotation_out_when_rank_drops() -> None:
    # 5日排第1 → 今日排第4：前期主线退潮
    assert SR._rotation(rank_1d=4, rank_5d=1) == "切出↓"


def test_rotation_flat_within_gap() -> None:
    assert SR._rotation(rank_1d=3, rank_5d=4) == "持平"
    assert SR._rotation(rank_1d=2, rank_5d=2) == "持平"


# ---------------------------------------------------------------------------
# 3. 聚合 + 排名 + 轮动 _compute_styles（合成行）
# ---------------------------------------------------------------------------

def _row(name: str, f1: float, f5: float, p1: float = 1.0, p5: float = 1.0,
         heat: float = 50.0) -> dict:
    return {"theme_name": name, "money_flow_1d": f1, "money_flow_5d": f5,
            "pct_chg_1d": p1, "pct_chg_5d": p5, "heat_score": heat}


def test_compute_aggregates_and_ranks() -> None:
    """科技今日资金最高→rank_1d=1；同风格成员资金累加、涨跌取均值。"""
    rows = [
        _row("半导体", 50.0, 10.0, p1=3.0),
        _row("元件", 30.0, 5.0, p1=1.0),           # 科技合计今日80
        _row("工业金属", 20.0, 40.0, p1=2.0),       # 周期今日20、5日40
    ]
    styles = SR._compute_styles(rows)
    by = {s.style: s for s in styles}
    tech = by["科技TMT"]
    assert tech.n == 2 and tech.flow_1d_yi == 80.0 and tech.flow_5d_yi == 15.0
    assert tech.pct_1d == 2.0                        # (3+1)/2
    assert tech.rank_1d == 1                          # 今日资金最高
    # top_industries 按今日资金降序
    assert tech.top_industries[0]["name"] == "半导体"


def test_compute_detects_rotation_in() -> None:
    """某风格今日资金强但5日弱 → 排名跃升 → 切入↑。"""
    rows = [
        _row("国有大型银行Ⅱ", 5.0, 100.0),   # 金融：今日弱、5日最强
        _row("专用设备", 90.0, 1.0),          # 制造：今日最强、5日最弱
        _row("半导体", 40.0, 50.0),           # 科技：居中
    ]
    res_styles = SR._compute_styles(rows)
    by = {s.style: s for s in res_styles}
    assert by["高端制造"].rank_1d == 1 and by["高端制造"].rank_5d == 3
    assert by["高端制造"].rotation == "切入↑"
    assert by["金融地产"].rotation == "切出↓"


def test_compute_empty_safe() -> None:
    assert SR._compute_styles([]) == []


# ---------------------------------------------------------------------------
# runner（无 pytest 依赖·与项目其余单测一致）
# ---------------------------------------------------------------------------

def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_style_radar 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
