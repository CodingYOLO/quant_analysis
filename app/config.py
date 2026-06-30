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
    deepseek_flash_model: str = Field("deepseek-v4-flash", description="高频低价值任务模型(V4快速)")
    deepseek_pro_model: str = Field("deepseek-v4-pro", description="低频高价值任务模型(V4推理,质量最强)")

    # ---------- Claude（可选）----------
    claude_api_key: str = Field("", description="Claude API Key（可选备用）")
    claude_model: str = Field("claude-sonnet-4-6")

    # ---------- LLM Provider ----------
    llm_provider: str = Field("deepseek", description="默认LLM提供商: deepseek | claude")

    # ---------- 推送渠道 ----------
    notify_channel: str = Field("serverchan", description="推送渠道: serverchan | email | none")
    serverchan_send_key: str = Field("", description="Server酱 SendKey")
    bark_key: str = Field("", description="用户1(我)的 Bark key；盯盘实时提醒用。可逗号分隔我自己的多台设备")
    bark_key_user2: str = Field("", description="用户2(爸爸)的 Bark key；可逗号分隔多设备。"
                               "其自选/持仓个性化信号只推他；全市场信号两台都收。留空=未接入(全归用户1)")
    # 幕数据沪深全推 L1 实时行情（TCP长连接·仅交易时间·token仅.env勿入库）
    fullpush_host: str = Field("", description="全推TCP host，生产 qt2.chagubang.com")
    fullpush_port: int = Field(0, description="全推TCP端口，生产 8379")
    fullpush_token: str = Field("", description="全推授权token，仅 .env 配置，禁止入库")
    fullpush_demo: bool = Field(False, description="演示开关：True 时接公开测试端点(回放数据)，休市预览/测试用；周一开盘前自动切回生产")
    # Web报告地址（手机简报末尾附链接，本地留空，部署后填写）
    web_base_url: str = Field("", description="Web报告访问地址，如 http://your-server:8000")
    # Web 登录认证（公网部署必填，留空则不鉴权）
    web_username: str = Field("admin", description="Web登录用户名")
    web_password: str = Field("", description="Web登录密码，公网部署务必设置")

    # 邮箱配置（163: host=smtp.163.com port=465 use_ssl=true）
    email_smtp_host: str = Field("smtp.163.com", description="SMTP服务器，163用smtp.163.com")
    email_smtp_port: int = Field(465, description="163/QQ用465(SSL)，Gmail用587(STARTTLS)")
    email_use_ssl: bool = Field(True, description="465端口用True，587端口用False")
    email_user: str = Field("", description="163邮箱地址，如 xxx@163.com")
    email_password: str = Field("", description="163授权码（不是登录密码，在163设置-POP3/SMTP里生成）")
    email_to: str = Field("")

    # ---------- 财联社（可选，填入Cookie后自动启用高质量新闻）----------
    cls_cookie: str = Field("", description="财联社登录Cookie（从Chrome开发者工具复制）")

    # ---------- 博查 Bocha 联网搜索（可选，填入后行业详情启用实时网络检索）----------
    bocha_api_key: str = Field("", description="博查 Web Search API Key（https://open.bochaai.com 注册），留空则不启用联网搜索")
    bocha_freshness: str = Field("oneWeek", description="搜索时效：oneDay/oneWeek/oneMonth/oneYear/noLimit")

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
