from flask import Flask, render_template, request
import sqlite3
import threading
import math
import json
import os
from datetime import datetime
import pytz
from database import DB_NAME, init_db
from main_scheduler import run_smart_scheduler, get_next_run_settings
from dotenv import load_dotenv
from logger import setup_logger

load_dotenv(dotenv_path='.env', override=True)
app = Flask(__name__)
TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("Dashboard")

def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        conn.row_factory = sqlite3.Row 
        
        # 1. 获取该币种下活跃的所有 Agent 的最新一条分析
        agents_query = "SELECT DISTINCT agent_name FROM summaries WHERE symbol = ?"
        agents = [row['agent_name'] for row in conn.execute(agents_query, (symbol,)).fetchall()]
        
        agent_summaries = []
        for agent in agents:
            latest_summary = conn.execute(
                "SELECT * FROM summaries WHERE symbol = ? AND agent_name = ? ORDER BY id DESC LIMIT 1", 
                (symbol, agent)
            ).fetchone()
            if latest_summary:
                agent_summaries.append(dict(latest_summary))

        # 2. 获取订单
        offset = (page - 1) * per_page
        total_count = conn.execute("SELECT COUNT(*) FROM orders WHERE symbol = ?", (symbol,)).fetchone()[0]
        
        cursor = conn.execute(
            "SELECT * FROM orders WHERE symbol = ? ORDER BY id DESC LIMIT ? OFFSET ?", 
            (symbol, per_page, offset)
        )
        orders = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        return agent_summaries, orders, total_count
    except Exception as e:
        logger.error(f"Error: {e}")
        return [], [], 0

def get_all_configs():
    """读取所有配置的辅助函数"""
    configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
    try:
        if configs_str: configs_str = configs_str.strip()
        configs = json.loads(configs_str)
        return configs
    except:
        return []

def get_configured_symbols():
    configs = get_all_configs()
    symbols = [cfg['symbol'] for cfg in configs if 'symbol' in cfg]
    # 去重
    seen = set()
    unique = []
    for s in symbols:
        if s not in seen:
            unique.append(s)
            seen.add(s)
    if not unique: return ["BTC/USDT", "ETH/USDT"]
    return unique

def get_symbol_specific_status(symbol):
    """
    计算特定币种的当前运行状态和频率
    """
    configs = get_all_configs()
    # 找到当前币种的配置
    target_config = next((c for c in configs if c.get('symbol') == symbol), None)
    
    if not target_config:
        return "未知", "N/A"
        
    mode = target_config.get('mode', 'STRATEGY').upper()
    
    # 获取时间判断频率 (复用调度器的逻辑)
    now = datetime.now(TZ_CN)
    weekday = now.weekday()
    hour = now.hour
    
    freq_text = "Unknown"
    
    # 逻辑：完全复刻 main_scheduler.py 的判断
    if mode == 'REAL':
        mode_text = "🔴 实盘模式 (Real)"
        if weekday == 5: freq_text = "1h (周六休整)"
        elif weekday == 6 and hour < 20: freq_text = "1h (周日白天)"
        else: freq_text = "15m (高频执行)"
    else:
        mode_text = "🔵 策略模式 (Strategy)"
        if weekday >= 5: freq_text = "4h (周末长线)"
        else: freq_text = "1h (工作日标准)"
        
    return mode_text, freq_text

@app.route('/')
def index():
    symbols = get_configured_symbols()
    symbol = request.args.get('symbol', symbols[0] if symbols else 'BTC/USDT')
    page = int(request.args.get('page', 1))
    per_page = 10
    
    agent_summaries, orders, total_count = get_dashboard_data(symbol, page, per_page)
    
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1

    # 1. 获取特定币种的状态 (新增)
    symbol_mode, symbol_freq = get_symbol_specific_status(symbol)

    return render_template(
        'dashboard.html', 
        agent_summaries=agent_summaries, 
        orders=orders, 
        symbols=symbols, 
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        total_orders=total_count,
        # 传给前端的变量改了
        symbol_mode=symbol_mode,
        symbol_freq=symbol_freq
    )

if __name__ == "__main__":
    init_db() 
    threading.Thread(target=run_smart_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=7860, debug=False)