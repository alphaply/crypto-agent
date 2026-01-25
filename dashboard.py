from flask import Flask, render_template, request
import pandas as pd
import sqlite3
import threading
import time
import math
import json
import os
from database import DB_NAME, init_db
from main_scheduler import job 
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        conn.row_factory = sqlite3.Row 
        
        # 1. 获取该币种下活跃的所有 Agent 的最新一条分析
        agents_query = "SELECT DISTINCT agent_name FROM summaries WHERE symbol = ?"
        agents = [row['agent_name'] for row in conn.execute(agents_query, (symbol,)).fetchall()]
        
        agent_summaries = []
        for agent in agents:
            safe_agent_name = agent if agent else "Unknown"
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
        print(f"Error: {e}")
        return [], [], 0

def get_configured_symbols():
    """解析 .env 中的 SYMBOL_CONFIGS 获取币种列表"""
    configs_str = os.getenv('SYMBOL_CONFIGS', '[]')
    try:
        configs = json.loads(configs_str)
        symbols = [cfg['symbol'] for cfg in configs if 'symbol' in cfg]
        if symbols:
            return symbols
    except Exception as e:
        print(f"Dashboard Config Error: {e}")
    
    # 如果配置为空或出错，返回默认
    return ["BTC/USDT", "ETH/USDT"]

@app.route('/')
def index():
    # 1. 动态获取币种列表
    symbols = get_configured_symbols()
    
    # 2. 获取当前选中的币种（如果 URL 没传，默认选列表第一个）
    symbol = request.args.get('symbol', symbols[0] if symbols else 'BTC/USDT')
    page = int(request.args.get('page', 1))
    per_page = 10
    
    agent_summaries, orders, total_count = get_dashboard_data(symbol, page, per_page)
    
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1

    return render_template(
        'dashboard.html', 
        agent_summaries=agent_summaries, 
        orders=orders, 
        symbols=symbols, 
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        total_orders=total_count
    )

def run_scheduler():
    import schedule
    print("--- [系统] 极简定时器已启动 ---")
    time.sleep(2) 
    job() 
    schedule.every(15).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    init_db() 
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=7860, debug=False)