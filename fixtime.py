import ccxt
import time
import os
from dotenv import load_dotenv

# 1. 加载 .env 文件中的 API Key
load_dotenv()

def test_and_fix_time_sync(proxy_port=None):
    # 从环境变量读取 Key
    api_key = os.getenv('BINANCE_API_KEY')
    secret = os.getenv('BINANCE_SECRET')

    if not api_key or not secret:
        print("❌ 错误：未在 .env 文件中找到 BINANCE_API_KEY 或 BINANCE_SECRET")
        return

    config = {
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
            'adjustForTimeDifference': True  # <--- 核心修复：自动对齐时间
        }
    }

    if proxy_port:
        config['proxies'] = {
            'http': f'http://127.0.0.1:{proxy_port}',
            'https': f'http://127.0.0.1:{proxy_port}',
        }

    print("正在连接 Binance...")
    
    try:
        exchange = ccxt.binanceusdm(config)
        
        # 1. 强制校准时间
        exchange.load_markets() 
        
        # 2. 打印时间偏差
        local_time = int(time.time() * 1000)
        server_time = exchange.fetch_time()
        diff = local_time - server_time
        print(f"✅ 时间同步成功！偏差值: {diff} ms (ccxt 已自动处理)")

        # 3. 测试私有接口 (余额)
        print("正在获取余额以验证 Key...")
        balance = exchange.fetch_balance()
        usdt = balance['USDT']['free'] if 'USDT' in balance else 0
        print(f"💰 验证成功！当前可用 USDT: {usdt}")

    except Exception as e:
        print(f"❌ 依然报错: {e}")

if __name__ == "__main__":
    # 记得改成你的代理端口
    test_and_fix_time_sync(10809)