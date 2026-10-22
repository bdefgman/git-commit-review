"""配置加载与校验：读取 <repo>/.ai-review/config.toml，缺失项用默认值补齐。"""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.11 以下用 tomli 补齐
    import tomli as tomllib

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_RELATIVE_PATH = ".ai-review/config.toml"

DEFAULT_INCLUDE = [
    "*.java",
    "*.kt",
    "*.py",
    "*.ts",
    "*.tsx",
    "*.js",
    "*.vue",
    "*.go",
    "*.xml",
    "*.yml",
    "*.yaml",
]

DEFAULT_EXCLUDE = [
    "**/target/**",
    "**/build/**",
    "**/dist/**",
    "**/out/**",
    "**/node_modules/**",
    "**/generated/**",
    "**/__generated__/**",
    "*.lock",
    "*.min.js",
    "*.min.css",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
]

_POLICY_VALUES = ("warn", "block")
_LARGE_DIFF_VALUES = ("new_files_only", "split", "skip")
# 配置文件中禁止出现的明文凭据键名
_FORBIDDEN_KEYS = {"api_key", "apikey", "secret", "password", "token"}


class ConfigError(Exception):
    """配置内容不合法。"""


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.0
    max_output_tokens: int = 1200
    timeout_seconds: float = 20.0
    retries: int = 1
    proxy: Optional[str] = None


@dataclass(frozen=True)
class ConventionsConfig:
    sources: List[str] = field(default_factory=list)
    on_unavailable: str = "warn"
    max_tokens: int = 4000


@dataclass(frozen=True)
class ReviewConfig:
    include: List[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    max_diff_lines: int = 400
    on_large_diff: str = "new_files_only"
    context_lines: int = 3


@dataclass(frozen=True)
class PolicyConfig:
    on_api_error: str = "warn"
    on_invalid_json: str = "warn"
    min_confidence: float = 0.6


@dataclass(frozen=True)
class ReportConfig:
    show_snippet: bool = True
    color: bool = True


@dataclass(frozen=True)
class IgnoreConfig:
    inline_directive: str = "ai-review-ignore"
    file: str = ".ai-review/ignore.toml"


@dataclass(frozen=True)
class Config:
    repo_root: Path
    config_path: Path
    model: ModelConfig = field(default_factory=ModelConfig)
    conventions: ConventionsConfig = field(default_factory=ConventionsConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    ignore: IgnoreConfig = field(default_factory=IgnoreConfig)


def load_config(repo_root: Path) -> Config:
    path = repo_root / CONFIG_RELATIVE_PATH
    raw = _read_toml(path)
    _reject_plain_credentials(raw)

    model_raw = _section(raw, "model")
    conventions_raw = _section(raw, "conventions")
    review_raw = _section(raw, "review")
    policy_raw = _section(raw, "policy")
    report_raw = _section(raw, "report")
    ignore_raw = _section(raw, "ignore")

    model = ModelConfig(
        base_url=_str(model_raw, "base_url", ModelConfig.base_url),
        model=_str(model_raw, "model", ModelConfig.model),
        api_key_env=_str(model_raw, "api_key_env", ModelConfig.api_key_env),
        temperature=_float(model_raw, "temperature", ModelConfig.temperature),
        max_output_tokens=_int(model_raw, "max_output_tokens", ModelConfig.max_output_tokens),
        timeout_seconds=_float(model_raw, "timeout_seconds", ModelConfig.timeout_seconds),
        retries=_int(model_raw, "retries", ModelConfig.retries),
        proxy=_opt_str(model_raw, "proxy"),
    )
    conventions = ConventionsConfig(
        sources=_str_list(conventions_raw, "sources", []),
        on_unavailable=_enum(conventions_raw, "on_unavailable", "warn", _POLICY_VALUES),
        max_tokens=_int(conventions_raw, "max_tokens", ConventionsConfig.max_tokens),
    )
    review = ReviewConfig(
        include=_str_list(review_raw, "include", list(DEFAULT_INCLUDE)),
        exclude=_str_list(review_raw, "exclude", list(DEFAULT_EXCLUDE)),
        max_diff_lines=_int(review_raw, "max_diff_lines", ReviewConfig.max_diff_lines),
        on_large_diff=_enum(review_raw, "on_large_diff", ReviewConfig.on_large_diff, _LARGE_DIFF_VALUES),
        context_lines=_int(review_raw, "context_lines", ReviewConfig.context_lines),
    )
    policy = PolicyConfig(
        on_api_error=_enum(policy_raw, "on_api_error", PolicyConfig.on_api_error, _POLICY_VALUES),
        on_invalid_json=_enum(policy_raw, "on_invalid_json", PolicyConfig.on_invalid_json, _POLICY_VALUES),
        min_confidence=_float(policy_raw, "min_confidence", PolicyConfig.min_confidence),
    )
    report = ReportConfig(
        show_snippet=_bool(report_raw, "show_snippet", ReportConfig.show_snippet),
        color=_bool(report_raw, "color", ReportConfig.color),
    )
    ignore = IgnoreConfig(
        inline_directive=_str(ignore_raw, "inline_directive", IgnoreConfig.inline_directive),
        file=_str(ignore_raw, "file", IgnoreConfig.file),
    )

    if not 0.0 <= policy.min_confidence <= 1.0:
        raise ConfigError("[policy] min_confidence 必须在 0 到 1 之间")

    return Config(
        repo_root=repo_root,
        config_path=path,
        model=model,
        conventions=conventions,
        review=review,
        policy=policy,
        report=report,
        ignore=ignore,
    )


def _read_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} 解析失败: {exc}") from exc


def _reject_plain_credentials(raw: Dict[str, Any], prefix: str = "") -> None:
    for key, value in raw.items():
        if isinstance(value, dict):
            _reject_plain_credentials(value, f"{prefix}{key}.")
            continue
        if key.lower() in _FORBIDDEN_KEYS:
            raise ConfigError(
                f"配置里不允许出现明文凭据 {prefix}{key}，请改为通过环境变量注入（如 api_key_env）"
            )


def _section(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必须是表结构")
    return value


def _str(section: Dict[str, Any], key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 必须是非空字符串")
    return value.strip()


def _opt_str(section: Dict[str, Any], key: str) -> Optional[str]:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    return value.strip() or None


def _int(section: Dict[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    return value


def _float(section: Dict[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} 必须是数字")
    return float(value)


def _bool(section: Dict[str, Any], key: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是布尔值")
    return value


def _enum(section: Dict[str, Any], key: str, default: str, allowed: tuple) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(f"{key} 只能是 {' / '.join(allowed)}")
    return value


def _str_list(section: Dict[str, Any], key: str, default: List[str]) -> List[str]:
    value = section.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} 必须是字符串数组")
    return [item.strip() for item in value if item.strip()]
