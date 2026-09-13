"""配置层的测试。

重点在数据库连接串的规范化：这是本地开发完全测不出问题、
一换到云数据库就必然踩到的地方，值得单独固定住行为。
"""

import pytest

from app.config import Settings, normalize_database_url


class TestNormalizeDatabaseUrl:
    """连接串规范化的行为固定。"""

    def test_local_url_is_unchanged(self) -> None:
        """本地开发用的连接串必须原样通过。

        这个函数只在部署时才发挥作用，如果它顺手改了本地地址，
        开发环境的故障会很难归因——因为本地从来没要求过 SSL。
        """
        local = "postgresql+asyncpg://localhost:5432/hookrelay_dev"
        assert normalize_database_url(local) == local

    def test_bare_postgres_scheme_gets_async_driver(self) -> None:
        """postgres:// 前缀要补上异步驱动名。

        云平台的控制台给的通常是 postgresql://，它默认指向同步驱动。
        本项目整条栈是异步的，驱动名不对会以"缺少同步依赖"的形式报错，
        错误信息不会提示真正的原因。
        """
        assert (
            normalize_database_url("postgres://u:p@host:5432/db")
            == "postgresql+asyncpg://u:p@host:5432/db"
        )
        assert (
            normalize_database_url("postgresql://u:p@host:5432/db")
            == "postgresql+asyncpg://u:p@host:5432/db"
        )

    def test_already_async_url_keeps_its_scheme(self) -> None:
        """已经是异步驱动的连接串不该被重复改写。"""
        url = "postgresql+asyncpg://u:p@host:5432/db"
        assert normalize_database_url(url).startswith("postgresql+asyncpg://")

    @pytest.mark.parametrize(
        ("sslmode", "expected"),
        [
            ("require", "require"),
            ("verify-ca", "verify-ca"),
            ("verify-full", "verify-full"),
            ("disable", "disable"),
            ("prefer", "prefer"),
        ],
    )
    def test_sslmode_is_renamed_to_ssl(self, sslmode: str, expected: str) -> None:
        """sslmode 要改名为 ssl，取值保持不变。

        asyncpg 用 ssl 参数、libpq 用 sslmode，两者取值同名，
        所以只需要换名字。取值本身不能被改动或丢失，
        否则会从"要求加密"悄悄退化成"不加密"。
        """
        result = normalize_database_url(f"postgresql://u:p@host:5432/db?sslmode={sslmode}")
        assert f"ssl={expected}" in result
        assert "sslmode" not in result

    def test_libpq_only_params_are_dropped(self) -> None:
        """libpq 专有参数要剔除，否则 asyncpg 直接报未知参数。"""
        url = (
            "postgresql://u:p@host:5432/db"
            "?sslmode=require&channel_binding=require&target_session_attrs=read-write"
        )
        result = normalize_database_url(url)
        assert "channel_binding" not in result
        assert "target_session_attrs" not in result
        assert "ssl=require" in result

    def test_unrelated_params_survive(self) -> None:
        """不认识的参数应保留而不是一律丢弃。

        连接串上可能挂着应用自己关心的参数，或者今后才会支持的驱动选项。
        白名单式地"只放过已知参数"会让这些参数静默消失。
        """
        result = normalize_database_url(
            "postgresql://u:p@host:5432/db?sslmode=require&application_name=hookrelay"
        )
        assert "application_name=hookrelay" in result
        assert "ssl=require" in result

    def test_neon_style_url_is_usable(self) -> None:
        """完整走一遍 Neon 控制台给出的连接串格式。"""
        neon = (
            "postgresql://neondb_owner:npg_abc123@ep-cool-name-123456"
            ".ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
        )
        result = normalize_database_url(neon)
        assert result.startswith("postgresql+asyncpg://neondb_owner:npg_abc123@")
        assert "ssl=require" in result
        assert "channel_binding" not in result
        # 主机名与库名不能被破坏，否则连不上
        assert "ap-southeast-1.aws.neon.tech/neondb" in result

    def test_empty_url_is_returned_as_is(self) -> None:
        """空值直接返回，不要把空串变成带参数的怪东西。"""
        assert normalize_database_url("") == ""


class TestSettingsValidation:
    """Settings 读取环境变量时也会走规范化。"""

    def test_settings_normalizes_env_value(self) -> None:
        """从环境变量读到的连接串必须是规范化之后的。

        应用、Alembic、Worker 都从 settings 取地址，
        在这一层统一转换才能保证它们连的是同一个库。
        """
        settings = Settings(
            database_url="postgresql://u:p@host:5432/db?sslmode=require",
            _env_file=None,  # 绕开 .env，避免本地配置干扰断言
        )
        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert "ssl=require" in settings.database_url

    def test_settings_local_default_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认值（本地开发地址）不该被规范化改动。

        要显式清掉环境变量：conftest 接在导入应用之前就设了 DATABASE_URL
        指向测试库，不清掉的话这里断言到的是测试库地址。
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        settings = Settings(_env_file=None)
        assert settings.database_url == "postgresql+asyncpg://localhost:5432/hookrelay_dev"

    def test_production_flag(self) -> None:
        """生产开关只在 prod 系列取值下打开。"""
        assert Settings(env="prod", _env_file=None).is_production is True
        assert Settings(env="production", _env_file=None).is_production is True
        assert Settings(env="dev", _env_file=None).is_production is False
        assert Settings(env="test", _env_file=None).is_production is False
