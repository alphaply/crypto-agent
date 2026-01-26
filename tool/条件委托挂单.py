import ccxt
import time

def place_market_tp_sl_for_eth():
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
        print("正在加载市场信息...")
        exchange.load_markets()
        
        # 【修改点 1】使用标准格式，CCXT 识别度更高
        target_symbol = 'ETHUSDT'  
        
        # --- 设置你的止盈止损价格 ---
        stop_loss_price = 2000.0    # 止损价
        take_profit_price = 4000.0  # 止盈价

        # 2. 获取持仓
        print(f"正在查询 {target_symbol} 当前持仓...")
        positions = exchange.fetch_positions([target_symbol])
        target_position = None
        for p in positions:
            if p['symbol'] == target_symbol or p['symbol'] == 'ETH/USDT:USDT':
                target_position = p
                break
        
        if not target_position:
            print("错误：未找到持仓。")
            return

        amount = float(target_position['contracts'])
        side = target_position['side']
        print(f"当前持仓: {amount} ETH, 方向: {side}")

        if amount <= 0 or side != 'long':
            print("提示：当前没有 ETH 多单持仓。")
            return

        print(f"\n正在为 {amount} ETH 设置双向止盈止损...")

        # 3. 下【市价止损】单 (STOP_MARKET)
        print(f"1. 正在提交市价止损 (触发价: {stop_loss_price})...")
        sl_order = exchange.create_order(
            symbol=target_symbol,
            type='STOP_MARKET',    # 市价止损
            side='sell',
            amount=amount,
            params={
                'stopPrice': stop_loss_price, 
                'positionSide': 'LONG',       
            }
        )
        print(f"   ✅ 止损单 ID: {sl_order['id']}")

        # 4. 下【市价止盈】单 (TAKE_PROFIT_MARKET)
        print(f"2. 正在提交市价止盈 (触发价: {take_profit_price})...")
        tp_order = exchange.create_order(
            symbol=target_symbol,
            type='TAKE_PROFIT_MARKET', # 市价止盈
            side='sell',
            amount=amount,
            params={
                'stopPrice': take_profit_price,
                'positionSide': 'LONG',
            }
        )
        print(f"   ✅ 止盈单 ID: {tp_order['id']}")
        
        print("\n所有操作完成！请去 App 查看【当前委托】->【止盈止损】")

    except Exception as e:
        print(f"\n❌ 发生错误: {e}")

if __name__ == '__main__':
    place_market_tp_sl_for_eth()