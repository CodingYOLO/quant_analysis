"""IND 行业领先指标适配器：Tushare 期货主力连续日线（fut_daily）。

口径（2026-08-03 逐一实测 21/21 可得·见 registry _IND 段）：
- value = 主力连续**收盘价**（close·缺失退 settle）——真实拼接价，分位/走势所见即所得；
- 换月跳空不在此处理：异动侧由 compute 的 V3 连续2日确认天然防御（跳空是单日毛刺），
  显示侧由 leadind 服务按 fut_mapping 识别换月日、用新主力自身 pre_close 重算当日涨跌幅
  （实测 20260720 CU 换月日主连 pre_close=103370=CU2609 自身昨收·换月感知✓）；
- 一次 range 调用取一个品种全窗口（867行/次实测单次取全·无分页）。
"""

from __future__ import annotations

import logging

import pandas as pd

from app.macro.adapters.base import Point, to_ymd

logger = logging.getLogger(__name__)

# metric code → Tushare 主力连续代码（唯一真源：与 registry._IND 一一对应·单测校验一致性）
FUT_CODES: dict[str, str] = {
    "fut_lh": "LH.DCE", "fut_c": "C.DCE",
    "fut_rb": "RB.SHF", "fut_i": "I.DCE", "fut_fg": "FG.ZCE", "fut_sa": "SA.ZCE",
    "fut_lc": "LC.GFE",
    "fut_cu": "CU.SHF", "fut_al": "AL.SHF", "fut_au": "AU.SHF", "fut_ag": "AG.SHF",
    "fut_sc": "SC.INE", "fut_ur": "UR.ZCE", "fut_v": "V.DCE", "fut_sp": "SP.SHF",
    "fut_ec": "EC.INE",
    "fut_sr": "SR.ZCE", "fut_p": "P.DCE", "fut_m": "M.DCE", "fut_cf": "CF.ZCE",
    "fut_ap": "AP.ZCE",
}


def _pro():
    from app.data.composite_provider import CompositeProvider
    return CompositeProvider()._ts._api


class FuturesMainAdapter:
    """21个品种逐个 range 拉取（每品种1次调用·夜间增量默认回看75日≈50行/次）。"""

    name = "tushare:fut_daily"
    codes = tuple(FUT_CODES)

    def fetch(self, start: str, end: str) -> list[Point]:
        from app.data.cache import rate_limited_call
        pro = _pro()
        pts: list[Point] = []
        for code, ts_code in FUT_CODES.items():
            try:
                df = rate_limited_call("tushare_fut_daily", pro.fut_daily,
                                       ts_code=ts_code, start_date=start, end_date=end)
            except Exception as e:
                # 单品种失败不拖垮整批：该品种当日走 NULL+告警路径，其余照常
                logger.warning("[IND] %s(%s) 取数失败: %s", code, ts_code, e)
                continue
            if df is None or df.empty:
                continue
            df = df.copy()
            close = pd.to_numeric(df.get("close"), errors="coerce")
            settle = pd.to_numeric(df.get("settle"), errors="coerce")
            df["_v"] = close.fillna(settle)          # 收盘缺失退结算价(极少·停板日等)
            for _, r in df.iterrows():
                if pd.isna(r["_v"]):
                    continue
                pts.append(Point(code=code, as_of=to_ymd(r["trade_date"]),
                                 value=float(r["_v"]), source=self.name))
        return pts
