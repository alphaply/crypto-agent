import sqlite3
import math
from flask import Blueprint, render_template, request, jsonify, session
from routes.utils import (
    DB_NAME, global_config, get_scheduler_status, get_symbol_specific_status,
    _chat_authed, logger
)
from database import (
    get_paginated_summaries, get_summary_count, delete_summaries_by_symbol,
    get_balance_history, get_trade_history, clean_financial_data,
    get_active_agents, get_paginated_orders
)

main_bp = Blueprint('main', __name__)

def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        conn.row_factory = sqlite3.Row 
        
        # 1. 获取该币种下活跃的所有 Agent 的最新一条分析
        configs = global_config.get_all_symbol_configs()
        symbol_config_ids = [conf['config_id'] for conf in configs if conf['symbol'] == symbol]
        
        agent_summaries = []
        for config_id in symbol_config_ids:
            latest_summary = conn.execute(
                "SELECT * FROM summaries WHERE config_id = ? ORDER BY id DESC LIMIT 1",
                (config_id,)
            ).fetchone()
            
            config = global_config.get_config_by_id(config_id)
            model_name = config.get('model', 'Unknown')

            if latest_summary:
                summary_dict = dict(latest_summary)
                summary_dict['model'] = model_name
                summary_dict['mode'] = config.get('mode', 'STRATEGY')
                summary_dict['leverage'] = global_config.get_leverage(config_id)
                summary_dict['display_name'] = f"{model_name} ({config.get('mode', 'STRATEGY')})"
                
                # 默认获取第一页订单
                orders, total = get_paginated_orders(config_id, page=1, per_page=10)
                summary_dict['all_orders'] = orders
                summary_dict['order_total'] = total
                summary_dict['order_page'] = 1
                
                agent_summaries.append(summary_dict)

        conn.close()
        return agent_summaries, [], len(agent_summaries)
    except Exception as e:
        logger.error(f"❌ 获取仪表盘数据失败: {e}")
        return [], [], 0

@main_bp.route('/')
def index():
    # ... (rest of the route logic)
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
    try:
        page = int(request.args.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    per_page = 20

    try:
        summaries = get_paginated_summaries(symbol, page, per_page, agent_name=agent_filter)
        total_count = get_summary_count(symbol, agent_name=agent_filter)
        total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
        active_agents = get_active_agents(symbol)
    except Exception as e:
        logger.error(f"Failed to load history page: symbol={symbol}, agent={agent_filter}, page={page}, error={e}")
        summaries = []
        total_count = 0
        total_pages = 1
        active_agents = []
    
    return render_template(
        'history.html',
        summaries=summaries,
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        total_count=total_count,
        active_agents=active_agents,
        current_agent=agent_filter
    )

@main_bp.route('/chat')
def chat_view():
    return render_template('chat.html', authed=_chat_authed())

@main_bp.route('/api/clean_history', methods=['POST'])
def clean_history():
    data = request.json
    password = data.get('password')
    captcha = data.get('captcha', '').upper()
    symbol = data.get('symbol')

    # 1. 验证码校验
    expected_captcha = session.get('captcha_answer')
    if not expected_captcha or captcha != expected_captcha:
        session.pop('captcha_answer', None)
        return jsonify({"success": False, "message": "验证码错误"}), 400
    session.pop('captcha_answer', None)

    # 2. 密码校验
    if password != global_config.admin_password:
        return jsonify({"success": False, "message": "密码错误"}), 401

    if not symbol:
        return jsonify({"success": False, "message": "缺少币种参数"}), 400

    delete_summaries_by_symbol(symbol)
    # 同时清除资金统计数据，让公开看板重新开始采样
    clean_financial_data(symbol)
    return jsonify({"success": True, "message": f"已成功重置 {symbol} 的所有历史及财务统计数据"})

@main_bp.route('/stats/public')
def public_stats_view():
    configs = global_config.get_all_symbol_configs()
    seen = set()
    symbols = []
    for c in configs:
        s = c.get('symbol')
        if s and s not in seen:
            symbols.append(s)
            seen.add(s)
    current_symbol = request.args.get('symbol', symbols[0] if symbols else 'BTC/USDT')
    return render_template('stats_public.html', symbols=symbols, current_symbol=current_symbol)

@main_bp.route('/api/orders')
def get_orders_api():
    config_id = request.args.get('config_id')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))
    if not config_id:
        return jsonify({"success": False, "message": "Missing config_id"})
    
    orders, total = get_paginated_orders(config_id, page, per_page)
    return jsonify({
        "success": True,
        "orders": orders,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": math.ceil(total / per_page)
    })
