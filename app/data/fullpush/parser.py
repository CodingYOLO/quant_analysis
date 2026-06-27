"""沪深全推 L1 报文解析（纯函数·零网络·可单测）。

全推 TCP 每条报文 = 4字节小端长度前缀 + UTF-8 正文；正文按 `$` 分隔 36 字段。
本模块只负责【字段解析 + 代码格式转换】，不碰网络/状态，便于单元测试。
字段顺序见 https://www.mushuju.com/L1_qtapi.html（已用真实报文实测校验）。
"""

from __future__ import annotations

# 36 字段下标（0-based）。来源：幕数据 L1 全推文档 + 真实报文实测。
_I_CODE, _I_NAME, _I_TS = 0, 1, 2
_I_OPEN, _I_HIGH, _I_LOW, _I_LAST = 3, 4, 5, 6
_I_VOL, _I_AMOUNT = 7, 8
_I_ASK_PX, _I_ASK_VOL = 9, 14          # 卖一~五价 9..13 / 卖一~五量 14..18
_I_BID_PX, _I_BID_VOL = 19, 24         # 买一~五价 19..23 / 买一~五量 24..28
_I_TURNOVER, _I_PREV_CLOSE = 29, 30
_I_LIMIT_UP, _I_LIMIT_DOWN = 31, 32
_I_VOL_RATIO, _I_INNER, _I_OUTER = 33, 34, 35
_FIELD_COUNT = 36

_MARKETS = ("SH", "SZ", "BJ")


def to_ts_code(mushuju_code: str) -> str:
    """`SH600000` → `600000.SH`；无法识别返回原值。"""
    c = mushuju_code.strip().upper()
    if len(c) > 2 and c[:2] in _MARKETS:
        return f"{c[2:]}.{c[:2]}"
    return mushuju_code


def to_mushuju_code(ts_code: str) -> str:
    """`600000.SH` → `SH600000`；无后缀返回原值。"""
    if "." in ts_code:
        num, mkt = ts_code.split(".", 1)
        return f"{mkt.upper()}{num}"
    return ts_code


def _f(x: str) -> float:
    """宽松转 float，失败返回 0.0（行情字段缺失常见）。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_record(line: str) -> dict | None:
    """单条全推报文 → 归一化行情 dict；字段不足返回 None。

    返回键与 DataProvider.get_realtime_quote 对齐（ts_code/name/price/pct_chg/
    open/high/low/prev_close/amount），并附带全推独有字段（五档/换手/量比/内外盘）。
    """
    f = line.split("$")
    if len(f) < _FIELD_COUNT:
        return None
    prev, last = _f(f[_I_PREV_CLOSE]), _f(f[_I_LAST])
    pct = round((last / prev - 1) * 100, 2) if prev > 0 else 0.0
    return {
        "ts_code": to_ts_code(f[_I_CODE]),
        "name": f[_I_NAME],
        "price": last,
        "pct_chg": pct,
        "open": _f(f[_I_OPEN]),
        "high": _f(f[_I_HIGH]),
        "low": _f(f[_I_LOW]),
        "prev_close": prev,
        "vol": _f(f[_I_VOL]),
        "amount": _f(f[_I_AMOUNT]),
        "turnover_rate": _f(f[_I_TURNOVER]),
        "limit_up": _f(f[_I_LIMIT_UP]),
        "limit_down": _f(f[_I_LIMIT_DOWN]),
        "vol_ratio": _f(f[_I_VOL_RATIO]),
        "inner": _f(f[_I_INNER]),
        "outer": _f(f[_I_OUTER]),
        "ask_px": [_f(f[_I_ASK_PX + i]) for i in range(5)],
        "ask_vol": [_f(f[_I_ASK_VOL + i]) for i in range(5)],
        "bid_px": [_f(f[_I_BID_PX + i]) for i in range(5)],
        "bid_vol": [_f(f[_I_BID_VOL + i]) for i in range(5)],
        "ts": int(_f(f[_I_TS])),
    }


def parse_message(payload: str) -> list[dict]:
    """一条 TCP 报文可能含多只（`#` 分隔）→ 解析为行情列表（跳过非法段）。"""
    out: list[dict] = []
    for seg in payload.split("#"):
        seg = seg.strip()
        if seg:
            q = parse_record(seg)
            if q is not None:
                out.append(q)
    return out
