import ccxt
import time

def auto_cancel_orders():
    # 1. 初始化
    exchange = ccxt.binanceusdm({
        'apiKey': '71qOCCXned5999rvG7yQ1JUwDG40xUPmTPrIZSY6WKLZqxsEARQcxCD8QKQSIlrP',
        'secret': '71s1jgFsMwRTfesAxJsjuGLsfum77Z5CK94QKa97is0pc6oPdfImrJePGDwg3noe',
        'enableRateLimit': True,
        'proxies': {
            'http': 'http://127.0.0.1:10809',
            'https': 'http://127.0.0.1:10809',
        },
    })

    try:
        print("--- 1. 正在连接并加载市场 ---")
        exchange.load_markets()
        
        # 2. 自动寻找正确的 Symbol 格式
        # 我们尝试去匹配包含 ETH 和 USDT 的合约
        target_symbol = None
        
        # 优先尝试标准 U本位永续格式
        candidates = ['ETH/USDT:USDT', 'ETH/USDT', 'ETHUSDT']
        
        print(f"--- 2. 正在自动匹配交易对名称 ---")
        for symbol in candidates:
            if symbol in exchange.markets:
                target_symbol = symbol
                print(f"✅ 匹配成功！CCXT 内部名称为: 【{target_symbol}】")
                break
        
        # 如果还没找到，打印一个列表看看
        if not target_symbol:
            print("❌ 自动匹配失败！打印前10个可用交易对供参考：")
            print(list(exchange.markets.keys())[:10])
            return

        # 3. 获取所有订单
        print(f"--- 3. 正在查询 {target_symbol} 的挂单 ---")
        orders = exchange.fetch_open_orders(symbol=target_symbol)
        
        # 4. 筛选止盈止损单
        targets = []
        for order in orders:
            # 只要包含 STOP 或 TAKE_PROFIT 的类型都算
            if 'STOP' in order['type'] or 'TAKE_PROFIT' in order['type']:
                targets.append(order)

        print(f"查询结果: 发现 {len(targets)} 个条件单。")

        if not targets:
            print("提示：当前没有查到条件单。")
            return

        # 5. 撤单
        print("\n--- 4. 开始撤销 ---")
        for order in targets:
            print(f"正在撤销 ID: {order['id']} | 类型: {order['type']} ...")
            exchange.cancel_order(id=order['id'], symbol=target_symbol)
            print("✅ 撤销成功！")

    except ccxt.AuthenticationError:
        print("❌ 权限错误：API Key 或 Secret 不正确，请检查！")
    except Exception as e:
        print(f"❌ 发生未知错误: {e}")

if __name__ == '__main__':
    auto_cancel_orders()