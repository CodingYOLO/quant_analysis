"""行情中枢 market_hub 单测：三源列名归一化 + nan 清洗。

零网络（注入 FakeProvider）。直接运行：python -m tests.test_market_hub
"""

from __future__ import annotations

import pandas as pd

import app.strategy.market_hub as M


class _Fake:
    def __init__(self, hot=None, news=None, cal=None, up=None):
        self._hot, self._news, self._cal, self._up = hot, news, cal, up

    def get_hot_rank(self):
        return self._hot

    def get_hot_up(self):
        return self._up

    def get_cls_news(self, date):
        return self._news

    def get_econ_calendar(self):
        return self._cal


def setup_function(_=None):
    M._CACHE.clear()        # 每个用例清缓存，避免互相干扰


def test_hot_rank_normalize() -> None:
    M._CACHE.clear()
    df = pd.DataFrame([{"当前排名": 1, "代码": "300308", "股票名称": "中际旭创", "最新价": 52.0, "涨跌幅": 8.9}])
    out = M.hot_rank(_Fake(hot=df), top=10)
    assert out[0]["code"] == "300308" and out[0]["name"] == "中际旭创" and out[0]["pct"] == 8.9


def test_news_flash_clean_nan() -> None:
    M._CACHE.clear()
    df = pd.DataFrame([{"标题": "某快讯", "发布时间": "2026-06-22 13:38:47+08:00",
                        "等级": "A", "来源": "财联社", "摘要": "nan", "链接": "nan"}])
    out = M.news_flash(_Fake(news=df), n=10)
    assert out[0]["title"] == "某快讯" and out[0]["level"] == "A"
    assert out[0]["summary"] == "" and out[0]["url"] == ""        # nan→空
    assert out[0]["time"] == "2026-06-22 13:38:47"               # 去时区


def test_econ_calendar_normalize() -> None:
    M._CACHE.clear()
    df = pd.DataFrame([{"日期": "2026-06-22", "时间": "21:30", "地区": "美国", "事件": "非农",
                        "公布": "nan", "预期": "20万", "前值": "18万", "重要性": 3}])
    out = M.econ_calendar(_Fake(cal=df))
    assert out[0]["event"] == "非农" and out[0]["actual"] == "" and out[0]["forecast"] == "20万"


def test_hot_up_normalize() -> None:
    M._CACHE.clear()
    df = pd.DataFrame([{"当前排名": 3, "排名较昨日变动": 17, "代码": "300398", "股票名称": "飞凯材料",
                        "最新价": 49.0, "涨跌幅": 7.3}])
    out = M.hot_up(_Fake(up=df), top=10)
    assert out[0]["rank_chg"] == 17 and out[0]["name"] == "飞凯材料"


def test_cache_serves_stale_on_failure() -> None:
    """先成功缓存→再失败应返回上次成功结果(不空屏)。"""
    M._CACHE.clear()
    good = pd.DataFrame([{"当前排名": 1, "代码": "300308", "股票名称": "中际旭创", "最新价": 52, "涨跌幅": 9}])
    assert M.hot_rank(_Fake(hot=good), 10)[0]["name"] == "中际旭创"   # 缓存成功结果
    M._CACHE["hot10"] = (0, M._CACHE["hot10"][1])                     # 强制过期
    out = M.hot_rank(_Fake(hot=pd.DataFrame()), 10)                   # 这次拉空
    assert out and out[0]["name"] == "中际旭创"                       # 仍返上次成功


def test_empty_safe() -> None:
    M._CACHE.clear()
    assert M.hot_rank(_Fake(hot=pd.DataFrame()), 10) == []
    assert M.news_flash(_Fake(news=None), 10) == []


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ test_market_hub 全部通过（{len(fns)} 项）")


if __name__ == "__main__":
    _run_all()
