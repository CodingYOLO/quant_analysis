"""IND ENSO 适配器：NOAA CPC ONI（Oceanic Niño Index·境外源）。

服务器实测（2026-08-04·腾讯云生产直连·纪律：本地测通不算数）：
- https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt → HTTP 200·23KB·2.7s；
- 919 行 = 1 表头(SEAS YR TOTAL ANOM) + 918 数据行·全部恰 4 列·无缺失值标记；
- Last-Modified: 2026-08-03 18:35 GMT → 最新一行 MJJ 2026(中心月6月)·
  距中心月月末(6/30)约 34 天——CPC 惯例每月初更新上一个完整三月窗。

口径：
- value = ANOM 列（Niño3.4 海区三月滑动平均海温距平·℃）——ENSO 官方判据：
  ≥+0.5℃ 连续 5 个滑动季=厄尔尼诺，≤-0.5℃=拉尼娜；
- SEAS 为三月滑动窗（DJF=上年12月-1月-2月），YR 是**中心月所在年**
  （NDJ 1950 = 1950.11-1950.12-1951.1·中心月 1950.12）→ as_of=中心月月末，
  与其他月频指标同构，配合 metric_meta.lag_days 保证 point-in-time；
- 近几个滑动季的数值会随 ERSST 数据集更新小幅修订——每晚重取全文件、
  upsert 覆盖写，修订被自然吸收，无需特殊处理。
"""

from __future__ import annotations

import logging

from app.macro.adapters.base import Point, month_end

logger = logging.getLogger(__name__)

_ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"

# 三月滑动窗标签 → 中心月（YR 即中心月所在年，见模块 docstring）
_SEASON_CENTER_MONTH: dict[str, int] = {
    "DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6,
    "JJA": 7, "JAS": 8, "ASO": 9, "SON": 10, "OND": 11, "NDJ": 12,
}


class OniAdapter:
    """单次 GET 取全历史文本（1950 至今·23KB），解析后按 [start, end] 过滤。"""

    name = "noaa:oni"
    codes = ("enso_oni",)
    # 月频且发布滞后约 36 自然日：sync 默认回看 75 天时单日增量只剩约 8 天余量，
    # CPC 发布顺延一周就会取空——放宽到 120 天（全文件一次取回·窗口加宽零成本）
    lookback_days = 120

    def fetch(self, start: str, end: str) -> list[Point]:
        pts: list[Point] = []
        for line in self._download().splitlines():
            p = self._parse_line(line)
            if p is not None and start <= p.as_of <= end:
                pts.append(p)
        return pts

    def _download(self) -> str:
        """失败直接抛给 sync 层（NULL+告警+45天结转兜底），不在此静默重试。"""
        import httpx
        resp = httpx.get(_ONI_URL, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    def _parse_line(self, line: str) -> Point | None:
        """一行 → Point；表头/格式异常行返回 None（实测全文件无缺失标记，防御仍保留）。"""
        parts = line.split()
        if len(parts) != 4 or parts[0] not in _SEASON_CENTER_MONTH:
            return None
        season, yr, _total, anom = parts
        try:
            year, value = int(yr), float(anom)
        except ValueError:
            logger.warning("[macro] %s 数值解析失败·跳过行: %r", self.name, line)
            return None
        if not 1900 <= year <= 2100:
            logger.warning("[macro] %s 年份越界·跳过行: %r", self.name, line)
            return None
        as_of = month_end(f"{year}{_SEASON_CENTER_MONTH[season]:02d}")
        return Point(code=self.codes[0], as_of=as_of, value=value, source=self.name)
