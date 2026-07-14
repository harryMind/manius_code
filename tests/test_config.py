from pathlib import Path

from manius_code.core.config import load_config


# 功能：验证环境变量会覆盖 .env、TOML 和内置默认配置。
# 设计：为每个配置层提供不同值，以单次加载同时验证四层优先级顺序。
def test_load_config_uses_documented_precedence(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "10.0.0.1"\nport = 7000\n', encoding="utf-8")
    (tmp_path / ".env").write_text("MANIUS_HOST=10.0.0.2\nMANIUS_PORT=7001\n", encoding="utf-8")
    config = load_config(
        tmp_path,
        {"MANIUS_CONFIG": str(config_path), "MANIUS_HOST": "10.0.0.3"},
    )
    assert config.host == "10.0.0.3"
    assert config.port == 7001
