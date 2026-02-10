"""
统一配置管理模块
支持交易对单独配置币安API密钥
"""
import os
import json
from typing import Optional, Tuple, List, Dict
from dotenv import load_dotenv
from utils.logger import setup_logger

logger = setup_logger("Config")


class Config:
    """统一配置管理类"""

    # 系统常量
    DEFAULT_LEVERAGE = 20
    DEFAULT_RECVWINDOW = 60000

    def __init__(self):
        """初始化配置管理器"""
        load_dotenv()
        self._load_global_config()
        self._load_symbol_configs()
        self._validate_config()

    def _load_global_config(self):
        """加载全局配置"""
        # 币安API配置
        self.global_binance_api_key = os.getenv('BINANCE_API_KEY')
        self.global_binance_secret = os.getenv('BINANCE_SECRET')

        # 系统配置
        self.admin_password = os.getenv('ADMIN_PASSWORD', '123456')
        self.enable_scheduler = os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true'
        self.leverage = int(os.getenv('LEVERAGE', self.DEFAULT_LEVERAGE))

        # LangChain配置
        self.langchain_tracing = os.getenv('LANGCHAIN_TRACING_V2', 'false').lower() == 'true'
        self.langchain_api_key = os.getenv('LANGCHAIN_API_KEY', '')
        self.langchain_project = os.getenv('LANGCHAIN_PROJECT', 'crypto-agent')

        # 交易模式
        self.trading_mode = os.getenv('TRADING_MODE', 'REAL')

        logger.info("✅ 全局配置加载完成")

    def _load_symbol_configs(self):
        """加载交易对配置"""
        configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
        try:
            self.symbol_configs = json.loads(configs_str)
            logger.info(f"✅ 交易对配置加载完成，共 {len(self.symbol_configs)} 个配置")
        except json.JSONDecodeError as e:
            logger.error(f"❌ 解析SYMBOL_CONFIGS失败: {e}")
            self.symbol_configs = []

    def _validate_config(self):
        """验证配置完整性"""
        errors = []

        # 检查是否至少有一个有效的币安API配置
        has_global_api = bool(self.global_binance_api_key and self.global_binance_secret)

        if not has_global_api:
            # 如果没有全局配置，检查是否所有交易对都有专属配置
            for cfg in self.symbol_configs:
                symbol = cfg.get('symbol')
                if not cfg.get('binance_api_key') or not cfg.get('binance_secret'):
                    errors.append(f"交易对 {symbol} 缺少币安API配置，且未配置全局API")

        if errors:
            error_msg = "配置验证失败:\n" + "\n".join(errors)
            logger.error(f"❌ {error_msg}")
            raise ValueError(error_msg)

        logger.info("✅ 配置验证通过")

    def get_binance_credentials(self, symbol: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        获取币安API凭证
        优先级：交易对专属配置 > 全局默认配置

        Args:
            symbol: 交易对符号，如 "BTC/USDT"

        Returns:
            (api_key, secret) 元组
        """
        if symbol:
            # 查找该交易对的专属配置
            for config in self.symbol_configs:
                if config.get('symbol') == symbol:
                    api_key = config.get('binance_api_key')
                    secret = config.get('binance_secret')
                    if api_key and secret:
                        logger.debug(f"使用交易对 {symbol} 的专属币安API配置")
                        return (api_key, secret)

        # 返回全局默认配置
        if symbol:
            logger.debug(f"交易对 {symbol} 使用全局币安API配置")
        else:
            logger.debug("使用全局币安API配置")

        return (self.global_binance_api_key, self.global_binance_secret)

    def get_symbol_config(self, symbol: str) -> Optional[Dict]:
        """
        获取指定交易对的完整配置

        Args:
            symbol: 交易对符号

        Returns:
            配置字典，如果不存在则返回None
        """
        for config in self.symbol_configs:
            if config.get('symbol') == symbol:
                return config
        return None

    def get_all_symbol_configs(self) -> List[Dict]:
        """
        获取所有交易对配置

        Returns:
            配置列表
        """
        return self.symbol_configs

    def reload_config(self):
        """重新加载配置（无需重启服务）"""
        logger.info("🔄 重新加载配置...")
        load_dotenv(override=True)
        self._load_global_config()
        self._load_symbol_configs()
        self._validate_config()
        logger.info("✅ 配置重新加载完成")


# 全局配置实例
config = Config()
