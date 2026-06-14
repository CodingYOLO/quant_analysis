"""
系统配置，通过 pydantic-settings 从 .env 文件读取。
所有模块通过 get_settings() 获取配置，禁止直接读取环境变量或硬编码。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- Tushare ----------
    tushare_token: str = Field(..., description="Tushare Pro API Token")

    # ---------- DeepSeek ----------
    deepseek_api_key: str = Field(..., description="DeepSeek API Key")
    deepseek_flash_model: str = Field("deepseek-chat", description="高频低价值任务模型")
    deepseek_pro_model: str = Field("deepseek-reasoner", description="低频高价值任务模型")

    # ---------- Claude（可选）----------
    claude_api_key: str = Field("", description="Claude API Key（可选备用）")
    claude_model: str = Field("claude-sonnet-4-6")

    # ---------- LLM Provider ----------
    llm_provider: str = Field("deepseek", description="默认LLM提供商: deepseek | claude")

    # ---------- 推送渠道 ----------
    notify_channel: str = Field("serverchan", description="推送渠道: serverchan | email | none")
    serverchan_send_key: str = Field("", description="Server酱 SendKey")

    # 邮箱配置
    email_smtp_host: str = Field("smtp.gmail.com")
    email_smtp_port: int = Field(587)
    email_user: str = Field("")
    email_password: str = Field("")
    email_to: str = Field("")

    # ---------- 财联社（可选，填入Cookie后自动启用高质量新闻）----------
    cls_cookie: str = Field("", description="财联社登录Cookie（从Chrome开发者工具复制）")

    # ---------- 路径 ----------
    cache_dir: Path = Field(Path("data_cache"))
    report_dir: Path = Field(Path("reports"))

    # ---------- 选股参数 ----------
    max_candidates: int = Field(10, description="最大候选股数量")
    min_market_cap: float = Field(20.0, description="市值下限（亿元）")
    max_market_cap: float = Field(500.0, description="市值上限（亿元）")

    def ensure_dirs(self) -> None:
        """确保缓存和报告目录存在。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回全局单例配置，首次调用时从 .env 加载。"""
    settings = Settings()
    settings.ensure_dirs()
    _patch_no_proxy()
    return settings


def _patch_no_proxy() -> None:
    """
    将 A股数据源的国内域名加入 NO_PROXY，使 akshare/requests 直连而不走代理。
    这样本地挂 VPN 也不影响数据拉取；部署到国内服务器时无副作用。
    """
    import os

    # requests 库的 NO_PROXY 不支持通配符，写根域名即可自动匹配所有子域名
    _CN_DOMAINS = ",".join([
        "eastmoney.com",
        "10jqka.com.cn",
        "sina.com.cn",
        "sinajs.cn",
        "gtimg.cn",
        "tushare.pro",
        "tushare.org",
        "akshare.xyz",
        "cninfo.com.cn",
        "cls.cn",
        "localhost",
        "127.0.0.1",
    ])

    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        if existing:
            os.environ[key] = existing + "," + _CN_DOMAINS
        else:
            os.environ[key] = _CN_DOMAINS
