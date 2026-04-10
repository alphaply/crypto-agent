import time
import concurrent.futures
from datetime import datetime, timedelta
import pytz
import os
from dotenv import load_dotenv
from agent.agent_graph import run_agent_for_config, summarize_content, generate_manual_daily_summary
from utils.market_data import MarketTool
from utils.logger import setup_logger
from config import config as global_config
from database import init_db, get_pending_daily_summary_data, save_daily_summary

# 加载环境变量 (.env 文件)
load_dotenv()

# 设置时区
TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))

# 初始化logger
logger = setup_logger("MainScheduler")

# 记录每个 Agent 上次运行的时间，用于频率控制
_last_run_times = {}
# 防止每日汇总任务在同一天重复执行
_daily_summary_done_date = None


def normalize_dca_freq(raw_freq):
    """将不同写法归一到 1d / 1w。"""
    freq = str(raw_freq or '1d').strip().lower()
    if freq in {'1w', 'weekly', 'week', 'w'}:
        return '1w'
    return '1d'


def parse_dca_time(raw_time):
    """兼容 HH / HH:MM / HH:MM:SS 等格式。"""
    text = str(raw_time or '08:00').strip()
    if not text:
        return 8, 0

    try:
        parts = text.split(':')
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 8, 0

    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    return hour, minute

# ==========================================
# 1. 频率与触发逻辑优化
# ==========================================

def check_dca_executed(config_id, now, freq='1d'):
    """
    检查指定周期内是否已执行过定投（通过检查是否有决策记录或分析摘要）
    """
    from database import get_db_conn
    try:
        normalized_freq = normalize_dca_freq(freq)
        if normalized_freq == '1w':
            start_of_week = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
            query_time = f"{start_of_week} 00:00:00"
        else:
            query_time = f"{now.strftime('%Y-%m-%d')} 00:00:00"

        with get_db_conn() as conn:
            c = conn.cursor()
            # 仅检查 DCA 执行记录，避免 chat/其它场景写入 summaries 造成误判。
            c.execute('''
                SELECT count(*) as cnt
                FROM orders
                WHERE config_id = ?
                  AND trade_mode = 'SPOT_DCA'
                  AND timestamp >= ?
            ''', (config_id, query_time))
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
        freq = normalize_dca_freq(config.get('dca_freq', '1d'))
        target_hour, target_minute = parse_dca_time(config.get('dca_time', '08:00'))
        
        # 放宽触发条件：只要到达或超过设定的时间，即可触发（避免错过精确心跳而漏执行）
        if now.hour < target_hour or (now.hour == target_hour and now.minute < target_minute):
            return False
            
        # 周检查 (如果是 1w)
        if freq == '1w':
            target_weekday = int(config.get('dca_weekday', 0)) # 0=周一, 6=周日
            if now.weekday() != target_weekday:
                return False
        
        # 防重复检查 (1): 内存级别检查，防止同周期内多次触发 (同日/同周)
        last_run = _last_run_times.get(config_id)
        if last_run:
            if freq == '1d' and last_run.date() == now.date():
                return False
            if freq == '1w' and last_run.year == now.year and last_run.isocalendar()[1] == now.isocalendar()[1]:
                return False

        # 防重复检查 (2): 数据库级别检查，确保该周期内没有重复执行
        if check_dca_executed(config_id, now, freq):
            return False
            
        return True

    # 2. 策略模式 (STRATEGY) 或 实盘模式 (REAL)
    # 使用自定义运行周期 (run_interval)，单位分钟
    default_interval = 60 if mode == 'STRATEGY' else 15
    interval = int(config.get('run_interval', default_interval))
    if interval < 15: interval = 15 # 强制最低 15 分钟保护

    # 检查当前分钟是否是该周期的对齐点 (以每天 00:00 为基准)
    minutes_since_midnight = now.hour * 60 + now.minute
    
    if minutes_since_midnight % interval == 0:
        # 防重复检查：确保在同一分钟内只触发一次
        last_run = _last_run_times.get(config_id)
        if last_run and last_run.hour == now.hour and last_run.minute == now.minute:
            return False
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

    # --- [Phase 3] 静默辅助监控: 每分钟检测模拟盘止盈止损 ---
    if mode == 'STRATEGY':
        try:
            m_tool = MarketTool(config_id=config_id)
            m_tool.run_silent_sl_tp()
        except: pass

    if not is_time_to_run(config, now): return

    logger.info(f"🚀 [{config_id}] 满足触发条件 ({mode}, Interval: {config.get('run_interval','Default')})，开始执行...")
    
    # 更新最后运行时间
    _last_run_times[config_id] = now

    try:
        run_agent_for_config(config)
    except Exception as e:
        logger.error(f"❌ Error executing Agent [{config_id}]: {e}")


def wait_until_next_minute():
    """
    精确等待到下一分钟的开始（如 XX:XX:00）
    """
    now = datetime.now().astimezone(TZ_CN)
    sleep_seconds = 60 - now.second - (now.microsecond / 1000000.0)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def job():
    configs = global_config.get_all_symbol_configs()
    active_configs = [c for c in configs if c.get('enabled', True)]
    
    if not active_configs:
        return

    # 使用线程池并行处理
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in active_configs]
        concurrent.futures.wait(futures)


def run_daily_summary_job():
    """每日执行：汇总昨天每个 agent 的所有 strategy_logic 为一条精炼的每日总结"""
    global _daily_summary_done_date
    now = datetime.now(TZ_CN)
    today_str = now.strftime('%Y-%m-%d')

    # 只要进入新的一天，且在凌晨 0-2 点之间（作为一个窗口），且今天还没跑过
    if now.hour >= 2 or _daily_summary_done_date == today_str:
        return

    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    logger.info(f"📅 [DailySummary] 开始汇总 {yesterday} 的策略逻辑...")

    configs = global_config.get_all_symbol_configs()
    for config in configs:
        if not config.get('enabled', True):
            continue
        config_id = config['config_id']
        generate_manual_daily_summary(config_id, yesterday)
        
    _daily_summary_done_date = today_str
    logger.info(f"📅 [DailySummary] {yesterday} 全部汇总完成")


def run_smart_scheduler():
    logger.info("--- [系统] 智能调度器启动 (1分钟高频心跳模式) ---")

    try:
        init_db()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")

    # 启动时执行一次热加载
    global_config.reload_config()

    while True:
        try:
            # 1. 等待到下一分钟开始
            wait_until_next_minute()
            
            # 2. 检查全局调度开关
            if not global_config.enable_scheduler:
                if datetime.now().minute % 10 == 0: # 每10分钟提示一次
                    logger.info("💤 调度器全局已禁用，待机中...")
                continue

            # 3. 每日汇总检查（00:00 时执行）
            run_daily_summary_job()

            # 4. 执行任务
            job()
            
            # 4. 周期性重载配置（每5分钟重载一次，或依赖 job 内部实时获取）
            if datetime.now().minute % 5 == 0:
                global_config.reload_config()

        except Exception as e:
            logger.error(f"❌ 调度主循环异常: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run_smart_scheduler()
