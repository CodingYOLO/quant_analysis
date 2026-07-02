"""
人气榜反转选股：曾火 → 洗盘冷落 → 人气拐头回升 的埋伏候选。

对标吴川 / 稳智AI「人气榜买入法」，但补其硬伤 + 取长补短：
  - 数据核心：东财「个股人气排名」轨迹（峰值 / 谷值 / 当前 / 回升）。
  - 稳智纯靠人气排名；本模块**叠加我们自有的关键位/企稳做双确认**（人气拐头 + 现价回踩到入局区间）。
  - 阈值全部 params 可调（默认取稳智公开值，**待回测校准·非真理**）。

设计：纯函数、无 IO、可回测。产出「关注候选池」，
**只做筛选与分档，不预测涨跌、不打分胜率、不构成买卖建议**（铁律）。
人气 = 散户关注度（自选/浏览/搜索），**非资金、非龙虎榜真钱**——口径要诚实。
"""

from __future__ import annotations

# 默认阈值（稳智公开口径·待用我们自己的回测校准，勿当真理）
DEFAULTS: dict = {
    "float_min_yi": 80.0,   # 流通市值下限(亿)·流动性门槛
    "peak_max": 100,        # 峰值须≤此(近期真进过 Top100·证明曾是关注焦点)
    "trough_lo": 300,       # 谷值下限(洗盘够冷)
    "trough_hi": 800,       # 谷值上限(别太冷/没人接力)
    "recover_min": 10,      # 回升下限(确认拐头·排除还在跌)
    "recover_fast": 200,    # 回升上限(排除回升太快·追高·埋伏点已过)
    "best_recover": 50,     # 回升≥此=最佳候选(趋势明确)·否则=稳健(刚拐头)
    "require_tech": True,   # 双确认：现价须回踩到入局区间(关键位 state in/watch)
}


def screen_hot_reversal(trajectories: list[dict], levels_map: dict | None = None,
                        params: dict | None = None) -> list[dict]:
    """对人气轨迹列表筛选反转候选。

    trajectories: [{code, name, cur_rank, peak_rank, trough_rank, float_mv_yi, ...}]
    levels_map: {code: build_key_levels(...) 输出}，用于技术面双确认（可空）。
    params: 覆盖 DEFAULTS 的阈值。
    返回：通过筛选的候选（含分档 tier / 技术态 / 依据 reasons），best 优先、回升多者靠前。
    """
    p = {**DEFAULTS, **(params or {})}
    out = [c for t in (trajectories or []) if (c := _evaluate(t, levels_map, p))]
    out.sort(key=lambda x: (0 if x["tier"] == "best" else 1, -x["recover"]))
    return out


def _evaluate(t: dict, levels_map: dict | None, p: dict) -> dict | None:
    """单只票过筛。全部条件命中才返回候选 dict，否则 None。"""
    peak, trough, cur = _i(t.get("peak_rank")), _i(t.get("trough_rank")), _i(t.get("cur_rank"))
    if None in (peak, trough, cur):
        return None
    fmv = _f(t.get("float_mv_yi"))
    recover = trough - cur                              # 回升 = 谷值 − 当前(名次降=回升)
    if fmv is not None and fmv < p["float_min_yi"]:
        return None
    if peak > p["peak_max"]:                            # 没真火过(峰值都进不了Top100)
        return None
    if not (p["trough_lo"] <= trough <= p["trough_hi"]):  # 冷落程度不在洗盘窗口
        return None
    if recover < p["recover_min"] or recover > p["recover_fast"]:  # 没拐头 / 回升太快
        return None
    tech = _tech_state(t.get("code"), levels_map)
    if p["require_tech"] and tech["state"] not in ("in", "watch"):
        return None
    return _pack(t, peak, trough, cur, recover, fmv, tech, p)


def _pack(t, peak, trough, cur, recover, fmv, tech, p) -> dict:
    """组装候选载荷：字段 + 分档 + 可溯源依据。"""
    reasons = [f"峰值#{peak}(近期进过Top100)", f"谷值#{trough}(洗盘冷落)",
               f"回升↑{recover}位(人气拐头)"]
    if fmv is not None:
        reasons.append(f"流通{fmv:.0f}亿")
    if tech["state"] != "na":
        reasons.append(f"技术：{tech['label']}")
    return {
        "code": t.get("code"), "name": t.get("name"),
        "cur_rank": cur, "peak_rank": peak, "trough_rank": trough, "recover": recover,
        "float_mv_yi": round(fmv) if fmv is not None else None,
        "tier": "best" if recover >= p["best_recover"] else "steady",
        "tech_state": tech["state"], "tech_label": tech["label"], "entry_zone": tech.get("zone"),
        "signal": t.get("signal") or "", "reasons": reasons,
    }


def _tech_state(code: str | None, levels_map: dict | None) -> dict:
    """查该票关键位的现价相对位置（双确认用）。无关键位→state='na'。"""
    lv = (levels_map or {}).get(code) if code else None
    if not lv or not lv.get("position"):
        return {"state": "na", "label": "无关键位数据", "zone": None}
    pos = lv["position"]
    return {"state": pos.get("state", "na"), "label": pos.get("label", ""),
            "zone": lv.get("entry_zone")}


# ── 编排（有 IO）：取轨迹 → 补流通市值 → 预筛 → 仅候选算关键位双确认 ──────────
def run_screen(provider, kind: str = "rank", days: int = 14,
               params: dict | None = None) -> dict:
    """完整流程（对上层暴露）。轨迹取自 hot_rank_log（自建+详情API 共用聚合）。

    先廉价预筛(人气+流通)得到少量候选，再**只对候选算关键位**做双确认，控计算量。
    返回 {ok, n_traj, candidates, best, steady, as_of, note}。
    """
    from app.strategy import db
    p = {**DEFAULTS, **(params or {})}
    trajs = db.hot_rank_trajectory(kind, days)
    if not trajs:
        return {"ok": True, "n_traj": 0, "candidates": [], "best": [], "steady": [],
                "note": "暂无人气轨迹——需先积累每日快照或跑家用详情API同步脚本"}
    _enrich_float_mv(provider, trajs)
    pre = screen_hot_reversal(trajs, None, {**p, "require_tech": False})   # 预筛(不含技术面)
    levels_map = _levels_for(provider, [c["code"] for c in pre]) if p["require_tech"] else {}
    final = screen_hot_reversal(trajs, levels_map, p)
    return {"ok": True, "n_traj": len(trajs), "candidates": final,
            "best": [c for c in final if c["tier"] == "best"],
            "steady": [c for c in final if c["tier"] == "steady"],
            "n_pre": len(pre), "params": p}


def _enrich_float_mv(provider, trajs: list[dict]) -> None:
    """给轨迹补流通市值(亿)：一次全市场 daily_basic·circ_mv(万元)/1e4。原地写 float_mv_yi。"""
    import datetime

    import pandas as pd

    from app.factors.breadth_qfq import _recent_trade_dates
    today = datetime.date.today().strftime("%Y%m%d")
    fm: dict[str, float] = {}
    for d in reversed(_recent_trade_dates(provider, today, 3)):   # 今日盘中 basic 未出→回退最近交易日
        try:
            dbf = provider.get_daily_basic(d)
        except Exception:
            dbf = None
        if dbf is not None and not dbf.empty:
            for ts, cmv in zip(dbf["ts_code"], pd.to_numeric(dbf["circ_mv"], errors="coerce")):
                code = str(ts).split(".")[0]
                if code and cmv == cmv:
                    fm[code] = round(float(cmv) / 1e4, 1)
            break
    for t in trajs:
        if t.get("float_mv_yi") is None:
            t["float_mv_yi"] = fm.get(str(t.get("code") or "").zfill(6))


def _levels_for(provider, codes: list[str]) -> dict:
    """仅对候选算关键位（复用 tech_chain._zone_for·按交易日缓存）。{code: levels|None}。"""
    from app.strategy.tech_chain import _zone_for
    out = {}
    for c in codes:
        try:
            out[c] = _zone_for(provider, c)
        except Exception:
            out[c] = None
    return out


# 家用【详情API同步脚本】：在家电脑跑·拉每票东财人气历史名次→推服务器(即时算峰值/谷值/回升)
# 东财封云IP·家住宅IP直连。首次运行会打印一只票的返回样例供核验列名(schema)。
DETAIL_SYNC_SCRIPT = r'''# hotrank_detail_sync.py —— 在你【家里电脑】跑·拉个股人气排名历史→推服务器
# 用途：为「人气榜反转选股」提供峰值/谷值/回升轨迹(能到300-800名·非Top100不够)。
# 需先：pip install akshare requests   用法：python hotrank_detail_sync.py
import akshare as ak, requests, base64, time, datetime

SERVER = "http://123.207.223.176:8000"
AUTH = "Basic " + base64.b64encode(b"admin:Astock@2026").decode()
DAYS = 20            # 每票保留最近多少个交易日的名次(≥14 够算2周窗口)
SLEEP = 0.8          # 每票间隔(秒)·别太快免被限流

def to_symbol(code):
    c = str(code).zfill(6)
    if c[0] in ("6", "9") or c[:3] in ("688", "689"): return "SH" + c
    if c[0] in ("8", "4") or c[:3] == "920": return "BJ" + c
    return "SZ" + c

def pick_cols(df):
    date_c = next((x for x in df.columns if x in ("时间", "日期", "date", "trade_date")), None)
    rank_c = next((x for x in df.columns if x in ("排名", "个股排名", "rank", "当前排名")), None)
    return date_c, rank_c

def fetch_one(code, name, printed):
    df = ak.stock_hot_rank_detail_em(symbol=to_symbol(code))
    if df is None or df.empty: return []
    if not printed[0]:
        printed[0] = True
        print("【首次样例·请核对列名】", list(df.columns)); print(df.tail(3).to_string())
    dc, rc = pick_cols(df)
    if not dc or not rc:
        print("!! 未识别 时间/排名 列·请把上面样例发给助手调整"); return []
    daily = {}                                     # 同一天多条→取当天最后一条(日粒度)
    for _, r in df.iterrows():
        d = str(r[dc])[:10].replace("-", "")
        try: daily[d] = int(r[rc])
        except Exception: pass
    days = sorted(daily)[-DAYS:]
    return [{"code": str(code).zfill(6), "name": name, "trade_date": d, "rank": daily[d]} for d in days]

def main():
    codes = requests.get(SERVER + "/api/hotrank/universe",
                         headers={"Authorization": AUTH}, timeout=30).json().get("codes", [])
    print("待扫描 %d 只(自选+产业链龙头)" % len(codes))
    rows, printed, fail = [], [False], 0
    for i, code in enumerate(codes, 1):
        try:
            rows += fetch_one(code, "", printed)
        except Exception as e:
            fail += 1
            if fail <= 5: print("  %s 失败: %s" % (code, e))
        if i % 50 == 0: print("  进度 %d/%d · 已收 %d 行" % (i, len(codes), len(rows)))
        time.sleep(SLEEP)
        if len(rows) >= 1500:                      # 分批推·防单次过大
            requests.post(SERVER + "/api/hotrank/history/ingest", json={"kind": "rank", "rows": rows},
                          headers={"Authorization": AUTH}, timeout=40); rows = []
    if rows:
        requests.post(SERVER + "/api/hotrank/history/ingest", json={"kind": "rank", "rows": rows},
                      headers={"Authorization": AUTH}, timeout=40)
    print("完成 · 失败 %d 只 · 现在去 /hotpicks 看候选池" % fail)

if __name__ == "__main__":
    main()
'''


def _i(v):
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None
