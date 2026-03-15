import time
import concurrent.futures
from datetime import datetime, timedelta
import pytz
import os
from dotenv import load_dotenv
from agent.agent_graph import run_agent_for_config, summarize_content
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
        try:
            target_hour = int(str(dca_time).split(':')[0])
            target_minute = int(str(dca_time).split(':')[1])
        except:
            target_hour, target_minute = 8, 0
        
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
    """每日 00:00 执行：汇总昨天每个 agent 的所有 strategy_logic 为一条精炼的每日总结"""
    global _daily_summary_done_date
    now = datetime.now(TZ_CN)

    # 只在 00:00 触发，且每天只执行一次
    if now.hour != 0 or now.minute != 0:
        return
    today_str = now.strftime('%Y-%m-%d')
    if _daily_summary_done_date == today_str:
        return

    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    logger.info(f"📅 [DailySummary] 开始汇总 {yesterday} 的策略逻辑...")

    configs = global_config.get_all_symbol_configs()
    for config in configs:
        if not config.get('enabled', True):
            continue
        config_id = config['config_id']
        symbol = config.get('symbol', 'Unknown')

        try:
            rows = get_pending_daily_summary_data(config_id, yesterday)
            if not rows:
                logger.debug(f"📅 [{config_id}] {yesterday} 无数据，跳过")
                continue

            # 拼接当天所有 strategy_logic
            combined = "\n".join(
                f"[{r['timestamp']}] {r['strategy_logic']}"
                for r in rows if r.get('strategy_logic')
            )
            if not combined.strip():
                continue

            # 使用 LLM 压缩为一段每日总结
            daily_summary = summarize_content(
                f"以下是 {yesterday} 一整天的多轮交易分析逻辑，请汇总为一段200字以内的当日策略行情回顾，"
                f"保留关键趋势判断、核心点位和操作意图的演变过程：\n\n{combined}",
                config
            )

            save_daily_summary(yesterday, symbol, config_id, daily_summary, len(rows))
            logger.info(f"✅ [{config_id}] {yesterday} 每日汇总完成 ({len(rows)} 条来源)")
        except Exception as e:
            logger.error(f"❌ [{config_id}] 每日汇总失败: {e}")

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
