"""形态/信号库包：导入即注册全部信号（个股回测、因子选股共用 PATTERN_REGISTRY）。"""

from . import ma_signals, price_volume  # noqa: F401  触发全部形态/信号注册
