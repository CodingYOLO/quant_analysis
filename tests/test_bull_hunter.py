"""
牛股发掘 bull_hunter 单测：埋伏分纯函数 + 三重硬门槛 + 低位语义 + 催化受限词表 +
事件避雷 + JSON 解析 + 催化层/埋伏层注入式集成（全部零网络）。

零依赖（纯函数 + 假 Provider/假 client + monkeypatch 内部采集），直接运行：
    python -m tests.test_bull_hunter
"""

from __future__ import annotations

import pandas as pd

import app.strategy.bull_hunter as bh


# ── 测试夹具 ────────────────────────────────────────────────────────────────

class _FakeLLM:
    """假 LLMClient：记录调用次数，返回预设文本。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def chat(self, messages, **kw) -> str:
        self.calls += 1
        return self.reply


def _rec(**over) -> dict:
    """一行信号表记录（含埋伏所需位置/资金/量价因子）。"""
    base = {
        "ts_code": "000001.SZ", "name": "测试股", "close": 10.0, "pct_chg": 1.0,
        "bias20": 0.0, "dist_high": -15.0, "change_7d": 2.0, "main_flow_3d": 1.0,
        "vol_ratio": 1.5, "rps50": 50.0, "circ_mv_yi": 150.0, "turnover": 3.0,
    }
    base.update(over)
    return base


def _perf(**over) -> dict:
    base = {"np_yoy": 20.0, "roe": 12.0, "forecast_level": None, "forecast_type": None,
            "express_yoy": None, "events": {}, "summary": ""}
    base.update(over)
    return base


def _ctx(**over) -> dict:
    base = {"heat": 60.0, "rising": False, "delta": 0.0, "net_flow_in": None, "in_catalyst": True}
    base.update(over)
    return base


# ── 通用工具 ────────────────────────────────────────────────────────────────

def test_ramp() -> None:
    assert bh._ramp(5, 0, 10) == 0.5
    assert bh._ramp(-1, 0, 10) == 0.0 and bh._ramp(99, 0, 10) == 1.0
    assert bh._ramp(5, 10, 10) == 0.0 and bh._ramp(12, 10, 10) == 1.0   # lo==hi 退化阶跃


def test_parse_json_array() -> None:
    assert bh._parse_json_array('```json\n[{"a":1}]\n```') == [{"a": 1}]
    assert bh._parse_json_array("前缀噪声 [1, 2, 3] 后缀") == [1, 2, 3]
    assert bh._parse_json_array("{}") is None        # 对象不是数组
    assert bh._parse_json_array("") is None


# ── 真业绩门槛 ──────────────────────────────────────────────────────────────

def test_has_real_perf() -> None:
    assert bh._has_real_perf({"np_yoy": 10}) is True
    assert bh._has_real_perf({"np_yoy": -5, "forecast_level": "good"}) is True
    assert bh._has_real_perf({"np_yoy": -5, "express_yoy": 8}) is True
    assert bh._has_real_perf({"np_yoy": -5}) is False        # 亏损无前瞻 → 不算真业绩
    assert bh._has_real_perf({}) is False                    # 无任何证据


def test_perf_score() -> None:
    full = bh._perf_score(_perf(np_yoy=60, roe=25, forecast_level="good", express_yoy=10))[0]
    assert full == bh._W_PERF                                # 各项拉满后封顶 30
    assert bh._perf_score(_perf(np_yoy=0, roe=0))[0] == 0.0  # 无增长无 ROE → 0


# ── 三重硬门槛 ──────────────────────────────────────────────────────────────

def test_hard_gate() -> None:
    ok, _ = bh._hard_gate(_rec(name="兆易创新"), _perf(np_yoy=30))
    assert ok

    p, r = bh._hard_gate(_rec(name="ST华业"), _perf(np_yoy=30))
    assert not p and "ST" in r                               # ST 剔除

    p, r = bh._hard_gate(_rec(), _perf(np_yoy=-10))
    assert not p and "业绩" in r                             # 无真业绩剔除

    p, r = bh._hard_gate(_rec(main_flow_3d=-1.0), _perf(np_yoy=30))
    assert not p and "流入" in r                             # 资金未流入剔除


# ── 各维度评分 ──────────────────────────────────────────────────────────────

def test_pos_score_low_position_scores_higher() -> None:
    """越没涨（低乖离/离高点远/近期涨幅小）→ 位置分越高（与选股池风险项相反）。"""
    low = bh._pos_score(_rec(bias20=-3, dist_high=-25, change_7d=0))[0]
    high = bh._pos_score(_rec(bias20=22, dist_high=-1, change_7d=18))[0]
    assert low > high and low > 12.0 and high < 3.0


def test_flow_score() -> None:
    assert bh._flow_score(_rec(main_flow_3d=2.0))[0] == bh._W_FLOW   # 达满分线
    assert bh._flow_score(_rec(main_flow_3d=0.0))[0] == 0.0


def test_vol_score_tent() -> None:
    """帐篷函数：温和放量最高，太缩/太爆都降分。"""
    shrink = bh._vol_score(_rec(vol_ratio=0.6))[0]
    mild = bh._vol_score(_rec(vol_ratio=1.5))[0]
    blow = bh._vol_score(_rec(vol_ratio=5.0))[0]
    assert mild == bh._W_VOL and mild > shrink and mild > blow


def test_cata_score() -> None:
    hot = bh._cata_score(_ctx(heat=85, rising=True, net_flow_in=True))[0]
    cold = bh._cata_score(_ctx(heat=20, rising=False, net_flow_in=False))[0]
    assert hot == bh._W_CATA and cold == 0.0


def test_event_penalty() -> None:
    pen, flags = bh._event_penalty({
        "float": {"next_days": 10, "next_ratio": 5.0},
        "holder_trade": {"de_count": 2},
        "block": {"premium_avg": -4.0},
    })
    assert pen > 0 and len(flags) == 3
    assert bh._event_penalty({})[0] == 0.0
    # 解禁远期（>30天）不罚
    assert bh._event_penalty({"float": {"next_days": 200, "next_ratio": 5.0}})[0] == 0.0
    # 小比例解禁不罚
    assert bh._event_penalty({"float": {"next_days": 5, "next_ratio": 0.5}})[0] == 0.0


# ── 埋伏分整合（纯函数）─────────────────────────────────────────────────────

def test_score_ambush_full() -> None:
    scored = bh._score_ambush(
        _rec(name="好埋伏", bias20=-3, dist_high=-25, change_7d=0,
             main_flow_3d=1.5, vol_ratio=1.5),
        _perf(np_yoy=40, roe=18, forecast_level="good", forecast_type="预增"),
        _ctx(heat=80, rising=True, net_flow_in=True), "半导体")
    assert scored["passed"] and scored["score"] > 70
    assert set(scored["dims"]) == {"perf", "cata", "flow", "pos", "vol", "penalty"}
    assert "半导体" in scored["falsify"]                      # 证伪止损含板块名


def test_score_ambush_rejected() -> None:
    # 真业绩达标但主力未净流入 → 资金门槛剔除
    no_flow = bh._score_ambush(_rec(name="无资金票", main_flow_3d=-2.0),
                               _perf(np_yoy=30), _ctx(), "题材")
    assert not no_flow["passed"] and "流入" in no_flow["gate_reason"]
    # 亏损无前瞻 → 真业绩门槛剔除
    no_perf = bh._score_ambush(_rec(name="亏损票", main_flow_3d=2.0),
                               _perf(np_yoy=-5), _ctx(), "题材")
    assert not no_perf["passed"] and "业绩" in no_perf["gate_reason"]


# ── 催化层：受限词表约束 ────────────────────────────────────────────────────

def test_normalize_catalysts_vocab_constraint() -> None:
    vocab = {"半导体", "人工智能"}
    heat = {"半导体": {"heat": 80, "rising": True}, "人工智能": {"heat": 60, "rising": False}}
    raw = [
        {"catalyst": "大基金加码", "type": "政策",
         "related_concepts": ["半导体", "库里没有的概念"], "evidence": ["新闻A"]},
        {"catalyst": "全是库外概念", "type": "题材",
         "related_concepts": ["完全虚构板块"], "evidence": []},
    ]
    out = bh._normalize_catalysts(raw, vocab, heat)
    assert len(out) == 1                                      # 第二条全库外 → 整条丢弃
    assert [c["name"] for c in out[0]["related_concepts"]] == ["半导体"]   # 库外被过滤
    assert out[0]["rising"] is True


def test_normalize_catalyst_heat_none() -> None:
    """概念无宽表热度数据 → heat=None（前端显示「热—」而非误导的「热0」）。"""
    vocab = {"有热度概念", "无热度概念"}
    heat = {"有热度概念": {"heat": 70, "rising": False}}        # 无热度概念 不在 heat_map
    raw = [{"catalyst": "X", "type": "题材",
            "related_concepts": ["有热度概念", "无热度概念"], "evidence": []}]
    out = bh._normalize_catalysts(raw, vocab, heat, [])
    cs = {c["name"]: c for c in out[0]["related_concepts"]}
    assert cs["有热度概念"]["heat"] == 70
    assert cs["无热度概念"]["heat"] is None                     # 无数据 → None


def test_enrich_evidence() -> None:
    """LLM 引用标题回连原始新闻：精确/相似匹配带 url+来源+日期，匹配不到仅留标题。"""
    news = [
        {"title": "大基金三期加码半导体", "url": "http://a", "site": "证券时报", "date": "2026-06-18"},
        {"title": "证监会扩大科创板第五套标准适用范围至人工智能大模型", "url": "http://b", "site": "上证报", "date": "2026-06-17"},
    ]
    exact = bh._enrich_evidence(["大基金三期加码半导体"], news)[0]
    assert exact["url"] == "http://a" and exact["site"] == "证券时报" and exact["date"] == "2026-06-18"
    fuzzy = bh._enrich_evidence(["证监会扩大科创板第五套标准至人工智能大模型领域"], news)[0]
    assert fuzzy["url"] == "http://b"                          # 轻微改写仍能相似匹配
    miss = bh._enrich_evidence(["完全无关的标题XYZ123"], news)[0]
    assert miss["title"] == "完全无关的标题XYZ123" and miss["url"] == ""
    none_news = bh._enrich_evidence(["某标题"], [])[0]
    assert none_news["title"] == "某标题" and none_news["url"] == ""


# ── 催化层：注入式集成（零网络）─────────────────────────────────────────────

def test_discover_catalysts_injected() -> None:
    bh._cache_get = lambda *a, **k: None                      # type: ignore[assignment]
    bh._cache_put = lambda *a, **k: None                      # type: ignore[assignment]
    bh._concept_vocab = lambda provider, date: (              # type: ignore[assignment]
        ["半导体", "人工智能"],
        {"半导体": {"heat": 80, "rising": True, "delta": 5},
         "人工智能": {"heat": 50, "rising": False, "delta": 0}})
    bh._gather_catalyst_news = lambda provider, date: [       # type: ignore[assignment]
        {"title": "大基金三期加码半导体", "site": "证券时报", "date": "2026-06-18", "summary": ""}]

    fake = _FakeLLM('[{"catalyst":"大基金三期加码半导体设备","type":"政策",'
                    '"related_concepts":["半导体","虚构板块"],'
                    '"evidence":["大基金三期加码半导体"]}]')
    out = bh.discover_catalysts("20260618", provider=object(), client=fake)
    assert out["ok"] and len(out["catalysts"]) == 1
    assert [c["name"] for c in out["catalysts"][0]["related_concepts"]] == ["半导体"]
    assert out["catalysts"][0]["type"] == "政策" and fake.calls == 1


def test_discover_catalysts_empty_vocab() -> None:
    bh._cache_get = lambda *a, **k: None                      # type: ignore[assignment]
    bh._concept_vocab = lambda provider, date: ([], {})       # type: ignore[assignment]
    out = bh.discover_catalysts("20260618", provider=object(), client=_FakeLLM("[]"))
    assert out["ok"] is False and "为空" in out["msg"]


# ── 埋伏层：注入式集成（零网络）─────────────────────────────────────────────

def test_find_ambush_stocks_integration() -> None:
    bh._cache_get = lambda *a, **k: None                      # type: ignore[assignment]
    bh._cache_put = lambda *a, **k: None                      # type: ignore[assignment]
    bh._concept_members = lambda provider, concept: [         # type: ignore[assignment]
        "000001.SZ", "000002.SZ", "000003.SZ"]
    table = pd.DataFrame([
        {"name": "好埋伏", "close": 10, "pct_chg": 1, "bias20": -2, "dist_high": -22,
         "change_7d": 1, "main_flow_3d": 1.2, "vol_ratio": 1.4, "rps50": 40,
         "circ_mv_yi": 150, "turnover": 3},
        {"name": "已高位", "close": 50, "pct_chg": 2, "bias20": 20, "dist_high": -1,
         "change_7d": 25, "main_flow_3d": 3, "vol_ratio": 4, "rps50": 95,
         "circ_mv_yi": 300, "turnover": 10},
        {"name": "没资金", "close": 8, "pct_chg": 0, "bias20": -5, "dist_high": -30,
         "change_7d": -2, "main_flow_3d": -1, "vol_ratio": 0.7, "rps50": 20,
         "circ_mv_yi": 120, "turnover": 1},
    ], index=["000001.SZ", "000002.SZ", "000003.SZ"])
    bh._signal_table_cached = lambda date, provider: table    # type: ignore[assignment]
    bh._concept_context = lambda provider, concept, date, in_catalyst: {  # type: ignore[assignment]
        "heat": 80, "rising": True, "delta": 5, "net_flow_in": True, "in_catalyst": True}

    import app.strategy.fundamentals as F
    F.get_financials = lambda ts, provider=None: {            # type: ignore[assignment]
        "ok": True, "rows": [{"netprofit_yoy": 35, "roe": 15}],
        "forecast": {"level": "good", "type": "预增"}, "express": {}, "events": {}}

    out = bh.find_ambush_stocks("半导体", "20260618", provider=object())
    assert out["ok"]
    names = [c["name"] for c in out["candidates"]]
    assert "好埋伏" in names
    assert "没资金" not in names                              # 资金硬门槛剔除
    by = {c["name"]: c["score"] for c in out["candidates"]}
    assert by["好埋伏"] > by.get("已高位", -1)                # 低位埋伏分更高


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n✅ 全部 {len(fns)} 个测试通过")


if __name__ == "__main__":
    _run_all()
