"""主力净流入的**唯一规范口径**（所有模块统一从这里取，杜绝口径分裂）。

主力净流入 = (超大单买入额 − 超大单卖出额) + (大单买入额 − 大单卖出额)
           = 超大单净 + 大单净，即东财 / 同花顺"主力"口径（大单 + 超大单）。

⚠️ **禁止**再用 Tushare 的 `net_mf_amount` 字段当"主力净流入"：
   它是 Tushare 另一套自有口径，实测全市场约 **50%** 个股与本口径**符号相反**
   （例：雅克科技 002409 连续多日，本口径流出而 net_mf_amount 流入），
   会与用户在东财/同花顺个股页看到的方向矛盾，导致误导。详见资金口径核查记录。

Tushare moneyflow 的 `*_amount` 字段单位均为**万元**，本函数返回值单位亦为**万元**，
调用方按需 `/1e4`(→亿) 或 `*1e4`(→元)。
"""

from __future__ import annotations

import pandas as pd

# 主力净流入所需的四个原始金额列（万元）
MAIN_FLOW_COLS = ("buy_elg_amount", "sell_elg_amount", "buy_lg_amount", "sell_lg_amount")


def has_main_flow_cols(mf: pd.DataFrame | None) -> bool:
    """moneyflow 表是否具备计算主力净流入所需的四个原始列。"""
    return (mf is not None and not mf.empty
            and all(c in mf.columns for c in MAIN_FLOW_COLS))


def main_net_wan(mf: pd.DataFrame | None) -> pd.Series:
    """主力净流入（万元）·index=ts_code = 超大单净 + 大单净（买 − 卖）。

    缺列 / 空表 → 返回空 Series。返回值行序与入参一致（index=ts_code），
    可 `.items()` 遍历或 `.to_numpy()` 按位置回填列。
    """
    if not has_main_flow_cols(mf):
        return pd.Series(dtype=float)
    n = pd.to_numeric
    v = ((n(mf["buy_elg_amount"], errors="coerce") - n(mf["sell_elg_amount"], errors="coerce"))
         + (n(mf["buy_lg_amount"], errors="coerce") - n(mf["sell_lg_amount"], errors="coerce")))
    return pd.Series(v.to_numpy(), index=mf["ts_code"].astype(str))
