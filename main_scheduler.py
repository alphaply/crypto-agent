import time
import concurrent.futures
from datetime import datetime
import pytz
from dotenv import load_dotenv
from agent.agent_graph import run_agent_for_config
from utils.logger import setup_logger
from config import config as global_config
from database import init_db

# 加载环境变量 (.env 文件)
load_dotenv()

# 设置时区
TZ_CN = pytz.timezone('Asia/Shanghai')

# 初始化logger
logger = setup_logger("MainScheduler")

# ==========================================
# 1. 硬编码配置 (保底配置)
# ==========================================
DEFAULT_SYMBOL_CONFIGS = '[]'


def get_all_configs():
    """
    获取配置（使用统一配置管理）
    """
    try:
        return global_config.get_all_symbol_configs()
    except Exception as e:
        logger.error(f"❌ 配置获取失败: {e}")
        return []


# ==========================================
# 辅助函数: 检查今日是否已执行过定投
# ==========================================
def check_dca_executed_today(config_id, date_str):
    from database import get_db_conn
    try:
        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute('''
                SELECT count(*) as cnt FROM orders 
                WHERE config_id = ? AND timestamp LIKE ? AND reason LIKE '%Spot DCA daily buy%'
            ''', (config_id, f"{date_str}%"))
            row = c.fetchone()
            return row['cnt'] > 0
    except Exception as e:
        logger.error(f"❌ 检查DCA记录失败: {e}")
        return False

def process_single_config(config):
    """
    单线程任务
    """
    config_id = config.get('config_id', 'unknown')
    symbol = config.get('symbol')
    mode = config.get('mode', 'STRATEGY').upper()

    if not symbol: return

    # ==========================================
    # 现货定投模式：每日指定时间执行一次
    # ==========================================
    if mode == 'SPOT_DCA':
        try:
            now = datetime.now(TZ_CN)
            today_str = now.strftime('%Y-%m-%d')
            target_hour = int(str(config.get('dca_time', '8')).split(':')[0])
            
            # 只有在设定的整点小时内才执行
            if now.hour == target_hour:
                # 检查数据库中今天是否已经挂单
                if check_dca_executed_today(config_id, today_str):
                    return
                
                logger.info(f"⏳ [{config_id}] 触发每日现货定投 Agent 任务...")
                # 记录一个空日志或者用一个特殊字段保证 check_dca_executed_today 不会重复触发
                from database import save_order_log
                # 保存一条特殊记录防止重复触发 (假定状态为 INIT，虽然订单还没成交)
                # 这个机制依赖于真实下单后也会带有 'Spot DCA daily buy'。
                # 更好的做法是在这里运行 agent。
                
                # 为了防止 Agent 运行慢导致同一小时内重复触发，我们可以简单用内存或数据库加锁
                # 在此，由于 check_dca_executed_today 检查的是真实订单，如果 Agent 跑完才下订单，中途可能重复。
                # 所以我们先插入一条 pending 的执行记录，或者这里让 Agent 执行完毕后自然带有记录。
                # 为简便，这里直接信任 Agent 执行
                
                run_agent_for_config(config)
                
                # 为了防止在 Agent 执行的 1-2 分钟内再次被调度器调度到（心跳可能 1m 一次，不过当前最快 15m）
                # 这里不需特殊处理，因为如果是 15m 一次，同一个小时可能触发 4 次。我们需要在 Agent 外面防抖。
                # 因此保存一条虚拟的“执行完成”记录：
                save_order_log(f"DCA-TRIGGER-{int(now.timestamp())}", symbol, config_id, 'trigger', 0, 0, 0, "Spot DCA daily buy triggered", trade_mode="REAL", config_id=config_id)
                
        except Exception as e:
            logger.error(f"❌ Error [{config_id}] SPOT_DCA: {e}")
        return

    # ==========================================
    # 策略模式：严格限制在整点运行
    # ==========================================
    # 如果是 STRATEGY 模式，我们只允许在整点 (XX:00) 附近运行。
    # 这样即使调度器因为实盘币种每 15分钟 唤醒了一次，
    # 策略币种在 15分、30分、45分 的时候也会自动跳过。
    if mode == 'STRATEGY':
        now_min = datetime.now(TZ_CN).minute
        # 容差 ±5分钟 (比如 09:55 - 10:05 之间算整点)
        if 5 < now_min < 55:
            # logger.info(f"⏳ [{config_id}] {symbol} 跳过 (当前 {now_min}分，非整点)")
            return

    try:
        run_agent_for_config(config)
    except Exception as e:
        logger.error(f"❌ Error [{config_id}] {symbol}: {e}")


def get_next_run_settings():
    """
    决定调度器的“心跳”频率
    逻辑：
    - 只要有实盘 (REAL) -> 15分钟一次
    - 全是策略 (STRATEGY) -> 1小时一次
    """
    configs = get_all_configs()

    if not configs:
        return 60, "无配置-待机"

    # 检查是否包含实盘
    real_coins = [c['symbol'] for c in configs if c.get('mode', 'STRATEGY').upper() == 'REAL']
    has_real_mode = len(real_coins) > 0

    if has_real_mode:
        # 只要有一个是实盘，整个系统必须保持高频心跳
        return 15, "🚀 混合/实盘模式 (15m)"
    else:
        # 全是策略，只需要每小时醒来一次
        return 60, "🔵 纯策略模式 (1h)"


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
    configs = get_all_configs()
    # 过滤掉已禁用的配置
    active_configs = [c for c in configs if c.get('enabled', True)]
    
    if not active_configs:
        logger.info("⏳ 没有任何活跃配置 (enabled=true)，跳过本轮执行。")
        return

    logger.info(f"🚀 系统唤醒 (检查 {len(active_configs)}/{len(configs)} 个配置)...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in active_configs]
        concurrent.futures.wait(futures)

    logger.info(f"本轮执行完毕。")


def run_smart_scheduler():
    logger.info("--- [系统] 智能调度器启动 ---")

    # 显式初始化数据库，确保表结构完整
    try:
        init_db()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")

    # 打印一次当前配置
    configs = get_all_configs()
    active_configs = [c for c in configs if c.get('enabled', True)]
    real = [c['symbol'] for c in active_configs if c.get('mode', 'STRATEGY').upper() == 'REAL']
    strat = [c['symbol'] for c in active_configs if c.get('mode', 'STRATEGY').upper() != 'REAL']

    logger.info(f"📊 活跃实盘组: {real}")
    logger.info(f"📊 活跃策略组: {strat}")
    logger.info(f"📊 已禁用组: {[c['symbol'] for c in configs if not c.get('enabled', True)]}")

    while True:
        try:
            # 重新获取配置以应对热更新
            configs = get_all_configs()
            active_configs = [c for c in configs if c.get('enabled', True)]
            
            # 决定心跳频率 (基于活跃配置)
            if not active_configs:
                interval, mode_str = 60, "无活跃配置-休眠 (1h)"
            else:
                has_real_mode = any(c.get('mode', 'STRATEGY').upper() == 'REAL' for c in active_configs)
                if has_real_mode:
                    interval, mode_str = 15, "🚀 活跃实盘模式 (15m)"
                else:
                    interval, mode_str = 60, "🔵 活跃策略模式 (1h)"

            logger.info(f"📅 [模式检测] {mode_str}")
            wait_until_next_slot(interval_minutes=interval, delay_seconds=10)
            job()

        except Exception as e:
            logger.error(f"❌ 调度异常: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_smart_scheduler()
