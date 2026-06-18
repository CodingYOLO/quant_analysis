"""
策略保存库 saved_strategies 单元测试（独立临时库，零副作用）。

零依赖，可直接运行：python -m tests.test_saved_strategies
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import app.strategy.saved_strategies as ss


def _use_temp_db():
    """把库路径指向临时文件，避免污染真实 strategies.db。"""
    tmp = Path(tempfile.mkdtemp()) / "strategies.db"
    ss._db_path = lambda: tmp  # type: ignore[assignment]


def test_save_list_delete_flow() -> None:
    _use_temp_db()
    payload = {"factors": ["kdj_gold"], "customs": [{"col": "rps120", "op": "ge", "val": 90}],
               "custom": None, "sort_by": "rps120"}
    sid = ss.save("强势放量", "admin", payload)
    assert sid > 0

    rows = ss.list_strategies()
    assert len(rows) == 1 and rows[0]["name"] == "强势放量"
    assert rows[0]["payload"]["customs"][0]["val"] == 90      # payload 正确还原

    # 同创建者同名覆盖（不新增）
    ss.save("强势放量", "admin", {"factors": ["macd_gold"], "sort_by": "rps50"})
    rows = ss.list_strategies()
    assert len(rows) == 1 and rows[0]["payload"]["factors"] == ["macd_gold"]

    # 他人不可删
    assert ss.delete(sid, "someone_else") is False
    assert len(ss.list_strategies()) == 1
    # 本人可删
    assert ss.delete(sid, "admin") is True
    assert ss.list_strategies() == []


def test_filters() -> None:
    _use_temp_db()
    ss.save("趋势低吸", "admin", {"factors": ["above_ma20"]})
    ss.save("游资打板", "userB", {"factors": ["limit_up"]})
    assert len(ss.list_strategies(creator="admin")) == 1          # 仅看本人
    assert len(ss.list_strategies(q="打板")) == 1                 # 名称搜索
    assert len(ss.list_strategies(q="userB")) == 1               # 创建者搜索
    assert len(ss.list_strategies()) == 2                        # 全部


def test_empty_name_rejected() -> None:
    _use_temp_db()
    try:
        ss.save("  ", "admin", {})
        assert False, "空名应抛异常"
    except ValueError:
        pass


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
