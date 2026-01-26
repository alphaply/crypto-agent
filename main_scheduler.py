import time
import json
import os
import concurrent.futures
from datetime import datetime, timedelta
import pytz 
from dotenv import load_dotenv
from agent_graph import run_agent_for_config

# 加载环境变量 (.env 文件)
load_dotenv()

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

# ==========================================
# 1. 硬编码配置 (用于本地调试/直接运行)
# ==========================================
# 注意：如果你希望优先使用环境变量，请在 get_all_configs 中调整顺序
DEFAULT_SYMBOL_CONFIGS = '[{"symbol": "BTC/USDT", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-c06f97c39bbd4c5187a24f0d466d4dd2", "model": "qwen3-max", "temperature": 0.7, "real_trade": false, "mode": "STRATEGY"}, {"symbol": "BTC/USDT", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-c06f97c39bbd4c5187a24f0d466d4dd2", "model": "qwen-plus", "temperature": 0.7, "real_trade": false, "mode": "STRATEGY"}, {"symbol": "ETH/USDT", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-c06f97c39bbd4c5187a24f0d466d4dd2", "model": "qwen3-max", "temperature": 0.7, "real_trade": true, "mode": "REAL"}, {"symbol": "SOL/USDT", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-c06f97c39bbd4c5187a24f0d466d4dd2", "model": "qwen3-max", "temperature": 0.7, "real_trade": false, "mode": "STRATEGY"}, {"symbol": "BNB/USDT", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-c06f97c39bbd4c5187a24f0d466d4dd2", "model": "qwen3-max", "temperature": 0.5, "real_trade": true, "mode": "REAL"}, {"symbol": "TRX/USDT", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "sk-c06f97c39bbd4c5187a24f0d466d4dd2", "model": "qwen-plus", "temperature": 0.7, "real_trade": false, "mode": "STRATEGY"}]'

def get_all_configs():
    """
    获取配置
    优先级:
    1. 环境变量 SYMBOL_CONFIGS
    2. 代码顶部的 DEFAULT_SYMBOL_CONFIGS 变量
    """
    # 尝试从环境变量获取
    configs_str = os.getenv('SYMBOL_CONFIGS')
    
    # 如果环境变量为空，使用代码里的默认值
    if not configs_str:
        # print("⚠️ 未检测到环境变量 SYMBOL_CONFIGS，使用代码内置默认配置。", flush=True)
        configs_str = DEFAULT_SYMBOL_CONFIGS

    try:
        # 清理可能存在的换行符（防止 .env 格式错误）
        if configs_str:
            configs_str = configs_str.strip()
            
        configs = json.loads(configs_str)
        return configs
    except Exception as e:
        print(f"❌ 配置解析失败: {e}", flush=True)
        print(f"   |-- 原始字符串: {configs_str[:50]}...", flush=True) # 打印前50个字符帮助debug
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
    根据 模式(Strategy/Real) 和 时间(工作日/周末) 动态决定运行间隔
    """
    # 1. 获取当前所有配置
    configs = get_all_configs()
    
    if not configs:
        print("⚠️ 警告: 没有加载到任何配置！将默认使用低频模式。", flush=True)
        return 60, "无配置-默认等待"

    # 2. 检查是否有任何一个配置是 REAL 模式 (增加调试打印)
    real_coins = [c['symbol'] for c in configs if c.get('mode', 'STRATEGY').upper() == 'REAL']
    has_real_mode = len(real_coins) > 0
    
    # print(f"🔍 模式检测: 共 {len(configs)} 个币种 | 实盘币种: {real_coins if real_coins else '无'}", flush=True)

    # 3. 获取当前时间信息
    now = datetime.now(TZ_CN)
    weekday = now.weekday() # 0=周一 ... 5=周六, 6=周日
    current_hour = now.hour

    interval_minutes = 60 # 默认 1h
    mode_name = "未知模式"

    # ==========================================
    # 分支 A: 包含实盘模式 -> 节奏较快
    # ==========================================
    if has_real_mode:
        # 1. 周六：全天 1h
        if weekday == 5:
            interval_minutes = 60
            mode_name = "🔴实盘-周六休整 (1h)"
            
        # 2. 周日：20:00 前 1h，20:00 后 15m
        elif weekday == 6:
            if current_hour < 20:
                interval_minutes = 60
                mode_name = "🔴实盘-周日白天 (1h)"
            else:
                interval_minutes = 15
                mode_name = "🔴实盘-周日启航 (15m)"
                
        # 3. 工作日 (周一至周五)：全天 15m
        else:
            interval_minutes = 15
            mode_name = "🔴实盘-工作日高频 (15m)"

    # ==========================================
    # 分支 B: 纯策略模式 -> 节奏较慢
    # ==========================================
    else:
        # 1. 周末 (周六、周日全天)：4h
        if weekday >= 5:
            interval_minutes = 240 # 4小时
            mode_name = "🔵策略-周末长线 (4h)"
            
        # 2. 工作日 (周一至周五)：1h
        else:
            interval_minutes = 60
            mode_name = "🔵策略-工作日标准 (1h)"

    return interval_minutes, mode_name

def wait_until_next_slot(interval_minutes, delay_seconds=20):
    """
    计算并睡眠直到下一个 K 线收盘时间点
    """
    now = datetime.now().astimezone(TZ_CN)
    now_ts = now.timestamp()
    interval_seconds = interval_minutes * 60
    
    # 核心算法：找到下一个整点倍数
    next_ts = ((now_ts // interval_seconds) + 1) * interval_seconds
    next_run_time_ts = next_ts + delay_seconds
    
    next_run_time = datetime.fromtimestamp(next_run_time_ts).astimezone(TZ_CN)
    sleep_seconds = next_run_time_ts - now_ts
    
    print(f"\n⏳ [调度器] 状态: 待机中 | 对齐周期: {interval_minutes}m", flush=True)
    print(f"   |-- 当前时间: {now.strftime('%H:%M:%S')}", flush=True)
    print(f"   |-- 下次执行: {next_run_time.strftime('%H:%M:%S')} (缓冲 {delay_seconds}s)", flush=True)
    print(f"   |-- 倒计时: {int(sleep_seconds)} 秒", flush=True)
    
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

def job():
    configs = get_all_configs()
    if not configs:
        print("❌ 没有配置，跳过本轮执行。", flush=True)
        return

    print(f"\n[{datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}] 🚀 启动新一轮分析 ({len(configs)} 个币种)...", flush=True)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in configs]
        concurrent.futures.wait(futures)
            
    print(f"[{datetime.now(TZ_CN).strftime('%H:%M:%S')}] ✅ 本轮结束。", flush=True)

def run_smart_scheduler():
    """
    封装好的智能调度主循环
    """
    print("--- [系统] 智能 K线对齐调度器已启动 ---", flush=True)
    
    # 启动时先打印一次配置状态，确认模式是否获取成功
    configs = get_all_configs()
    real_coins = [c['symbol'] for c in configs if c.get('mode', 'STRATEGY').upper() == 'REAL']
    print(f"🔍 [初始化检查] 加载配置: {len(configs)} 个 | 实盘模式: {len(real_coins)} 个 ({', '.join(real_coins)})")
    
    while True:
        try:
            # 1. 获取当前应该跑的频率
            interval, mode_str = get_next_run_settings()
            print(f"\n📅 [系统扫描] {mode_str} | 目标间隔: {interval} 分钟", flush=True)
            
            # 2. 睡眠直到下一个时间点
            wait_until_next_slot(interval_minutes=interval, delay_seconds=20)
            
            # 3. 醒来后，立即干活
            job()
            
        except Exception as e:
            print(f"❌ 调度循环发生异常: {e}", flush=True)
            time.sleep(60)

if __name__ == "__main__":
    run_smart_scheduler()