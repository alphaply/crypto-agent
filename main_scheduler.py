import time
import json
import os
import concurrent.futures
from datetime import datetime, timedelta
import pytz # 需要安装: pip install pytz
from dotenv import load_dotenv
from agent_graph import run_agent_for_config

# 加载环境变量
load_dotenv()

# 设置时区 (以北京时间为例，方便判断美盘)
TZ_CN = pytz.timezone('Asia/Shanghai')

def get_all_configs():
    """获取配置"""
    configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
    try:
        return json.loads(configs_str)
    except Exception as e:
        print(f"❌ 配置解析失败: {e}")
        return []

def process_single_config(config):
    """单线程任务"""
    symbol = config.get('symbol')
    if not symbol: return
    try:
        run_agent_for_config(config)
    except Exception as e:
        print(f"❌ Error {symbol}: {e}")

def get_next_run_settings():
    """
    核心逻辑：根据当前时间，决定下一次运行的【间隔】和【具体时间点】
    """
    now = datetime.now(TZ_CN)
    weekday = now.weekday() # 0=周一 ... 5=周六, 6=周日
    current_hour = now.hour

    # --- 策略 1: 判断是否是周末 ---
    is_weekend = (weekday >= 5) 

    # --- 策略 2: 判断是否是美盘强波动时段 (北京时间 21:00 - 次日 04:00) ---
    # 即使是周末，有时候周日晚上美盘也会动，这里简单处理：周末优先低频
    is_us_session = (current_hour >= 21 or current_hour < 7)

    # 决策间隔 (单位: 分钟)
    if is_weekend:
        interval_minutes = 60  # 周末：1小时一次
        mode_name = "周末低频 (1h)"
    else:
        # 工作日
        if is_us_session:
            # 你可以在这里改成 5，如果你想在美盘每 5 分钟跑一次
            interval_minutes = 15 
            mode_name = "美盘时段 (15m)" 
        else:
            interval_minutes = 15
            mode_name = "亚欧盘时段 (15m)"

    return interval_minutes, mode_name

def wait_until_next_slot(interval_minutes, delay_seconds=20):
    """
    计算并睡眠直到下一个 K 线收盘时间点
    :param interval_minutes: 间隔 (5, 15, 60 等)
    :param delay_seconds: 收盘后的缓冲时间 (防止交易所数据延迟)
    """
    now = datetime.now()
    
    # 将当前时间转为时间戳
    now_ts = now.timestamp()
    
    # 间隔转为秒
    interval_seconds = interval_minutes * 60
    
    # 核心算法：找到下一个整点倍数
    # 例如 interval=900s (15m), 当前是 1000s
    # 下一次 = (1000 // 900 + 1) * 900 = 1800s
    next_ts = ((now_ts // interval_seconds) + 1) * interval_seconds
    
    # 加上缓冲时间 (例如 :00分20秒 执行)
    next_run_time = datetime.fromtimestamp(next_ts) + timedelta(seconds=delay_seconds)
    
    # 计算需要睡多久
    sleep_seconds = (next_run_time - datetime.now()).total_seconds()
    
    print(f"\n⏳ [调度器] 当前模式: 等待 K线收盘对齐...")
    print(f"   |-- 下次执行: {next_run_time.strftime('%H:%M:%S')} (缓冲 {delay_seconds}s)")
    print(f"   |-- 倒计时: {int(sleep_seconds)} 秒")
    
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

def job():
    configs = get_all_configs()
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 启动新一轮分析 ({len(configs)} 个币种)...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in configs]
        concurrent.futures.wait(futures)
            
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 本轮结束。")
def run_smart_scheduler():
    """
    封装好的智能调度主循环，供 Dashboard 调用
    """
    # 启动时先打印一下
    print("--- [系统] 智能 K线对齐调度器已启动 ---")
    
    while True:
        try:
            # 1. 获取当前应该跑的频率 (周末/美盘/亚盘)
            interval, mode_str = get_next_run_settings()
            
            print(f"\n📅 [调度状态] {mode_str} | 目标间隔: {interval} 分钟")

            # 2. 睡眠直到下一个对齐的时间点 (比如 10:00:20, 10:15:20)
            wait_until_next_slot(interval_minutes=interval, delay_seconds=20)
            
            # 3. 醒来，执行任务
            job()
            
        except Exception as e:
            print(f"❌ 调度循环发生异常: {e}")
            time.sleep(60) # 出错后冷却1分钟防止死循环刷屏

if __name__ == "__main__":
    # 本地直接运行脚本时执行
    run_smart_scheduler()