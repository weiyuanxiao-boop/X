import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()

CONFIG_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 4936
    log_dir: str = "logs"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def log_dir_path(self) -> Path:
        return CONFIG_DIR / self.log_dir


class ModelConfig:
    def __init__(self):
        config_path = CONFIG_DIR / "model_config.yaml"
        aliases_path = CONFIG_DIR / "aliases.yaml"
        
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        self._models = self._config.get("models", {})
        self._default = self._config.get("default_model", "")
        self._auth = self._parse_auth(self._config.get("auth"))
        self._limits = self._parse_limits(self._config.get("limits"))
        self._upstream = self._parse_upstream(self._config.get("upstream"))
        
        # Load aliases from separate file
        if aliases_path.exists():
            with open(aliases_path, "r", encoding="utf-8") as f:
                self._aliases = yaml.safe_load(f) or {}
        else:
            self._aliases = {}

    @staticmethod
    def _parse_auth(raw) -> dict:
        """解析 auth 配置。

        keys 支持两种写法：
            keys: ["sk-a", "sk-b"]              -> 名字为 key1 / key2
            keys: {team-a: "sk-a", ci: "sk-b"}  -> 名字用于日志 auth.principal

        **默认 enabled=True**（安全优先）。keys 为空时非本地请求一律拒绝（fail closed），
        本地回环地址仍放行。需要临时关闭时显式写 enabled: false。
        """
        if not isinstance(raw, dict):
            raw = {}

        keys: dict[str, str] = {}
        keys_raw = raw.get("keys") or []
        if isinstance(keys_raw, dict):
            for name, value in keys_raw.items():
                if value:
                    keys[str(name)] = str(value)
        elif isinstance(keys_raw, list):
            for index, value in enumerate(keys_raw, 1):
                if value:
                    keys[f"key{index}"] = str(value)

        enabled = raw.get("enabled")
        if enabled is None:
            enabled = True

        return {"enabled": bool(enabled), "keys": keys}

    @staticmethod
    def _parse_limits(raw) -> dict:
        """解析资源限制。0 表示不限制（max_body_mb / max_logged_body_mb 除外）。"""
        if not isinstance(raw, dict):
            raw = {}

        def _int(name: str, default: int) -> int:
            try:
                value = int(raw.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value >= 0 else default

        return {
            "max_body_mb": _int("max_body_mb", 32),
            "rate_limit_per_minute": _int("rate_limit_per_minute", 0),
            "max_logged_body_mb": _int("max_logged_body_mb", 8),
        }

    @staticmethod
    def _parse_upstream(raw) -> dict:
        """解析上游连接配置。

        trust_env 默认 False：不读取 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 等环境变量，
        避免 API key 与对话内容被环境中的代理截获。需要走代理时必须显式配置 proxy。
        """
        if not isinstance(raw, dict):
            raw = {}

        trust_env = raw.get("trust_env")
        proxy = raw.get("proxy")
        return {
            "trust_env": False if trust_env is None else bool(trust_env),
            "proxy": str(proxy) if proxy else None,
        }

    def get_auth_config(self) -> dict:
        return self._auth

    def get_limits(self) -> dict:
        return self._limits

    def get_upstream_config(self) -> dict:
        return self._upstream
    
    def save_aliases(self):
        """Save aliases to aliases.yaml file.

        使用 yaml 序列化而不是字符串拼接：alias / target 中的换行、冒号、
        引号等会被正确转义，避免写坏文件或注入额外 YAML 结构。
        """
        aliases_path = CONFIG_DIR / "aliases.yaml"
        body = yaml.safe_dump(
            self._aliases,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        with open(aliases_path, "w", encoding="utf-8") as f:
            f.write("# Model Aliases Configuration\n")
            f.write("# Format: alias_name: target_model\n\n")
            f.write(body)

    def _resolve(self, model_name: str) -> str:
        return self._aliases.get(model_name, model_name)

    def get_model(self, model_name: str) -> dict | None:
        name = self._resolve(model_name)
        if name in self._models:
            return self._models[name]
        if self._default:
            return self._models.get(self._default)
        return None

    def list_models(self) -> list[str]:
        return list(self._models.keys()) + list(self._aliases.keys())

    def get_upstream_info(self, model_name: str, downstream_format: str = None) -> dict:
        """Get upstream model info, preferring format that matches downstream to avoid conversion.

        Args:
            model_name: The model name to look up
            downstream_format: The format of the downstream request ('openai' or 'anthropic')
                              If provided, prefer the matching upstream format.
                              If the requested format is not available, raise ValueError.

        Returns:
            dict with provider, upstream_model, api_key, base_url, etc.
        
        Raises:
            ValueError: If the model doesn't support the requested downstream_format
        """
        cfg = self.get_model(model_name)
        if not cfg:
            raise ValueError(f"Unknown model: {model_name}")

        api_key = os.environ.get(cfg["api_key_env"], "")
        if not api_key:
            raise ValueError(f"API key not set for model {model_name} (env: {cfg['api_key_env']})")

        # Handle new base_url structure with openai/anthropic sub-keys
        base_url_cfg = cfg.get("base_url", {})

        # Determine which format to use
        if isinstance(base_url_cfg, dict):
            # New format: base_url has 'openai' and/or 'anthropic' sub-keys
            openai_url = base_url_cfg.get("openai")
            anthropic_url = base_url_cfg.get("anthropic")

            # Prefer format that matches downstream
            if downstream_format == "openai":
                if openai_url:
                    provider = "openai"
                    base_url = openai_url
                else:
                    raise ValueError(f"Model '{model_name}' does not support OpenAI API format (only supports: {'anthropic' if anthropic_url else 'none'})")
            elif downstream_format == "anthropic":
                if anthropic_url:
                    provider = "anthropic"
                    base_url = anthropic_url
                else:
                    raise ValueError(f"Model '{model_name}' does not support Anthropic/Claude API format (only supports: {'openai' if openai_url else 'none'})")
            # No downstream_format specified: use whatever is available
            elif openai_url:
                provider = "openai"
                base_url = openai_url
            elif anthropic_url:
                provider = "anthropic"
                base_url = anthropic_url
            else:
                raise ValueError(f"No base_url configured for model {model_name}")
        else:
            # Legacy format: base_url is a string, need 'provider' field
            base_url = base_url_cfg
            provider = cfg.get("provider")
            if not provider:
                raise ValueError(f"No provider specified for model {model_name}")
            # legacy 格式同样要校验协议：否则会用错误的鉴权头去打上游，
            # 下游只能收到上游的 401/404，看不出真正原因
            if downstream_format and provider != downstream_format:
                raise ValueError(
                    f"Model '{model_name}' only supports {provider} API format "
                    f"(downstream requested {downstream_format})"
                )

        return {
            "provider": provider,
            "upstream_model": cfg["upstream_model"],
            "api_key": api_key,
            "base_url": base_url,
            "api_version": cfg.get("api_version", ""),
            "reasoning_effort": cfg.get("reasoning_effort"),
        }


def setup_logger(name: str) -> logging.Logger:
    """Setup logger with file and console handlers.
    
    All loggers write to a single app.log file.
    """
    settings = get_settings()
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # Ensure log directory exists
    settings.log_dir_path.mkdir(parents=True, exist_ok=True)

    # Single file handler for all loggers
    log_file = settings.log_dir_path / "app.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # Formatter with logger name
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Avoid duplicate handlers
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_model_config() -> ModelConfig:
    return ModelConfig()


@lru_cache
def get_logger(name: str = "app") -> logging.Logger:
    return setup_logger(name)
