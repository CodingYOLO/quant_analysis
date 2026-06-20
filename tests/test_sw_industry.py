"""
申万行业数据层单测：index_member_all 分页拼接 + stock_basic.industry 覆盖为申万一级。

零依赖（裸构造 TushareProvider + 假 pro_api），可直接运行：python -m tests.test_sw_industry
"""

from __future__ import annotations

import pandas as pd

from app.data.tushare_provider import TushareProvider


def _stub_provider(fake_api=None) -> TushareProvider:
    """绕过 __init__（避免真 token），注入假 api。"""
    p = object.__new__(TushareProvider)
    p._api = fake_api
    return p


# ── 分页拉取（index_member_all 单页上限 3000）─────────────────────────────────
class _FakeApi:
    """模拟 index_member_all：offset 0→3000行、3000→2864行、再往后空。"""
    def __init__(self):
        self.calls = []

    def index_member_all(self, is_new="Y", fields="", offset=0, limit=3000):
        self.calls.append(offset)
        def page(n, base):
            return pd.DataFrame({
                "l1_code": ["801080.SI"] * n, "l1_name": ["电子"] * n,
                "l2_code": ["801081.SI"] * n, "l2_name": ["半导体"] * n,
                "ts_code": [f"{base + i:06d}.SZ" for i in range(n)],
                "name": [f"股{base + i}" for i in range(n)],
            })
        if offset == 0:
            return page(3000, 0)
        if offset == 3000:
            return page(2864, 3000)
        return pd.DataFrame()


def test_fetch_sw_pagination_concats_all() -> None:
    api = _FakeApi()
    out = _stub_provider(api)._fetch_sw_industry_map()
    assert len(out) == 5864                       # 3000 + 2864
    assert api.calls == [0, 3000]                 # 末页(<3000)即停，省一次空调用
    assert list(out.columns) == ["ts_code", "l1_code", "l1_name", "l2_code", "l2_name"]
    assert out["ts_code"].is_unique               # 去重


def test_fetch_sw_single_page() -> None:
    class _OnePage:
        def index_member_all(self, **k):
            if k.get("offset", 0) == 0:
                return pd.DataFrame({"l1_code": ["a"], "l1_name": ["电子"], "l2_code": ["b"],
                                     "l2_name": ["半导体"], "ts_code": ["000001.SZ"], "name": ["平安"]})
            return pd.DataFrame()
    out = _stub_provider(_OnePage())._fetch_sw_industry_map()
    assert len(out) == 1 and out.iloc[0]["l1_name"] == "电子"


# ── 覆盖 industry 为申万一级 ──────────────────────────────────────────────────
def _raw_basic() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "600519.SH", "999999.SZ"],   # 第三只不在申万映射
        "name": ["平安银行", "贵州茅台", "某新股"],
        "industry": ["银行", "白酒", "其他"],                  # Tushare 原行业
    })


def _sw_map() -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "600519.SH"],
        "l1_code": ["801780.SI", "801120.SI"], "l1_name": ["银行", "食品饮料"],
        "l2_code": ["x", "y"], "l2_name": ["国有大型银行", "白酒Ⅱ"],
    })


def test_overlay_sw_industry() -> None:
    p = _stub_provider()
    p.get_sw_industry_map = lambda: _sw_map()          # type: ignore[assignment]
    out = p._overlay_sw_industry(_raw_basic())
    by = out.set_index("ts_code")
    # 申万二级=主口径(industry)，一级在 industry_l1（供上卷）
    assert by.loc["600519.SH", "industry"] == "白酒Ⅱ"      # 主口径=申万二级
    assert by.loc["600519.SH", "industry_l1"] == "食品饮料"  # 上卷=申万一级
    assert by.loc["000001.SZ", "industry"] == "国有大型银行"
    # 保留 Tushare 原值
    assert by.loc["600519.SH", "industry_src"] == "白酒"
    # 不在申万映射 → 回退原 Tushare 行业（不丢覆盖）
    assert by.loc["999999.SZ", "industry"] == "其他"
    assert pd.isna(by.loc["999999.SZ", "industry_l1"])


def test_overlay_fallback_when_sw_empty() -> None:
    p = _stub_provider()
    p.get_sw_industry_map = lambda: pd.DataFrame()      # type: ignore[assignment]
    out = p._overlay_sw_industry(_raw_basic())
    # 申万不可用 → industry 原样不变，不加 industry_src（优雅回退）
    assert out["industry"].tolist() == ["银行", "白酒", "其他"]
    assert "industry_src" not in out.columns


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
