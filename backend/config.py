import os
from copy import deepcopy
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from backend.config_store import load_effective_runtime_snapshot
from backend.utils.logger import setup_logger


logger = setup_logger("Config")


class Config:
    DEFAULT_LEVERAGE = 20
    DEFAULT_RECVWINDOW = 60000

    timezone = "Asia/Shanghai"
    trading_mode = "REAL"
    leverage = 20
    admin_password = "123456"
    enable_scheduler = True
    global_binance_api_key = None
    global_binance_secret = None
    global_okx_api_key = None
    global_okx_secret = None
    global_okx_passphrase = None
    langchain_tracing = False
    langchain_api_key = ""
    langchain_project = "crypto-agent"
    llm_timeout_seconds = 120.0
    llm_max_retries = 2
    global_summarizer_model = ""
    global_summarizer_api_base = ""
    global_summarizer_api_key = ""
    symbol_configs: List[Dict] = []
    configs_by_id: Dict[str, Dict] = {}
    source = "env"

    def __init__(self):
        self.reload_config()

    def _load_bootstrap_settings(self) -> None:
        load_dotenv(override=False)
        self.timezone = os.getenv("TIMEZONE", "Asia/Shanghai")
        self.admin_password = os.getenv("ADMIN_PASSWORD", "123456")

    def _normalize_symbol_configs(self, configs: List[Dict]) -> List[Dict]:
        normalized: list[dict] = []
        config_ids: set[str] = set()

        for index, raw_config in enumerate(configs or []):
            config = deepcopy(raw_config or {})
            if "config_id" not in config or not str(config.get("config_id") or "").strip():
                symbol = config.get("symbol", "unknown").replace("/", "-").lower()
                model = str(config.get("model", "default")).split("-")[0]
                config["config_id"] = f"{symbol}-{model}-{index}"
                logger.warning(f"Config {index} missing config_id, auto-generated {config['config_id']}")

            config_id = str(config["config_id"]).strip()
            if config_id in config_ids:
                raise ValueError(f"Duplicate config_id: {config_id}")
            config_ids.add(config_id)
            config["config_id"] = config_id
            normalized.append(config)

        return normalized

    def _apply_snapshot(self, snapshot: dict) -> None:
        self._load_bootstrap_settings()

        self.global_binance_api_key = snapshot.get("global_binance_api_key")
        self.global_binance_secret = snapshot.get("global_binance_secret")
        self.global_okx_api_key = snapshot.get("global_okx_api_key")
        self.global_okx_secret = snapshot.get("global_okx_secret")
        self.global_okx_passphrase = snapshot.get("global_okx_passphrase")
        self.langchain_api_key = snapshot.get("langchain_api_key", "")
        self.global_summarizer_api_key = snapshot.get("global_summarizer_api_key", "")

        self.enable_scheduler = bool(snapshot.get("enable_scheduler", True))
        self.leverage = int(snapshot.get("leverage", self.DEFAULT_LEVERAGE))
        self.langchain_tracing = bool(snapshot.get("langchain_tracing", False))
        self.langchain_project = snapshot.get("langchain_project", "crypto-agent")
        self.llm_timeout_seconds = float(snapshot.get("llm_timeout_seconds", 120.0))
        self.llm_max_retries = int(snapshot.get("llm_max_retries", 2))
        self.trading_mode = snapshot.get("trading_mode", "REAL")
        self.global_summarizer_model = snapshot.get("global_summarizer_model", "")
        self.global_summarizer_api_base = snapshot.get("global_summarizer_api_base", "")
        self.source = snapshot.get("source", "env")

        self.symbol_configs = self._normalize_symbol_configs(snapshot.get("agents", []))
        self.configs_by_id = {cfg["config_id"]: cfg for cfg in self.symbol_configs}
        self._validate_config()
        logger.info(
            f"Runtime configuration loaded from {self.source} with {len(self.symbol_configs)} agent config(s)"
        )

    def _validate_config(self) -> None:
        errors = []
        has_binance = bool(self.global_binance_api_key and self.global_binance_secret)
        has_okx = bool(self.global_okx_api_key and self.global_okx_secret and self.global_okx_passphrase)

        if not has_binance and not has_okx:
            for cfg in self.symbol_configs:
                symbol = cfg.get("symbol")
                exchange = str(cfg.get("exchange", "binance")).lower()

                if exchange == "okx":
                    if not cfg.get("api_key") or not cfg.get("secret") or not cfg.get("passphrase"):
                        errors.append(f"{symbol} (OKX) is missing API credentials and no global OKX key is set")
                else:
                    key = cfg.get("binance_api_key") or cfg.get("api_key")
                    secret = cfg.get("binance_secret") or cfg.get("secret")
                    if not key or not secret:
                        errors.append(f"{symbol} (Binance) is missing API credentials and no global Binance key is set")

        if errors:
            message = "Configuration validation failed:\n" + "\n".join(errors)
            logger.error(message)
            raise ValueError(message)

    def get_exchange_credentials(
        self,
        config_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        config = None
        if config_id:
            config = self.configs_by_id.get(config_id)
        elif symbol:
            for item in self.symbol_configs:
                if item.get("symbol") == symbol:
                    config = item
                    break

        if config:
            exchange = str(config.get("exchange", "binance")).lower()
            passphrase = config.get("passphrase")

            if exchange == "binance":
                api_key = config.get("binance_api_key") or config.get("api_key")
                secret = config.get("binance_secret") or config.get("secret")
            else:
                api_key = config.get("api_key")
                secret = config.get("secret")

            if api_key and secret:
                if exchange == "okx" and not passphrase:
                    passphrase = self.global_okx_passphrase
                return exchange, api_key, secret, passphrase

            if exchange == "okx":
                return "okx", self.global_okx_api_key, self.global_okx_secret, self.global_okx_passphrase
            return "binance", self.global_binance_api_key, self.global_binance_secret, None

        return "binance", self.global_binance_api_key, self.global_binance_secret, None

    def get_binance_credentials(
        self,
        config_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        exchange, key, secret, _ = self.get_exchange_credentials(config_id, symbol)
        if exchange == "binance":
            return key, secret
        return None, None

    def get_config_by_id(self, config_id: str) -> Optional[Dict]:
        return self.configs_by_id.get(config_id)

    def get_symbol_config(self, symbol: str) -> Optional[Dict]:
        for config in self.symbol_configs:
            if config.get("symbol") == symbol:
                logger.warning("symbol-based config lookup is deprecated; prefer config_id")
                return config
        return None

    def get_configs_by_symbol(self, symbol: str) -> List[Dict]:
        return [cfg for cfg in self.symbol_configs if cfg.get("symbol") == symbol]

    def get_leverage(self, config_id: Optional[str] = None) -> int:
        if config_id:
            config = self.configs_by_id.get(config_id)
            if config and "leverage" in config and config.get("leverage") is not None:
                return int(config.get("leverage"))
        return int(self.leverage)

    def get_all_symbol_configs(self) -> List[Dict]:
        return list(self.symbol_configs)

    def reload_config(self):
        snapshot = load_effective_runtime_snapshot()
        self._apply_snapshot(snapshot)


config = Config()
