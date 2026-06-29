"""大盘体检数据层——纯函数单测（不连网·确定性）。

覆盖 净涨停 / 市场状态 / 地量冰点信号 / 事件研究 四个纯函数。
build_overview 与 _sector_matrix 依赖 provider 取数，此处不连网测试。
"""

from __future__ import annotations

from app.strategy.market_overview import (
    detect_dryup_signals,
    event_study,
    net_limit_series,
    regime_series,
)


def test_net_limit() -> None:
    assert net_limit_series([80, 10, None], [5, 30, 4]) == [75, -20, -4]
    assert net_limit_series([], []) == []


def test_regime_strong_mid_weak() -> None:
    # 强：涨停多+广度高+无跌停
    assert regime_series([90], [3], [62])[0]["label"] == "强"
    # 弱：跌停多
    assert regime_series([10], [45], [50])[0]["label"] == "弱"
    # 弱：广度冰点
    assert regime_series([20], [5], [30])[0]["label"] == "弱"
    # 震荡：两不靠
    assert regime_series([40], [10], [48])[0]["label"] == "震荡"
    # None 广度安全退化为震荡
    assert regime_series([40], [10], [None])[0]["label"] == "震荡"


def test_dryup_signal_fires_on_local_low_ice() -> None:
    # 构造一个成交额谷底 + 广度冰点的局部低点（索引5）
    amount = [1.2, 1.1, 1.0, 0.9, 0.7, 0.5, 0.8, 1.0, 1.1, 1.3, 1.2, 1.0]
    breadth = [55, 50, 45, 38, 30, 22, 35, 55, 60, 70, 65, 58]
    sigs = detect_dryup_signals(amount, breadth)
    assert 5 in sigs


def test_dryup_no_signal_when_breadth_high() -> None:
    # 同样地量但广度不冰点 → 不触发
    amount = [1.2, 1.1, 1.0, 0.9, 0.7, 0.5, 0.8, 1.0, 1.1, 1.3, 1.2, 1.0]
    breadth = [70] * 12
    assert detect_dryup_signals(amount, breadth) == []


def test_dryup_collapses_adjacent() -> None:
    # 连续多日都满足 → 只留一个（5日内合并）
    amount = [1.5, 1.4, 0.5, 0.45, 0.5, 0.48, 0.5, 1.4, 1.5, 1.6, 1.5, 1.4]
    breadth = [60, 55, 30, 28, 30, 29, 31, 55, 60, 65, 62, 58]
    sigs = detect_dryup_signals(amount, breadth)
    assert len(sigs) == 1


def test_event_study_aligns_and_counts() -> None:
    # index_cum 相对首日累计%；信号在索引5，其后明显反弹
    idx = [0, -1, -2, -3, -4, -5, -3, -1, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    ev = event_study(idx, [5], pre=3, horizon=10)
    assert ev["n"] == 1
    assert ev["rel_days"][0] == -3 and ev["rel_days"][-1] == 10
    # 信号日(第0天)基准为0
    zero_pos = ev["rel_days"].index(0)
    assert ev["paths"][0][zero_pos] == 0
    # T+5 相对信号日为正 → 胜率100%
    assert ev["winrate_t5"] == 100
    assert ev["median_t5"] is not None and ev["median_t5"] > 0


def test_event_study_skips_out_of_range() -> None:
    # 信号太靠边(放不下 pre/horizon) → 跳过·n=0
    idx = [0, 1, 2, 3, 4]
    ev = event_study(idx, [1], pre=3, horizon=10)
    assert ev["n"] == 0 and ev["mean"] == [] and ev["winrate_t5"] is None


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_market_overview 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
