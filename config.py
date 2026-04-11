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

    # 默认属性 (防止某些情况下加载失败导致 AttributeError)
    timezone = 'Asia/Shanghai'
    trading_mode = 'REAL'
    leverage = 20
    admin_password = '123456'
    enable_scheduler = True
    global_binance_api_key = None
    global_binance_secret = None
    global_okx_api_key = None
    global_okx_secret = None
    global_okx_passphrase = None
    langchain_tracing = False
    langchain_api_key = ''
    langchain_project = 'crypto-agent'
    symbol_configs = []
    configs_by_id = {}

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

        # 欧易API配置
        self.global_okx_api_key = os.getenv('OKX_API_KEY')
        self.global_okx_secret = os.getenv('OKX_SECRET')
        self.global_okx_passphrase = os.getenv('OKX_PASSPHRASE')

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

        # 时区配置
        self.timezone = os.getenv('TIMEZONE', 'Asia/Shanghai')

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

        # 检查是否至少有一个有效的交易所API配置
        has_binance = bool(self.global_binance_api_key and self.global_binance_secret)
        has_okx = bool(self.global_okx_api_key and self.global_okx_secret and self.global_okx_passphrase)

        if not has_binance and not has_okx:
            # 如果没有全局配置，检查是否所有交易对都有专属配置
            for cfg in self.symbol_configs:
                symbol = cfg.get('symbol')
                exchange = cfg.get('exchange', 'binance').lower()
                
                if exchange == 'okx':
                    if not cfg.get('api_key') or not cfg.get('secret') or not cfg.get('passphrase'):
                         errors.append(f"交易对 {symbol} (OKX) 缺少API配置，且未配置全局OKX API")
                else: # binance
                    # 兼容老的 key 名 (binance_api_key) 和新的通用名 (api_key)
                    b_key = cfg.get('binance_api_key') or cfg.get('api_key')
                    b_secret = cfg.get('binance_secret') or cfg.get('secret')
                    if not b_key or not b_secret:
                        errors.append(f"交易对 {symbol} (Binance) 缺少币安API配置，且未配置全局API")

        if errors:
            error_msg = "配置验证失败:\n" + "\n".join(errors)
            logger.error(f"❌ {error_msg}")
            raise ValueError(error_msg)

        logger.info("✅ 配置验证通过")

    def get_exchange_credentials(self, config_id: Optional[str] = None, symbol: Optional[str] = None) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
        """
        获取交易所凭证
        优先级：config_id专属配置 > symbol专属配置 > 全局默认配置

        Returns:
            (exchange, api_key, secret, passphrase) 元组
        """
        config = None
        if config_id:
            config = self.configs_by_id.get(config_id)
        elif symbol:
            for cfg in self.symbol_configs:
                if cfg.get('symbol') == symbol:
                    config = cfg
                    break

        if config:
            exchange = config.get('exchange', 'binance').lower()
            # 优先检查通用命名的 api_key/secret/passphrase
            api_key = config.get('api_key')
            secret = config.get('secret')
            passphrase = config.get('passphrase')
            
            # 兼容币安旧命名
            if exchange == 'binance':
                api_key = api_key or config.get('binance_api_key')
                secret = secret or config.get('binance_secret')
            
            if api_key and secret:
                if exchange == 'okx' and not passphrase:
                    # 如果是OKX但没专属passphrase，尝试用全局的
                    passphrase = self.global_okx_passphrase
                
                logger.debug(f"使用配置 {config.get('config_id', symbol)} 的专属{exchange} API")
                return (exchange, api_key, secret, passphrase)

            # 如果 config 存在但没写 API，则回退到该 exchange 的全局配置
            if exchange == 'okx':
                return ('okx', self.global_okx_api_key, self.global_okx_secret, self.global_okx_passphrase)
            else:
                return ('binance', self.global_binance_api_key, self.global_binance_secret, None)

        # 全局默认回退 (默认 Binance)
        return ('binance', self.global_binance_api_key, self.global_binance_secret, None)

    def get_binance_credentials(self, config_id: Optional[str] = None, symbol: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        获取币安API凭证（向后兼容）
        """
        exch, key, secret, _ = self.get_exchange_credentials(config_id, symbol)
        if exch == 'binance':
            return (key, secret)
        return (None, None)

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
        return list(self.symbol_configs)

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
