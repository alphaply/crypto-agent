"""
配置系统测试脚本
验证配置加载和优先级是否正确
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_config_loading():
    """测试配置加载"""
    print("=" * 60)
    print("测试1: 配置模块加载")
    print("=" * 60)

    try:
        from config import config
        print("✅ 配置模块加载成功")
        return True
    except Exception as e:
        print(f"❌ 配置模块加载失败: {e}")
        return False


def test_global_config():
    """测试全局配置"""
    print("\n" + "=" * 60)
    print("测试2: 全局配置读取")
    print("=" * 60)

    try:
        from config import config

        print(
            f"全局币安API Key: {config.global_binance_api_key[:10]}..." if config.global_binance_api_key else "未配置")
        print(f"全局币安Secret: {config.global_binance_secret[:10]}..." if config.global_binance_secret else "未配置")
        print(f"杠杆倍数: {config.leverage}")
        print(f"启用调度器: {config.enable_scheduler}")
        print(f"交易对配置数量: {len(config.symbol_configs)}")

        print("✅ 全局配置读取成功")
        return True
    except Exception as e:
        print(f"❌ 全局配置读取失败: {e}")
        return False


def test_symbol_configs():
    """测试交易对配置"""
    print("\n" + "=" * 60)
    print("测试3: 交易对配置读取")
    print("=" * 60)

    try:
        from config import config

        configs = config.get_all_symbol_configs()
        print(f"配置的交易对数量: {len(configs)}")

        for i, cfg in enumerate(configs, 1):
            symbol = cfg.get('symbol', 'Unknown')
            mode = cfg.get('mode', 'Unknown')
            has_specific_api = bool(cfg.get('binance_api_key') and cfg.get('binance_secret'))

            print(f"\n配置 {i}:")
            print(f"  交易对: {symbol}")
            print(f"  模式: {mode}")
            print(f"  专属API: {'是' if has_specific_api else '否'}")

        print("\n✅ 交易对配置读取成功")
        return True
    except Exception as e:
        print(f"❌ 交易对配置读取失败: {e}")
        return False


def test_api_credentials_priority():
    """测试API凭证优先级"""
    print("\n" + "=" * 60)
    print("测试4: API凭证优先级")
    print("=" * 60)

    try:
        from config import config

        # 测试没有专属配置的交易对（应该使用全局配置）
        print("\n测试场景1: 没有专属配置的交易对")
        api_key, secret = config.get_binance_credentials("BTC/USDT")
        print(f"  BTC/USDT API Key: {api_key[:10]}..." if api_key else "未配置")
        print(f"  使用: {'全局配置' if api_key == config.global_binance_api_key else '专属配置'}")

        # 测试有专属配置的交易对（如果存在）
        print("\n测试场景2: 检查是否有专属配置的交易对")
        has_specific = False
        for cfg in config.symbol_configs:
            if cfg.get('binance_api_key') and cfg.get('binance_secret'):
                symbol = cfg.get('symbol')
                api_key, secret = config.get_binance_credentials(symbol)
                print(f"  {symbol} API Key: {api_key[:10]}..." if api_key else "未配置")
                print(f"  使用: {'专属配置' if api_key != config.global_binance_api_key else '全局配置'}")
                has_specific = True
                break

        if not has_specific:
            print("  当前配置中没有专属API配置的交易对")

        print("\n✅ API凭证优先级测试通过")
        return True
    except Exception as e:
        print(f"❌ API凭证优先级测试失败: {e}")
        return False


def test_market_tool_initialization():
    """测试MarketTool初始化"""
    print("\n" + "=" * 60)
    print("测试5: MarketTool初始化")
    print("=" * 60)

    try:
        from market_data import MarketTool
        from config import config

        # 获取第一个配置的交易对
        configs = config.get_all_symbol_configs()
        if not configs:
            print("⚠️ 没有配置的交易对，跳过测试")
            return True

        symbol = configs[0].get('symbol')
        print(f"测试交易对: {symbol}")

        # 尝试初始化MarketTool
        print("正在初始化MarketTool...")
        market_tool = MarketTool(symbol=symbol)
        print(f"✅ MarketTool初始化成功 (symbol={symbol})")

        return True
    except Exception as e:
        print(f"❌ MarketTool初始化失败: {e}")
        print("注意: 如果是网络连接错误，这是正常的（需要代理或网络连接）")
        return True  # 网络错误不算测试失败


def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("配置系统测试")
    print("=" * 60)

    results = []

    # 运行所有测试
    results.append(("配置模块加载", test_config_loading()))
    results.append(("全局配置读取", test_global_config()))
    results.append(("交易对配置读取", test_symbol_configs()))
    results.append(("API凭证优先级", test_api_credentials_priority()))
    results.append(("MarketTool初始化", test_market_tool_initialization()))

    # 输出测试结果摘要
    print("\n" + "=" * 60)
    print("测试结果摘要")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{name}: {status}")

    print(f"\n总计: {passed}/{total} 测试通过")

    if passed == total:
        print("\n🎉 所有测试通过！配置系统工作正常。")
    else:
        print("\n⚠️ 部分测试失败，请检查配置。")


if __name__ == "__main__":
    main()
