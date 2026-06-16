"""factors 包：核心技术因子(core) + 主题宽表因子(breadth_qfq/theme_wide)。
保持向后兼容：原 app/factors.py 的函数经 core 重新导出。"""
from app.factors.core import *  # noqa: F401,F403
