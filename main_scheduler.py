import time
import schedule
import json
import os
from dotenv import load_dotenv
from agent_graph import run_agent_for_config
from datetime import datetime

# 加载环境变量
load_dotenv()

def get_all_configs():
    """直接获取所有配置列表"""
    configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
    try:
        configs = json.loads(configs_str)
        return configs
    except Exception as e:
        print(f"❌ 解析 SYMBOL_CONFIGS 失败: {e}")
        return []

def job():
    # 每次执行重新加载配置
    configs = get_all_configs()
    
    print(f"\n[{datetime.now()}] === Starting Multi-Agent Cycle ({len(configs)} agents) ===")
    
    # ✅ 遍历每一个配置项，而不是遍历币种名
    for config in configs:
        symbol = config.get('symbol')
        model = config.get('model')
        
        # 简单校验
        if not symbol: continue

        try:
            # 直接把整个 config 字典传进去
            run_agent_for_config(config)
            
            # 休息一下，避免并发太高
            time.sleep(3) 
        except Exception as e:
            print(f"Error processing {symbol} ({model}): {e}")
            
    print(f"[{datetime.now()}] === Cycle Completed ===")

if __name__ == "__main__":
    while True:
        schedule.run_pending()
        time.sleep(1)