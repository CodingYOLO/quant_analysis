"""实时盯盘 主攻研判(build_live_mainline) + 机会雷达(build_opportunity_radar) 纯函数测试。

关键：
  - 主攻研判：冲高回落的涌入板块(看着强其实在撤)应判退潮·不当主攻；
  - 机会雷达：个股×板块共振加权(多信号+加速共振最强)·放量下跌(出货)剔除·急拉move不当今日涨幅。

运行：.venv/bin/python tests/test_live_mainline_radar.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy.realtime_fund import build_live_mainline, build_opportunity_radar  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


_SECTORS = [
    {"industry": "创新药", "net_yi": 18.5, "avg_pct": 3.2, "leader": "恒瑞医药",
     "leader_pct": 5.8, "traj_label": "加速流入", "traj_tone": "up"},
    {"industry": "军工电子", "net_yi": 12.3, "avg_pct": 2.1, "leader": "中航光电",
     "leader_pct": 4.2, "traj_label": "拐头流入", "traj_tone": "up"},
    {"industry": "游戏", "net_yi": 8.0, "avg_pct": 1.0, "leader": "恺英网络",
     "leader_pct": 2.0, "traj_label": "冲高回落", "traj_tone": "warn"},
]
_OUT = [{"industry": "半导体", "net_yi": -15.4}, {"industry": "消费电子", "net_yi": -9.6}]


def test_mainline_lead_and_ebb():
    m = build_live_mainline(_SECTORS, _OUT, {"state": "震荡"})
    _assert(m["lead"]["industry"] == "创新药", f"主攻应为创新药，实得 {m['lead']}")
    _assert(m["second"]["industry"] == "军工电子", f"次攻应为军工电子，实得 {m['second']}")
    ebb_names = [e["industry"] for e in m["ebb"]]
    _assert("游戏" in ebb_names, "游戏(冲高回落·净买+8亿看着强)应判退潮/避雷·不当主攻")
    _assert(m["lead"]["industry"] != "游戏" and m["second"]["industry"] != "游戏",
            "冲高回落板块不应进主攻/次攻")


def test_radar_resonance_ranking_and_filter():
    fr = [{"ts_code": "300750.SZ", "name": "宁德时代", "pct_chg": 4.0, "net_yi": 8.5, "industry": "电池"},
          {"ts_code": "600276.SH", "name": "恒瑞医药", "pct_chg": 5.8, "net_yi": 12.0, "industry": "创新药"}]
    sg = [{"ts_code": "600276.SH", "name": "恒瑞医药", "move": 3.2, "industry": "创新药"},
          {"ts_code": "000001.SZ", "name": "平安银行", "move": 2.0, "industry": "银行"}]
    vs = [{"ts_code": "600276.SH", "name": "恒瑞医药", "pct_chg": 5.8, "vol_ratio": 3.5, "industry": "创新药"},
          {"ts_code": "002415.SZ", "name": "海康威视", "pct_chg": -1.2, "vol_ratio": 4.0, "industry": "安防"}]
    sec = [{"industry": "创新药", "net_yi": 18.5, "traj_tone": "up", "traj_label": "加速流入"},
           {"industry": "电池", "net_yi": 5.0, "traj_tone": "neu", "traj_label": "平稳流入"}]
    radar = build_opportunity_radar(fr, sg, vs, sec)
    names = [r["name"] for r in radar]
    _assert(radar[0]["name"] == "恒瑞医药", f"三信号+加速共振应排第一，实得 {names}")
    _assert(radar[0]["strength"] == 9, f"恒瑞强度应为9(3信号×2+加速共振3)，实得 {radar[0]['strength']}")
    _assert(radar[0]["pct_chg"] == 5.8, "今日涨幅应为5.8%(非急拉move 3.2)")
    _assert("海康威视" not in names, "放量下跌(出货)应被剔除")
    _assert(radar[0]["resonance"] and radar[0]["resonance"]["tone"] == "up", "恒瑞应标板块共振·加速")


def _run():
    tests = [test_mainline_lead_and_ebb, test_radar_resonance_ranking_and_filter]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n✅ 全部 {len(tests)} 项通过")


if __name__ == "__main__":
    _run()
