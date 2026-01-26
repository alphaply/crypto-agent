import ccxt
from pprint import pprint

def force_scan_everything():
    # 1. 初始化
    exchange = ccxt.binanceusdm({
        'apiKey': '71qOCCXned5999rvG7yQ1JUwDG40xUPmTPrIZSY6WKLZqxsEARQcxCD8QKQSIlrP',
        'secret': '71s1jgFsMwRTfesAxJsjuGLsfum77Z5CK94QKa97is0pc6oPdfImrJePGDwg3noe',
        'enableRateLimit': True,
        'proxies': {
            'http': 'http://127.0.0.1:10809',
            'https': 'http://127.0.0.1:10809',
        },
        'options': {
            # =============== 关键修改 ===============
            # 强制关闭警告，允许无参数全站扫描
            'warnOnFetchOpenOrdersWithoutSymbol': False, 
            'defaultType': 'future',
            # =======================================
        }
    })

    try:
        print("--- 正在加载市场 (稍等) ---")
        exchange.load_markets()
        
        print("--- 正在暴力扫描全账户所有挂单 ---")
        # 这次不会报错了，它会强制去币安服务器把所有角落的单子抓出来
        all_orders = exchange.fetch_open_orders()
        
        print(f"\n======== 扫描结果: 发现 {len(all_orders)} 个挂单 ========")
        
        found_target = False
        for order in all_orders:
            symbol = order['symbol']
            oid = order['id']
            otype = order['type']
            trigger = order['info'].get('stopPrice', 'N/A')
            
            print(f"🔴 发现: [{symbol}] | ID: {oid} | 类型: {otype} | 触发价: {trigger}")
            
            # 只要是 ETH 的单子，不管名字叫什么，都标记出来
            if 'ETH' in symbol:
                found_target = True

        if len(all_orders) == 0:
            print("\n❌ 依然显示 0 个挂单。")
            print("如果 App 上确实有，那么结论只有一个：")
            print("👉 你现在的 API Key 对应的账户，和你 App 上看的账户，【绝对不是同一个】！")
            print("👉 请检查：1. 是否有子账户？ 2. App 是否切到了模拟盘？")
        elif found_target:
            print("\n✅ 终于找到了！请复制上面的 ID 和 Symbol 去运行撤单脚本。")

    except Exception as e:
        print(f"❌ 发生错误: {e}")

if __name__ == '__main__':
    force_scan_everything()