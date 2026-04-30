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
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        self._models = self._config.get("models", {})
        self._default = self._config.get("default_model", "")
        self._aliases = self._config.get("aliases", {})

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
