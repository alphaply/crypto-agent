import concurrent.futures
import os
import time
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from backend.agent.agent_graph import (
    generate_short_memory_for_config,
    generate_manual_daily_summary,
    get_short_memory_bucket,
    run_agent_for_config,
)
from backend.config import config as global_config
from backend.database import init_db
from backend.utils.logger import setup_logger
from backend.utils.market_data import MarketTool


load_dotenv()

TZ_CN = pytz.timezone(getattr(global_config, "timezone", "Asia/Shanghai"))
logger = setup_logger("MainScheduler")

_last_run_times = {}
_daily_summary_done_date = None
_short_memory_done_buckets = set()


def normalize_dca_freq(raw_freq):
    freq = str(raw_freq or "1d").strip().lower()
    if freq in {"1w", "weekly", "week", "w"}:
        return "1w"
    return "1d"


def parse_dca_time(raw_time):
    text = str(raw_time or "08:00").strip()
    if not text:
        return 8, 0

    try:
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 8, 0

    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    return hour, minute


def check_dca_executed(config_id, now, freq="1d"):
    from backend.database import get_db_conn

    try:
        normalized_freq = normalize_dca_freq(freq)
        if normalized_freq == "1w":
            start_of_week = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            query_time = f"{start_of_week} 00:00:00"
        else:
            query_time = f"{now.strftime('%Y-%m-%d')} 00:00:00"

        with get_db_conn() as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT count(*) as cnt
                FROM orders
                WHERE config_id = ?
                  AND trade_mode = 'SPOT_DCA'
                  AND timestamp >= ?
                """,
                (config_id, query_time),
            )
            row = c.fetchone()
            return row[0] > 0
    except Exception as e:
        logger.error(f"Failed to check DCA execution state: {e}")
        return False


def is_time_to_run(config, now):
    mode = config.get("mode", "STRATEGY").upper()
    config_id = config.get("config_id")

    if mode == "SPOT_DCA":
        freq = normalize_dca_freq(config.get("dca_freq", "1d"))
        target_hour, target_minute = parse_dca_time(config.get("dca_time", "08:00"))

        if now.hour < target_hour or (
            now.hour == target_hour and now.minute < target_minute
        ):
            return False

        if freq == "1w":
            target_weekday = int(config.get("dca_weekday", 0))
            if now.weekday() != target_weekday:
                return False

        last_run = _last_run_times.get(config_id)
        if last_run:
            if freq == "1d" and last_run.date() == now.date():
                return False
            if (
                freq == "1w"
                and last_run.year == now.year
                and last_run.isocalendar()[1] == now.isocalendar()[1]
            ):
                return False

        if check_dca_executed(config_id, now, freq):
            return False

        return True

    default_interval = 60 if mode == "STRATEGY" else 15
    interval = int(config.get("run_interval", default_interval))
    if interval < 15:
        interval = 15

    minutes_since_midnight = now.hour * 60 + now.minute

    if minutes_since_midnight % interval == 0:
        last_run = _last_run_times.get(config_id)
        if last_run and last_run.hour == now.hour and last_run.minute == now.minute:
            return False
        return True

    return False


def process_single_config(config):
    config_id = config.get("config_id", "unknown")
    symbol = config.get("symbol")
    mode = config.get("mode", "STRATEGY").upper()
    now = datetime.now(TZ_CN)

    if not symbol:
        return

    if mode == "STRATEGY":
        try:
            m_tool = MarketTool(config_id=config_id)
            m_tool.run_silent_sl_tp()
        except Exception:
            pass

    if not is_time_to_run(config, now):
        return

    logger.info(
        f"[{config_id}] scheduler triggered ({mode}, interval={config.get('run_interval', 'default')})"
    )
    _last_run_times[config_id] = now

    try:
        run_agent_for_config(config)
    except Exception as e:
        logger.error(f"Error executing agent [{config_id}]: {e}")


def wait_until_next_minute():
    now = datetime.now().astimezone(TZ_CN)
    sleep_seconds = 60 - now.second - (now.microsecond / 1000000.0)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def job():
    configs = global_config.get_all_symbol_configs()
    active_configs = [c for c in configs if c.get("enabled", True)]

    if not active_configs:
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_single_config, config) for config in active_configs]
        concurrent.futures.wait(futures)


def run_daily_summary_job():
    global _daily_summary_done_date
    now = datetime.now(TZ_CN)
    today_str = now.strftime("%Y-%m-%d")

    if now.hour >= 2 or _daily_summary_done_date == today_str:
        return

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info(f"[DailySummary] start summarizing {yesterday}")

    configs = global_config.get_all_symbol_configs()
    for config in configs:
        if not config.get("enabled", True):
            continue
        config_id = config["config_id"]
        generate_manual_daily_summary(config_id, yesterday)

    _daily_summary_done_date = today_str
    logger.info(f"[DailySummary] completed summarizing {yesterday}")


def run_short_memory_job():
    now = datetime.now(TZ_CN)
    target_time = now - timedelta(seconds=1)
    bucket_start, _ = get_short_memory_bucket(target_time)
    bucket_key = bucket_start.strftime("%Y-%m-%d %H:%M:%S")

    if now.minute != 0 or now.hour % 4 != 0:
        return
    if bucket_key in _short_memory_done_buckets:
        return

    configs = [cfg for cfg in global_config.get_all_symbol_configs() if cfg.get("enabled", True)]
    for config in configs:
        config_id = config.get("config_id")
        if not config_id:
            continue
        try:
            generate_short_memory_for_config(config_id, now_cn=target_time)
        except Exception as exc:
            logger.error(f"[ShortMemory] failed for {config_id}: {exc}")

    _short_memory_done_buckets.add(bucket_key)
    while len(_short_memory_done_buckets) > 12:
        _short_memory_done_buckets.pop()


def run_scheduler_forever():
    logger.info("[System] scheduler loop started")

    try:
        init_db()
        logger.info("Database initialized for scheduler")
    except Exception as e:
        logger.error(f"Database initialization failed in scheduler: {e}")

    global_config.reload_config()

    while True:
        try:
            wait_until_next_minute()

            if not global_config.enable_scheduler:
                if datetime.now().minute % 10 == 0:
                    logger.info("Scheduler disabled globally, waiting...")
                continue

            run_daily_summary_job()
            run_short_memory_job()
            job()

            if datetime.now().minute % 5 == 0:
                global_config.reload_config()

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
            time.sleep(10)


def scheduler_should_run() -> bool:
    return bool(getattr(global_config, "enable_scheduler", True))
