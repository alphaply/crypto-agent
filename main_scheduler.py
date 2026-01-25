import time
import json
import os
import concurrent.futures
from datetime import datetime, timedelta
import pytz 
from dotenv import load_dotenv
from agent_graph import run_agent_for_config

# 加载环境变量
load_dotenv()

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

def get_all_configs():
    """获取配置"""
    configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
    try:
        return json.loads(configs_str)
    except Exception as e:
        print(f"❌ 配置解析失败: {e}", flush=True)
        return []

def process_single_config(config):
    """单线程任务"""
    symbol = config.get('symbol')
    if not symbol: return
    try:
        run_agent_for_config(config)
    except Exception as e:
        print(f"❌ Error {symbol}: {e}", flush=True)

def get_next_run_settings():
    """
    核心逻辑：根据当前时间，决定下一次运行的【间隔】和【具体时间点】
    修改点：周日晚上 (20:00后) 恢复为高频模式
    """
    now = datetime.now(TZ_CN)
    weekday = now.weekday() # 0=周一 ... 5=周六, 6=周日
    current_hour = now.hour

    # --- 定义“周末低频模式”的生效时间 ---
    # 逻辑：
    # 1. 如果是周六 (5)，全天低频
    # 2. 如果是周日 (6)，且时间在晚上 20:00 之前，低频；20:00 之后恢复高频
    # 3. 其他时间 (周一至周五)，高频
    
    is_weekend_low_freq_time = False

    if weekday == 5:
        # 周六：全天低频
        is_weekend_low_freq_time = True
    elif weekday == 6:
        # 周日：20:00 之前低频，20:00 之后恢复活跃
        if current_hour < 20: 
            is_weekend_low_freq_time = True
        else:
            is_weekend_low_freq_time = False
    else:
        # 周一至周五：全天活跃
        is_weekend_low_freq_time = False

    # --- 决策间隔 (单位: 分钟) ---
    if is_weekend_low_freq_time:
        interval_minutes = 60  # 周末/周日白天的低频模式
        mode_name = "周末低频 (1h)"
    else:
        # 工作日模式 (含周日晚)
        # 即使是工作日，也可以区分一下美盘活跃时段用于日志显示
        is_us_session = (current_hour >= 21 or current_hour < 7)
        interval_minutes = 15
        
        if is_us_session:
            mode_name = "美盘强波 (15m)" 
        elif weekday == 6 and current_hour >= 20:
             mode_name = "周日启航 (15m)"
        else:
            mode_name = "亚欧时段 (15m)"

    return interval_minutes, mode_name

def wait_until_next_slot(interval_minutes, delay_seconds=20):
    """
    计算并睡眠直到下一个 K 线收盘时间点
    """
    # 获取当前时间（带时区）
    now = datetime.now().astimezone(TZ_CN)
    
    # 将当前时间转为时间戳
    now_ts = now.timestamp()
    
    # 间隔转为秒
    interval_seconds = interval_minutes * 60
    
    # 核心算法：找到下一个整点倍数
    # 例如 interval=900s (15m), 下一次就是整 15, 30, 45, 00 分
    next_ts = ((now_ts // interval_seconds) + 1) * interval_seconds
    
    # 加上缓冲时间 (例如 :00分20秒 执行)
    next_run_time_ts = next_ts + delay_seconds
    
    # 转回 datetime 对象用于显示 (强制北京时间)
    next_run_time = datetime.fromtimestamp(next_run_time_ts).astimezone(TZ_CN)
    
    # 计算需要睡多久
    sleep_seconds = next_run_time_ts - now_ts
    
    print(f"\n⏳ [调度器] 状态: 待机中 | 模式: K线对齐", flush=True)
    print(f"   |-- 当前时间: {now.strftime('%H:%M:%S')}", flush=True)
    print(f"   |-- 下次执行: {next_run_time.strftime('%H:%M:%S')} (缓冲 {delay_seconds}s)", flush=True)
    print(f"   |-- 倒计时: {int(sleep_seconds)} 秒", flush=True)
    
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

def job():
    configs = get_all_configs()
    # 使用 flush=True 确保日志立即打印
    print(f"\n[{datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}] 🚀 启动新一轮分析 ({len(configs)} 个币种)...", flush=True)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in configs]
        concurrent.futures.wait(futures)
            
    print(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ✅ 本轮结束。", flush=True)

def run_smart_scheduler():
    """
    封装好的智能调度主循环，供 Dashboard 调用
    """
    print("--- [系统] 智能 K线对齐调度器已启动 ---", flush=True)
    
    # 刚启动时，先不执行 job，而是先进入等待逻辑，对齐下一个 K 线
    
    while True:
        try:
            # 1. 获取当前应该跑的频率
            interval, mode_str = get_next_run_settings()
            print(f"\n📅 [系统扫描] {mode_str} | 目标间隔: {interval} 分钟", flush=True)
            
            # 2. 睡眠直到下一个时间点 (这句代码执行完，意味着已经睡醒了)
            wait_until_next_slot(interval_minutes=interval, delay_seconds=20)
            
            # 3. 醒来后，立即干活
            job()
            
        except Exception as e:
            print(f"❌ 调度循环发生异常: {e}", flush=True)
            time.sleep(60) # 出错后冷却1分钟

if __name__ == "__main__":
    run_smart_scheduler()