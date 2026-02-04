from flask import Flask, render_template, request,jsonify
import sqlite3
import threading
import math
import json
import os
from datetime import datetime
import pytz
from database import (
    DB_NAME, init_db, 
    get_paginated_summaries, get_summary_count, delete_summaries_by_symbol,
    get_balance_history, get_trade_history, clean_financial_data
)
from main_scheduler import run_smart_scheduler, get_next_run_settings
from dotenv import load_dotenv
from utils.logger import setup_logger

load_dotenv(dotenv_path='.env', override=True)
app = Flask(__name__)
TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("Dashboard")

def get_scheduler_status():
    """获取调度器状态，根据环境变量决定是否运行调度器"""
    scheduler_enabled = os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true'
    return scheduler_enabled

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
        freq_text = "15m (高频执行)"
    else:
        mode_text = "🔵 策略模式 (Strategy)"
        freq_text = "1h (低频执行)"
        
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
    
    # 2. 获取调度器状态
    scheduler_enabled = get_scheduler_status()

    # 获取资金曲线数据 (新增)
    balance_history = get_balance_history(symbol, limit=200)
    
    # 获取历史成交记录 (新增)
    trade_history = get_trade_history(symbol, limit=50)

    # 处理资金曲线数据给前端 Chart.js 使用
    chart_labels = [row['timestamp'][5:16] for row in balance_history] # 只取 MM-DD HH:MM
    chart_data = [row['total_equity'] for row in balance_history]

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
        symbol_freq=symbol_freq,
        scheduler_enabled=scheduler_enabled,
        balance_history=balance_history,
        trade_history=trade_history,
        chart_labels=chart_labels,
        chart_data=chart_data,
    )



@app.route('/history')
def history_view():
    symbol = request.args.get('symbol', 'BTC/USDT')
    page = int(request.args.get('page', 1))
    per_page = 10 # 每页显示10条分析
    
    summaries = get_paginated_summaries(symbol, page, per_page)
    total_count = get_summary_count(symbol)
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
    
    return render_template(
        'history.html', # <--- 我们将创建这个新模板
        summaries=summaries,
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages
    )

# 3. 新增路由：删除历史 (API)
@app.route('/api/clean_history', methods=['POST'])
def clean_history():
    data = request.json
    password = data.get('password')
    symbol = data.get('symbol')
    
    # 验证密码
    admin_pass = os.getenv('ADMIN_PASSWORD')
    if not admin_pass:
        return jsonify({'success': False, 'message': '服务端未配置 ADMIN_PASSWORD'})
        
    if password != admin_pass:
        return jsonify({'success': False, 'message': '密码错误，拒绝操作'})
        
    try:
        # 删除分析记录
        count_summary = delete_summaries_by_symbol(symbol)
        
        # 删除资金和成交记录 (新增)
        count_financial = clean_financial_data(symbol)
        
        logger.info(f"🗑️ [Dashboard] Cleaned all data for {symbol}")
        return jsonify({'success': True, 'message': f'已删除 {count_summary} 条分析, {count_financial} 条财务记录'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/scheduler-status', methods=['GET'])
def get_scheduler_status_api():
    """API接口：返回调度器状态"""
    status = get_scheduler_status()
    return jsonify({"enabled": status})


@app.route('/api/toggle-scheduler', methods=['POST'])
def toggle_scheduler():
    """API接口：切换调度器状态"""
    data = request.json
    enable = data.get('enable', None)
    if enable is not None:
        # 注意：这里只是模拟设置，实际需要重启调度器
        logger.info(f"调度器状态切换请求: {'启用' if enable else '禁用'}")
        return jsonify({"success": True, "enabled": enable})
    else:
        return jsonify({"success": False, "message": "参数错误"})
    

if __name__ == "__main__":
    init_db() 
    # 检查是否启用调度器
    if get_scheduler_status():
        scheduler_thread = threading.Thread(target=run_smart_scheduler, daemon=True)
        scheduler_thread.start()
        print("✅ 定时任务已启动")
    else:
        print("❌ 定时任务已被禁用，仅运行网页服务")
    app.run(host='0.0.0.0', port=7860, debug=False)