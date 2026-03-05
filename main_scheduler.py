import time
import concurrent.futures
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from agent.agent_graph import run_agent_for_config
from utils.logger import setup_logger
from config import config as global_config
from database import init_db

# 加载环境变量 (.env 文件)
load_dotenv()

# 设置时区
TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))

# 初始化logger
logger = setup_logger("MainScheduler")

# ==========================================
# 1. 频率与触发逻辑优化
# ==========================================

def check_dca_executed(config_id, now, freq='1d'):
    """
    检查指定周期内是否已执行过定投（通过检查是否有决策记录或分析摘要）
    """
    from database import get_db_conn
    try:
        if freq == '1w':
            start_of_week = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
            query_time = f"{start_of_week}%"
        else:
            query_time = f"{now.strftime('%Y-%m-%d')}%"

        with get_db_conn() as conn:
            c = conn.cursor()
            # 检查 orders 表（买入/观望）或 summaries 表（分析结果）
            c.execute('''
                SELECT 
                    (SELECT count(*) FROM orders WHERE config_id = ? AND timestamp >= ?) +
                    (SELECT count(*) FROM summaries WHERE config_id = ? AND timestamp >= ?) as cnt
            ''', (config_id, query_time, config_id, query_time))
            row = c.fetchone()
            return row[0] > 0
    except Exception as e:
        logger.error(f"❌ 检查DCA记录失败: {e}")
        return False

def is_time_to_run(config, now):
    """
    统一判断某个配置是否应该在当前时刻运行
    """
    mode = config.get('mode', 'STRATEGY').upper()
    config_id = config.get('config_id')
    
    # 1. 现货定投模式 (SPOT_DCA)
    if mode == 'SPOT_DCA':
        freq = config.get('dca_freq', '1d').lower() # '1d' or '1w'
        dca_time = config.get('dca_time', '08:00')
        target_hour = int(str(dca_time).split(':')[0])
        
        # 小时检查
        if now.hour != target_hour:
            return False
            
        # 周检查 (如果是 1w)
        if freq == '1w':
            target_weekday = int(config.get('dca_weekday', 0)) # 0=周一, 6=周日
            if now.weekday() != target_weekday:
                return False
        
        # 防重复检查
        if check_dca_executed(config_id, now, freq):
            return False
            
        return True

    # 2. 策略模式 (STRATEGY) - 仅限整点
    if mode == 'STRATEGY':
        # 容差 ±5分钟
        if 5 < now.minute < 55:
            return False
        return True

    # 3. 实盘模式 (REAL) - 默认每轮心跳都运行 (由 get_next_run_settings 控制 15m 一次)
    if mode == 'REAL':
        return True
        
    return False

def process_single_config(config):
    """
    单个 Agent 任务处理
    """
    config_id = config.get('config_id', 'unknown')
    symbol = config.get('symbol')
    mode = config.get('mode', 'STRATEGY').upper()
    now = datetime.now(TZ_CN)

    if not symbol: return
    if not is_time_to_run(config, now): return

    logger.info(f"⏳ [{config_id}] 满足触发条件 ({mode})，开始执行 Agent 任务...")

    try:
        run_agent_for_config(config)
    except Exception as e:
        logger.error(f"❌ Error executing Agent [{config_id}]: {e}")


def get_next_run_settings(active_configs):
    """
    决定调度器的心跳频率
    """
    if not active_configs:
        return 60, "无活跃配置-休眠 (1h)"

    # 只要有一个是实盘模式，系统保持 15m 心跳
    has_real_mode = any(c.get('mode', 'STRATEGY').upper() == 'REAL' for c in active_configs)
    
    if has_real_mode:
        return 15, "🚀 活跃实盘模式 (15m)"
    else:
        # 全是策略或定投，每小时醒来检查一次即可
        return 60, "🔵 策略/定投模式 (1h)"


def wait_until_next_slot(interval_minutes, delay_seconds=10):
    now = datetime.now().astimezone(TZ_CN)
    now_ts = now.timestamp()
    interval_seconds = interval_minutes * 60

    next_ts = ((now_ts // interval_seconds) + 1) * interval_seconds
    next_run_time_ts = next_ts + delay_seconds

    next_run_time = datetime.fromtimestamp(next_run_time_ts).astimezone(TZ_CN)
    sleep_seconds = next_run_time_ts - now_ts

    logger.info(f"⏳ [调度器] 状态: 待机中 | 心跳间隔: {interval_minutes}m")
    logger.info(f"   |-- 下次唤醒: {next_run_time.strftime('%H:%M:%S')}")

    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def job():
    configs = global_config.get_all_symbol_configs()
    active_configs = [c for c in configs if c.get('enabled', True)]
    
    if not active_configs:
        logger.info("⏳ 没有任何活跃配置 (enabled=true)，跳过本轮执行。")
        return

    logger.info(f"🚀 系统唤醒 (检查 {len(active_configs)}/{len(configs)} 个配置)...")

    # 使用线程池并行处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in active_configs]
        concurrent.futures.wait(futures)

    logger.info(f"本轮执行完毕。")


def run_smart_scheduler():
    logger.info("--- [系统] 智能调度器启动 ---")

    try:
        init_db()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")

    while True:
        try:
            # 重新获取配置以应对热更新
            configs = global_config.get_all_symbol_configs()
            active_configs = [c for c in configs if c.get('enabled', True)]
            
            # 决定心跳频率并等待
            interval, mode_str = get_next_run_settings(active_configs)
            logger.info(f"📅 [模式检测] {mode_str}")
            
            wait_until_next_slot(interval_minutes=interval, delay_seconds=10)
            
            # 重新加载配置并执行
            global_config.reload_config()
            job()

        except Exception as e:
            logger.error(f"❌ 调度主循环异常: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_smart_scheduler()
