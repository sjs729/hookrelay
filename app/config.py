"""应用配置。

设计意图：所有可变配置都从环境变量读取，本地开发时自动加载 .env 文件。
这样同一份代码在本地、CI、生产环境用不同配置运行，不需要改任何代码。
"""

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 这些是 libpq（psql / psycopg）专有的连接参数。
# asyncpg 不认识它们，会当成未知的关键字参数直接报 TypeError。
# 云平台（Neon、Supabase、Render）生成的连接串里经常带 channel_binding。
_LIBPQ_ONLY_PARAMS = frozenset(
    {
        "channel_binding",
        "gssencmode",
        "options",
        "target_session_attrs",
    }
)


def normalize_database_url(url: str) -> str:
    """把数据库连接串统一成 asyncpg 能接受的形式。

    这个问题只在跨环境时才会暴露：本地开发用 Homebrew 装的 PostgreSQL，
    连接串里没有任何 SSL 参数，一切正常；换成云数据库之后，平台给的连接串
    是 libpq 风格的（``?sslmode=require&channel_binding=require``），
    而 asyncpg 两个参数都不认识，启动时直接报错。

    做两件事：

    1. **驱动前缀归一**。``postgres://`` 和 ``postgresql://`` 默认指向同步驱动，
       而本项目的引擎是异步的，必须显式写成 ``postgresql+asyncpg://``。
    2. **参数名翻译**。``sslmode`` 改名为 asyncpg 使用的 ``ssl``；
       两者的取值恰好同名（disable / allow / prefer / require /
       verify-ca / verify-full），所以改个名字即可。
       其余 libpq 专有参数一律剔除。

    没有 SSL 参数时（本地开发）原样返回，不会引入任何变化。
    """
    if not url:
        return url

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql+asyncpg"

    cleaned: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered == "sslmode":
            cleaned.append(("ssl", value))
        elif lowered in _LIBPQ_ONLY_PARAMS:
            continue
        else:
            cleaned.append((key, value))

    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(cleaned), parts.fragment))


class Settings(BaseSettings):
    """环境变量配置。

    字段名小写，对应同名的大写环境变量（pydantic-settings 默认大小写不敏感）。
    例如字段 database_url 读取环境变量 DATABASE_URL。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 基础 ----------
    env: str = "dev"
    log_level: str = "INFO"
    # 是否输出 JSON 结构化日志。本地开发用可读文本，
    # 部署到平台后在环境变量里设为 true，便于日志系统按字段检索
    log_json: bool = False
    # 仅用于本地开发的占位值，生产环境必须通过环境变量覆盖
    secret_key: str = "dev-insecure-key-please-change"  # noqa: S105

    # ---------- 数据库 ----------
    database_url: str = "postgresql+asyncpg://localhost:5432/hookrelay_dev"
    test_database_url: str = "postgresql+asyncpg://localhost:5432/hookrelay_test"

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _normalize_database_urls(cls, value: str) -> str:
        """在配置层就把连接串规范化。

        放在这里而不是各个建引擎的地方：应用、Alembic、Worker 都从
        settings 读同一个值，在这一处转换能保证它们看到的是同一个地址。
        散在多处转换就容易出现「迁移连的库和应用连的库不同」这类问题。
        """
        return normalize_database_url(value)

    # ---------- Worker 投递引擎 ----------
    worker_concurrency: int = 10
    worker_batch_size: int = 50
    worker_poll_interval_seconds: float = 1.0
    worker_lock_timeout_seconds: int = 120
    # Worker 进程自己的指标端口。prometheus_client 的默认 registry 是进程内的，
    # 投递指标只存在于 Worker 进程，Web 的 /metrics 里看不到，
    # 所以 Worker 必须另开一个端口供采集器抓取
    worker_metrics_port: int = 9101

    # ---------- 入站接收 ----------
    ingest_rate_limit_per_minute: int = 120
    max_ingest_body_bytes: int = 1024 * 1024
    signature_tolerance_seconds: int = 300

    # ---------- 默认投递策略 ----------
    default_max_attempts: int = 5
    default_timeout_seconds: int = 10
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: int = 3600
    retry_jitter_ratio: float = 0.2

    @property
    def is_production(self) -> bool:
        """是否为生产环境。用于区分开放式调试行为与安全默认值。"""
        return self.env.lower() in {"prod", "production"}


@lru_cache
def get_settings() -> Settings:
    """返回配置单例。

    用 lru_cache 保证环境变量只解析一次，避免每次请求都读一遍 .env 文件。
    """
    return Settings()
