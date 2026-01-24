import time
import schedule
from agent_graph import run_agent_for_symbol
from datetime import datetime

# 定义你关注的币种列表
TARGET_SYMBOLS = [
    'BTC/USDT',
    'ETH/USDT',
    # 'SOL/USDT',
    # 'BNB/USDT'
]

def job():
    print(f"\n[{datetime.now()}] === Starting Multi-Symbol Cycle ===")
    
    for symbol in TARGET_SYMBOLS:
        try:
            run_agent_for_symbol(symbol)
            # 休息一下，避免并发请求太多触发 API 限制
            time.sleep(3) 
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            
    print(f"[{datetime.now()}] === Cycle Completed ===")

# 立即执行一次
job()

# 每 15 分钟执行一次
schedule.every(15).minutes.do(job)

if __name__ == "__main__":
    while True:
        schedule.run_pending()
        time.sleep(1)