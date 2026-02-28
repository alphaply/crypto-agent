import sqlite3
import math
from flask import Blueprint, render_template, request, jsonify
from routes.utils import (
    DB_NAME, global_config, get_scheduler_status, get_symbol_specific_status,
    _chat_authed, logger
)
from database import (
    get_paginated_summaries, get_summary_count, delete_summaries_by_symbol,
    get_balance_history, get_trade_history, clean_financial_data,
    get_active_agents
)

main_bp = Blueprint('main', __name__)

def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        conn.row_factory = sqlite3.Row 
        
        # 1. 获取该币种下活跃的所有 Agent 的最新一条分析
        # 这里逻辑沿用之前重构后的：按 config_id 聚合
        configs = global_config.get_all_symbol_configs()
        symbol_config_ids = [conf['config_id'] for conf in configs if conf['symbol'] == symbol]
        
        agent_summaries = []
        for config_id in symbol_config_ids:
            latest_summary = conn.execute(
                "SELECT * FROM summaries WHERE config_id = ? ORDER BY id DESC LIMIT 1",
                (config_id,)
            ).fetchone()
            
            if latest_summary:
                summary_dict = dict(latest_summary)
                config = global_config.get_config_by_id(config_id)
                
                if config:
                    summary_dict['model'] = config.get('model', 'Unknown')
                    summary_dict['mode'] = config.get('mode', 'STRATEGY')
                    summary_dict['leverage'] = global_config.get_leverage(config_id)
                    summary_dict['display_name'] = f"{config.get('model', 'Unknown')} ({config.get('mode', 'STRATEGY')})"
                
                # 获取 20 条流水
                full_orders = conn.execute(
                    "SELECT * FROM orders WHERE config_id = ? ORDER BY id DESC LIMIT 20",
                    (config_id,)
                ).fetchall()
                summary_dict['all_orders'] = [dict(o) for o in full_orders]
                agent_summaries.append(summary_dict)

        conn.close()
        return agent_summaries, [], len(agent_summaries)
    except Exception as e:
        logger.error(f"❌ 获取仪表盘数据失败: {e}")
        return [], [], 0

@main_bp.route('/')
def index():
    # 获取配置的币种列表
    configs = global_config.get_all_symbol_configs()
    seen = set()
    symbols = []
    for c in configs:
        s = c.get('symbol')
        if s and s not in seen:
            symbols.append(s)
            seen.add(s)
    
    current_symbol = request.args.get('symbol', symbols[0] if symbols else 'BTC/USDT')
    page = int(request.args.get('page', 1))
    
    agent_summaries, _, _ = get_dashboard_data(current_symbol, page)
    symbol_mode, symbol_freq, symbol_enabled = get_symbol_specific_status(current_symbol)
    scheduler_enabled = get_scheduler_status()

    return render_template(
        'dashboard.html',
        symbols=symbols,
        current_symbol=current_symbol,
        agent_summaries=agent_summaries,
        symbol_mode=symbol_mode,
        symbol_freq=symbol_freq,
        scheduler_enabled=scheduler_enabled,
        symbol_enabled=symbol_enabled
    )

@main_bp.route('/history')
def history_view():
    symbol = request.args.get('symbol', 'BTC/USDT')
    agent_filter = request.args.get('agent', 'ALL')
    page = int(request.args.get('page', 1))
    per_page = 20
    
    summaries = get_paginated_summaries(symbol, page, per_page, agent_name=agent_filter)
    total_count = get_summary_count(symbol, agent_name=agent_filter)
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
    active_agents = get_active_agents(symbol)
    
    return render_template(
        'history.html',
        summaries=summaries,
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        active_agents=active_agents,
        current_agent=agent_filter
    )

@main_bp.route('/api/clean_history', methods=['POST'])
def clean_history():
    if not _chat_authed(): return jsonify({"success": False, "message": "未授权"}), 401
    data = request.json
    symbol = data.get('symbol')
    delete_summaries_by_symbol(symbol)
    return jsonify({"success": True, "message": f"已清空 {symbol} 的所有历史数据"})
