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

            # 验证和处理 config_id
            config_ids = set()
            for i, config in enumerate(self.symbol_configs):
                # 如果没有 config_id，自动生成
                if 'config_id' not in config:
                    symbol = config.get('symbol', 'unknown').replace('/', '-').lower()
                    model = config.get('model', 'default').split('-')[0]
                    config['config_id'] = f"{symbol}-{model}-{i}"
                    logger.warning(f"⚠️ 配置 {i} 缺少 config_id，自动生成: {config['config_id']}")

                # 检查 config_id 唯一性
                config_id = config['config_id']
                if config_id in config_ids:
                    raise ValueError(f"配置ID重复: {config_id}")
                config_ids.add(config_id)

            # 使用字典存储，以 config_id 为键，方便快速查询
            self.configs_by_id = {cfg['config_id']: cfg for cfg in self.symbol_configs}

            logger.info(f"✅ 交易对配置加载完成，共 {len(self.symbol_configs)} 个配置")
        except json.JSONDecodeError as e:
            logger.error(f"❌ 解析SYMBOL_CONFIGS失败: {e}")
            self.symbol_configs = []
            self.configs_by_id = {}

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

    def get_binance_credentials(self, config_id: Optional[str] = None, symbol: Optional[str] = None) -> Tuple[
        Optional[str], Optional[str]]:
        """
        获取币安API凭证
        优先级：config_id专属配置 > symbol专属配置 > 全局默认配置

        Args:
            config_id: 配置ID（推荐使用）
            symbol: 交易对符号（向后兼容，不推荐）

        Returns:
            (api_key, secret) 元组
        """
        # 优先使用 config_id 查询
        if config_id:
            config = self.configs_by_id.get(config_id)
            if config:
                api_key = config.get('binance_api_key')
                secret = config.get('binance_secret')
                if api_key and secret:
                    logger.debug(f"使用配置 {config_id} 的专属币安API")
                    return (api_key, secret)

        # 向后兼容：使用 symbol 查询（返回第一个匹配）
        if symbol:
            for config in self.symbol_configs:
                if config.get('symbol') == symbol:
                    api_key = config.get('binance_api_key')
                    secret = config.get('binance_secret')
                    if api_key and secret:
                        logger.debug(f"使用交易对 {symbol} 的专属币安API配置")
                        return (api_key, secret)

        # 返回全局默认配置
        logger.debug("使用全局币安API配置")
        return (self.global_binance_api_key, self.global_binance_secret)

    def get_config_by_id(self, config_id: str) -> Optional[Dict]:
        """
        通过配置ID获取完整配置（推荐使用）

        Args:
            config_id: 配置ID

        Returns:
            配置字典，如果不存在则返回None
        """
        return self.configs_by_id.get(config_id)

    def get_symbol_config(self, symbol: str) -> Optional[Dict]:
        """
        获取指定交易对的完整配置（向后兼容，不推荐）
        ⚠️ 警告：如果有多个相同交易对，只返回第一个

        Args:
            symbol: 交易对符号

        Returns:
            配置字典，如果不存在则返回None
        """
        for config in self.symbol_configs:
            if config.get('symbol') == symbol:
                logger.warning(f"⚠️ 使用 symbol 查询配置已过时，建议使用 config_id")
                return config
        return None

    def get_configs_by_symbol(self, symbol: str) -> List[Dict]:
        """
        获取指定交易对的所有配置（支持多个相同交易对）

        Args:
            symbol: 交易对符号

        Returns:
            配置列表
        """
        return [cfg for cfg in self.symbol_configs if cfg.get('symbol') == symbol]

    def get_leverage(self, config_id: Optional[str] = None) -> int:
        """
        获取杠杆倍数
        优先级：配置专属杠杆 > 全局默认杠杆

        Args:
            config_id: 配置ID

        Returns:
            杠杆倍数
        """
        if config_id:
            config = self.configs_by_id.get(config_id)
            if config and 'leverage' in config:
                leverage = config.get('leverage')
                logger.debug(f"使用配置 {config_id} 的专属杠杆: {leverage}x")
                return int(leverage)

        # 返回全局默认杠杆
        logger.debug(f"使用全局杠杆配置: {self.leverage}x")
        return self.leverage

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
