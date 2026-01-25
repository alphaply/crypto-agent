import ccxt
import os
import json
from dotenv import load_dotenv
from datetime import datetime

# 加载 .env 环境变量
load_dotenv()

def print_json(data):
    """漂亮地打印 JSON 数据"""
    print(json.dumps(data, indent=4, default=str))

def debug_account():
    # 1. 初始化交易所
    api_key = os.getenv('BINANCE_API_KEY')
    print(api_key)
    secret = os.getenv('BINANCE_SECRET')
    
    if not api_key:
        print("❌ 错误：未找到 BINANCE_API_KEY，请检查 .env 文件")
        return

    exchange = ccxt.binanceusdm({
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'},
        'proxies': {
            'http': 'http://127.0.0.1:10809',
            'https': 'http://127.0.0.1:10809', # 注意：这里通常也填 http 协议
        },
    })

    symbol = "ETH/USDT"  # 你正在测试的币种
    print(f"🔍 正在连接币安合约，查询 {symbol} 的所有挂单...\n")

    try:
        exchange.load_markets()
        exchange.fetch
        # 2. 获取 Open Orders (未成交的挂单)
        # 注意：币安合约有时候把条件单放在 openOrders，有时候归类不同
        # 我们不做任何过滤，直接看 raw data
        orders = exchange.fetch_open_orders(symbol)
        
        print(f"📊 ----------------------------------------------------")
        print(f"📊 共发现 {len(orders)} 个活跃挂单 (Open Orders)")
        print(f"📊 ----------------------------------------------------\n")

        for i, o in enumerate(orders):
            print(f"🔹 [第 {i+1} 单] ID: {o['id']}")
            print(f"   类型 (CCXT): {o['type']}") 
            print(f"   方向: {o['side']}")
            print(f"   价格 (Price): {o.get('price')} (这是限价单价格)")
            print(f"   触发价 (StopPrice): {o.get('stopPrice')} (这是条件单触发价)")
            print(f"   状态: {o['status']}")
            
            # 关键：打印原始 info，看看币安底层怎么说的
            print(f"   👉 原始类型 (Raw Type): {o['info'].get('type')}")
            print(f"   👉 原始触发价 (Raw Stop): {o['info'].get('stopPrice')}")
            print(f"   👉 Reduce Only: {o['info'].get('reduceOnly')}")
            print("-" * 40)

        # 3. 额外检查：如果列表为空，或者没有看到止盈止损
        # 可能是因为主单还没成交。
        # 在币安，如果你是在下 Limit 单时附带的 TP/SL，
        # **只有当主 Limit 单成交（Filled）变成持仓后，止盈止损单才会生成！**
        if len(orders) > 0:
            print("\n💡 调试分析提示：")
            print("1. 如果你看到了 LIMIT 单，但没看到 STOP/TAKE_PROFIT：")
            print("   -> 检查你的主 Limit 单是否还是 'NEW' (未成交) 状态？")
            print("   -> 币安机制：'带单'的止盈止损只有在主单成交瞬间才会创建。")
            
            print("\n2. 如果你想看到它们，必须：")
            print("   -> 要么主单成交。")
            print("   -> 要么手动下独立的 'STOP_MARKET' 订单（而不是附带在 params 里）。")

    except Exception as e:
        print(f"❌ 发生错误: {e}")

if __name__ == "__main__":
    debug_account()