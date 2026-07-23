import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
"""
配置规则：默认值  < TOML配置文件(~/.manius/config.toml)  < ./.env 文件  < 系统环境变量
后续可添加 CLI参数配置
"""

class ConfigError(ValueError):
    pass

class LogConfig(BaseModel):
    level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    format: str = Field(default="text", pattern="^(text|json)$")
    file: Path | None = None          # None = 输出stdout（终端）
    file_rotation: bool = True        # 是否开启日志轮转
    max_size_mb: int = Field(default=10, ge=1)
    backup_count: int = Field(default=5, ge=0)


class LlmConfig(BaseModel):
    api_key: str | None = None
    default_model: str = "claude-sonnet-4-20250514"
    default_base_url: str | None = None


class TavilyConfig(BaseModel):
    api_key: str | None = None


class TraceConfig(BaseModel):
    enabled: bool = True
    file: Path = Field(default_factory=lambda: Path.home() / ".manius" / "traces" / "daemon.jsonl")
    max_queue_size: int = Field(default=10_000, ge=1)
    max_size_mb: int = Field(default=10, ge=1)
    backup_count: int = Field(default=5, ge=0)


class ManiusConfig(BaseModel):
    # IPC服务基础
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=7437, ge=1, le=65535)
    workspace: Path = Field(default_factory=Path.cwd)
    
    # 日志配置
    log: LogConfig = Field(default_factory=LogConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    max_steps: int = Field(default=20, ge=1)


_CONFIG_KEYS = set(ManiusConfig.model_fields)


# 读取简单的 KEY=VALUE .env 文件并返回其配置项。
def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ConfigError(f"Invalid .env line in {path}: {line}")
        values[key.strip()] = value.strip().strip("\"'")
    return values


# 将带 MANIUS_ 前缀的环境变量映射为内部配置键。
def _environment_values(values: dict[str, str]) -> dict[str, object]:
    prefix = "MANIUS_"
    mapped: dict[str, object] = {}
    llm_values: dict[str, str] = {}
    tavily_values: dict[str, str] = {}
    trace_values: dict[str, str] = {}
    for key, value in values.items():
        uppercase_key = key.upper()
        if uppercase_key == "ANTHROPIC_API_KEY":
            llm_values["api_key"] = value
        elif uppercase_key == "TAVILY_API_KEY":
            tavily_values["api_key"] = value
        elif uppercase_key.startswith(prefix):
            config_key = key[len(prefix) :].lower()
            if config_key.startswith("llm_"):
                llm_values[config_key.removeprefix("llm_")] = value
            elif config_key.startswith("tavily_"):
                tavily_values[config_key.removeprefix("tavily_")] = value
            elif config_key.startswith("trace_"):
                trace_values[config_key.removeprefix("trace_")] = value
            else:
                mapped[config_key] = value
    if llm_values:
        mapped["llm"] = llm_values
    if tavily_values:
        mapped["tavily"] = tavily_values
    if trace_values:
        mapped["trace"] = trace_values
    return mapped


# 读取 TOML 配置文件，并拒绝未声明的配置键。
def _read_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with path.open("rb") as file:
        values = tomllib.load(file)
    unknown = set(values) - _CONFIG_KEYS
    if unknown:
        raise ConfigError(f"Unknown config keys in {path}: {', '.join(sorted(unknown))}")
    return values


# 按默认值、配置文件、.env 和环境变量的优先级加载配置。
def load_config(cwd: Path | None = None, environ: dict[str, str] | None = None) -> ManiusConfig:
    working_directory = cwd or Path.cwd()
    environment = dict(environ if environ is not None else os.environ)
    configured_path = _environment_values(environment).get("config")
    config_path = Path(configured_path).expanduser() if configured_path else Path.home() / ".manius" / "config.toml"
    merged: dict[str, object] = {}
    merged.update(_read_toml(config_path))
    merged.update(_environment_values(_read_dotenv(working_directory / ".env")))
    merged.update(_environment_values(environment))
    try:
        config = ManiusConfig.model_validate(merged)
    except ValidationError as error:
        raise ConfigError(str(error)) from error
    workspace = config.workspace.expanduser()
    config.workspace = workspace.resolve() if workspace.is_absolute() else (working_directory / workspace).resolve()
    return config
