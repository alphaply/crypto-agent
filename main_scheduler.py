import time
import schedule
import json
import os
from dotenv import load_dotenv
from agent_graph import run_agent_for_symbol
from datetime import datetime

# 加载环境变量
load_dotenv()

def get_target_symbols():
    """从环境变量 SYMBOL_CONFIGS 中动态获取币种列表"""
    configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
    default_symbols = ['BTC/USDT', 'ETH/USDT'] # 兜底默认值
    
    try:
        configs = json.loads(configs_str)
        # 提取配置中所有的 symbol
        symbols = [cfg['symbol'] for cfg in configs if 'symbol' in cfg]
        
        if not symbols:
            print("⚠️ 警告: SYMBOL_CONFIGS 为空或格式错误，使用默认币种列表。")
            return default_symbols
            
        return symbols
    except Exception as e:
        print(f"❌ 解析 SYMBOL_CONFIGS 失败: {e}，使用默认列表。")
        return default_symbols

# 初始化目标币种
TARGET_SYMBOLS = get_target_symbols()

def job():
    # 每次执行前重新加载（可选：如果你希望不重启程序就能动态更新配置，把 get_target_symbols 放这里）
    # global TARGET_SYMBOLS
    # TARGET_SYMBOLS = get_target_symbols()
    
    print(f"\n[{datetime.now()}] === Starting Multi-Symbol Cycle ===")
    print(f"📋 Target Symbols: {TARGET_SYMBOLS}")
    
    for symbol in TARGET_SYMBOLS:
        try:
            run_agent_for_symbol(symbol)
            # 休息一下，避免并发请求太多触发 API 限制
            time.sleep(3) 
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            
    print(f"[{datetime.now()}] === Cycle Completed ===")

# # 立即执行一次
# job()

# # 每 15 分钟执行一次
# schedule.every(15).minutes.do(job)

if __name__ == "__main__":
    while True:
        schedule.run_pending()
        time.sleep(1)