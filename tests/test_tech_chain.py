"""产业链地图 tech_chain 单测：龙头领涨置顶 + 节点强弱分档 + 今日风格判断。

零网络（注入 FakeProvider 的 get_spot_em）。直接运行：python -m tests.test_tech_chain
"""

from __future__ import annotations

import pandas as pd

import app.strategy.tech_chain as C


class _Fake:
    def __init__(self, pct_by_code):
        rows = [{"ts_code": C._to_ts(k), "name": k, "price": 10, "pct_chg": v}
                for k, v in pct_by_code.items()]
        self._df = pd.DataFrame(rows)

    def get_realtime_quote(self, codes):
        return self._df


def _provider(**pct):
    C._SPOT_CACHE.clear(); C._STALE.clear()
    return _Fake(pct)


def test_level_buckets() -> None:
    assert C._level(5.0) == "strong" and C._level(1.0) == "warm"
    assert C._level(0.0) == "neutral" and C._level(-3.0) == "weak"


def test_node_leaders_sort_and_anchor() -> None:
    C._SPOT_CACHE.clear()
    node = {"name": "x", "leaders": [("龙头A", "001"), ("中军B", "002"), ("中军C", "003")]}
    spot = {"001": {"pct": 2.0}, "002": {"pct": 9.0}, "003": {"pct": -1.0}}
    leaders, avg = C._node_leaders(node, spot)
    assert leaders[0]["name"] == "中军B"          # 今日领涨置顶(9%)
    assert any(x["is_anchor"] and x["name"] == "龙头A" for x in leaders)  # 结构龙头标记仍在A
    assert avg == round((2 + 9 - 1) / 3, 2)


def test_build_chain_structure() -> None:
    # 给半导体设备龙头一个涨幅，验证节点上色 + 领头羊
    prov = _provider(**{"002371": 8.0, "688012": 6.0, "688072": 4.0})
    out = C.build_chain(prov, "半导体")
    assert out["ok"] and out["name"] == "半导体"
    n0 = out["layers"][0]["nodes"][0]               # 半导体设备
    assert n0["level"] == "strong" and n0["lead"]["pct"] == 8.0   # 领头羊=涨最多的


def test_today_style_material_lean() -> None:
    C._SPOT_CACHE.clear(); C._STALE.clear()
    # 只给资源材料层的票大涨 → 风格应判"资源/材料端"
    mat_codes = {"002371": 7, "688126": 7, "002428": 7, "688498": 7, "688046": 7,
                 "600111": 7, "600549": 7, "002155": 7, "002460": 7, "601899": 7,
                 "601600": 7, "600183": 7, "002741": 7, "688519": 7, "300398": 7, "600497": 7,
                 "688313": 7, "600487": 7, "000831": 7, "600259": 7, "000657": 7, "002378": 7,
                 "600301": 7, "000960": 7, "002466": 7, "603993": 7, "600362": 7, "000807": 7}
    st = C.today_style(_Fake(mat_codes))
    assert st["lean"] == "material" and "资源" in st["text"]


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_tech_chain 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
