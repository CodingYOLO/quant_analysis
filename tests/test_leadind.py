"""领先指标页一致性测试（2026-08-03）：注册表/链条映射/适配器/涨跌幅口径 四方对齐。"""

from __future__ import annotations

import pandas as pd

from app.macro import registry
from app.macro.adapters.ts_fut import FUT_CODES
from app.macro.compute import changes
from app.macro.leadind import CHAINS, IMPACT, validate_leadind


def test_registry_validates_with_ind_layer():
    assert registry.validate() == []


def test_leadind_mapping_consistency():
    # 链条引用存在·IND指标都有传导映射·每个只进一条链·适配器与注册表一一对应
    assert validate_leadind() == []


def test_all_fut_codes_registered_and_enabled():
    reg = {m.code: m for m in registry.METRICS}
    for code in FUT_CODES:
        assert code in reg, code
        m = reg[code]
        assert m.layer == "IND_leading" and m.enabled and m.freq == "daily"
        # 商品价格必须走涨跌幅%口径：unit 为 点 或以 元/ 开头（compute.changes 按此分派）
        assert m.unit == "点" or m.unit.startswith("元/"), f"{code}: unit={m.unit}"
        # direction 必须为0：商品价格对全A无统一方向·不得进宏观TOTAL评分
        assert m.direction == 0, code


def test_changes_price_unit_uses_pct():
    s = pd.Series([100.0, 110.0, 99.0])
    c1, _ = changes(s, "元/吨", "daily")
    assert round(float(c1.iloc[1]), 2) == 10.0          # 涨跌幅% 而非绝对差(=10元)
    assert round(float(c1.iloc[2]), 2) == -10.0


def test_changes_amount_unit_stays_absolute():
    # 回归保护：量级类(亿元)仍走绝对差——资金净流入过零·百分比无意义(既有铁律)
    s = pd.Series([5.0, -3.0])
    c1, _ = changes(s, "亿元", "daily")
    assert round(float(c1.iloc[1]), 2) == -8.0


def test_chain_reused_macro_codes_exist():
    reg = {m.code for m in registry.METRICS}
    for ch in CHAINS:
        for c in ch["codes"]:
            assert c in reg, f"{ch['label']} 引用不存在指标 {c}"


def test_impact_texts_nonempty():
    for code, imp in IMPACT.items():
        assert imp.get("up_good"), f"{code}: up_good 为空"
