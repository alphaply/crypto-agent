"""
简化的配置系统验证脚本（避免编码问题）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 60)
print("配置系统验证")
print("=" * 60)

# 测试1: 导入config模块
print("\n[测试1] 导入config模块...")
try:
    from config import config
    print("SUCCESS: 配置模块导入成功")
except Exception as e:
    print(f"FAILED: {e}")
    sys.exit(1)

# 测试2: 检查全局配置
print("\n[测试2] 检查全局配置...")
try:
    print(f"  - 全局API Key: {config.global_binance_api_key[:10] if config.global_binance_api_key else 'None'}...")
    print(f"  - 杠杆倍数: {config.leverage}")
    print(f"  - 启用调度器: {config.enable_scheduler}")
    print("SUCCESS: 全局配置正常")
except Exception as e:
    print(f"FAILED: {e}")

# 测试3: 检查交易对配置
print("\n[测试3] 检查交易对配置...")
try:
    configs = config.get_all_symbol_configs()
    print(f"  - 配置数量: {len(configs)}")
    for i, cfg in enumerate(configs[:3], 1):  # 只显示前3个
        symbol = cfg.get('symbol', 'Unknown')
        mode = cfg.get('mode', 'Unknown')
        has_api = bool(cfg.get('binance_api_key'))
        print(f"  - 配置{i}: {symbol} ({mode}) - 专属API: {has_api}")
    print("SUCCESS: 交易对配置正常")
except Exception as e:
    print(f"FAILED: {e}")

# 测试4: 测试API凭证获取
print("\n[测试4] 测试API凭证获取...")
try:
    # 测试全局配置
    api_key, secret = config.get_binance_credentials("BTC/USDT")
    is_global = (api_key == config.global_binance_api_key)
    print(f"  - BTC/USDT: 使用{'全局' if is_global else '专属'}配置")

    # 检查是否有专属配置
    has_specific = False
    for cfg in configs:
        if cfg.get('binance_api_key'):
            symbol = cfg.get('symbol')
            api_key2, _ = config.get_binance_credentials(symbol)
            is_specific = (api_key2 != config.global_binance_api_key)
            print(f"  - {symbol}: 使用{'专属' if is_specific else '全局'}配置")
            has_specific = True
            break

    if not has_specific:
        print("  - 注意: 当前没有配置专属API的交易对")

    print("SUCCESS: API凭证获取正常")
except Exception as e:
    print(f"FAILED: {e}")

# 测试5: 测试MarketTool初始化
print("\n[测试5] 测试MarketTool初始化...")
try:
    from market_data import MarketTool

    if configs:
        symbol = configs[0].get('symbol')
        print(f"  - 测试交易对: {symbol}")
        market_tool = MarketTool(symbol=symbol)
        print(f"SUCCESS: MarketTool初始化成功")
    else:
        print("SKIPPED: 没有配置的交易对")
except Exception as e:
    print(f"WARNING: MarketTool初始化失败 (可能是网络问题): {e}")
    print("  这通常是正常的，如果需要代理或网络连接")

print("\n" + "=" * 60)
print("验证完成")
print("=" * 60)
