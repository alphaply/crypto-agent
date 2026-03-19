import sqlite3
import math
from datetime import datetime, timedelta
import pytz
import time
from flask import Blueprint, render_template, request, jsonify, session
from routes.utils import (
    DB_NAME, global_config, get_scheduler_status, get_symbol_specific_status,
    _chat_authed, logger, TZ_CN
)
from database import (
    get_paginated_summaries, get_summary_count, delete_summaries_by_symbol,
    get_balance_history, get_trade_history, clean_financial_data,
    get_active_agents, get_paginated_orders, get_db_conn, get_daily_summaries
)

main_bp = Blueprint('main', __name__)

def verify_admin_action(password, captcha):
    """验证管理员密码与动态验证码"""
    now = time.time()
    lock_until = session.get('lock_until', 0)
    if now < lock_until:
        remain = int(lock_until - now)
        return False, f"尝试次数过多，请在 {remain} 秒后再试", True, 429

    fails = session.get('failed_attempts', 0)
    need_captcha = (fails >= 3)

    if need_captcha:
        expected_captcha = session.get('captcha_answer')
        if not expected_captcha:
            return False, "验证码已过期，请刷新", True, 400
        if captcha != expected_captcha:
            session.pop('captcha_answer', None)
            return False, "验证码错误", True, 403
        session.pop('captcha_answer', None)

    if password != global_config.admin_password:
        fails += 1
        session['failed_attempts'] = fails
        if fails >= 5:
            session['lock_until'] = now + 900
            session['failed_attempts'] = 0
            return False, "错误次数过多，账号已锁定 15 分钟", True, 429
        time.sleep(fails * 0.5)
        return False, f"密码错误 (剩余 {5 - fails} 次尝试)", fails >= 3, 401
    
    session['failed_attempts'] = 0
    session.pop('lock_until', None)
    return True, "", False, 200


def calculate_next_run(config, latest_summary=None):
    """根据配置和最后一次执行时间计算下一次预定运行时间"""
    mode = config.get('mode', 'STRATEGY').upper()
    now = datetime.now(TZ_CN)
    
    if mode in ['REAL', 'STRATEGY']:
        default_interval = 60 if mode == 'STRATEGY' else 15
        interval = int(config.get('run_interval', default_interval))
        if interval < 15: interval = 15

        # 统一逻辑：按整点周期对齐 (以每天 00:00 为基准)
        minutes_since_midnight = now.hour * 60 + now.minute
        # 计算下一个对齐的时间点
        next_total_minutes = ((minutes_since_midnight // interval) + 1) * interval

        # 考虑到可能跨天
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_total_minutes)

        return next_run.strftime('%H:%M')        
    elif mode == 'SPOT_DCA':
        # ... (定投逻辑保持不变)
        dca_time_str = config.get('dca_time', '08:00')
        try:
            hour, minute = map(int, dca_time_str.split(':'))
        except:
            hour, minute = 8, 0
            
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        
        if config.get('dca_freq', '1d') == '1w':
            target_weekday = int(config.get('dca_weekday', 0)) # 0=周一
            days_ahead = target_weekday - now.weekday()
            if days_ahead < 0 or (days_ahead == 0 and now > next_run):
                days_ahead += 7
            next_run += timedelta(days=days_ahead)
        else:
            if now > next_run:
                next_run += timedelta(days=1)
                
        return next_run.strftime('%m-%d %H:%M')
        
    return "N/A"

def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        with get_db_conn() as conn:
            # 1. 获取该币种下配置的所有 Agent
            configs = global_config.get_all_symbol_configs()
            symbol_configs = [conf for conf in configs if conf['symbol'] == symbol]
            
            agent_summaries = []
            for config in symbol_configs:
                config_id = config['config_id']
                latest_summary_row = conn.execute(
                    "SELECT * FROM summaries WHERE config_id = ? ORDER BY id DESC LIMIT 1",
                    (config_id,)
                ).fetchone()
                
                
                mode = config.get('mode', 'STRATEGY').upper()
                model_name = config.get('model', 'Unknown')
                
                enabled = config.get('enabled', True)

                latest_summary = None
                if latest_summary_row:
                    latest_summary = dict(latest_summary_row)
                    summary_dict = latest_summary.copy()
                else:
                    # 如果没有历史摘要，创建一个占位符
                    summary_dict = {
                        'config_id': config_id,
                        'agent_name': model_name,
                        'symbol': symbol,
                        'content': "💤 该 Agent 尚未产生任何分析数据。请确保调度器已开启并等待其运行。",
                        'strategy_logic': "暂无逻辑",
                        'timestamp': "N/A",
                        'agent_type': None,
                        'id': -1
                    }
                
                summary_dict['model'] = model_name
                summary_dict['mode'] = mode
                summary_dict['enabled'] = enabled
                summary_dict['next_run'] = calculate_next_run(config, latest_summary)
                
                if mode == 'SPOT_DCA':
                    summary_dict['freq'] = f"{config.get('dca_freq', '1d')} (定投)"
                else:
                    default_int = 60 if mode == 'STRATEGY' else 15
                    interval = config.get('run_interval', default_int)
                    summary_dict['freq'] = f"{interval}m ({'高频' if int(interval) <= 15 else '定期'})"
                    
                summary_dict['leverage'] = global_config.get_leverage(config_id)
                summary_dict['display_name'] = f"{model_name} ({mode})"
                
                # 默认获取第一页订单
                orders, total = get_paginated_orders(config_id, page=1, per_page=10)
                summary_dict['all_orders'] = orders
                summary_dict['order_total'] = total
                summary_dict['order_page'] = 1

                # 每日策略汇总
                summary_dict['daily_summaries'] = get_daily_summaries(config_id, days=7)
                
                agent_summaries.append(summary_dict)

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

    ok, msg, need_captcha, status_code = verify_admin_action(password, captcha)
    if not ok:
        return jsonify({"success": False, "message": msg, "need_captcha": need_captcha}), status_code

    if not symbol:
        return jsonify({"success": False, "message": "缺少币种参数", "need_captcha": False}), 400

    delete_summaries_by_symbol(symbol)
    # 同时清除资金统计数据，让公开看板重新开始采样
    clean_financial_data(symbol)
    return jsonify({"success": True, "message": f"已成功重置 {symbol} 的所有历史及财务统计数据", "need_captcha": False})

@main_bp.route('/api/daily_summary/update', methods=['POST'])
def update_daily_summary_api():
    data = request.json
    password = data.get('password')
    captcha = data.get('captcha', '').upper()
    date_str = data.get('date')
    config_id = data.get('config_id')
    summary_content = data.get('summary')

    ok, msg, need_captcha, status_code = verify_admin_action(password, captcha)
    if not ok:
        return jsonify({"success": False, "message": msg, "need_captcha": need_captcha}), status_code

    if not date_str or not config_id or not summary_content:
        return jsonify({"success": False, "message": "参数不完整", "need_captcha": False}), 400

    try:
        from database import update_daily_summary as db_update_ds
        db_update_ds(date_str, config_id, summary_content)
        return jsonify({"success": True, "message": "每日总结已更新", "need_captcha": False})
    except Exception as e:
        logger.error(f"更新每日总结错误: {e}")
        return jsonify({"success": False, "message": "数据库更新失败", "need_captcha": False}), 500

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

@main_bp.route('/api/daily_summaries')
def get_daily_summaries_api():
    config_id = request.args.get('config_id')
    days = int(request.args.get('days', 7))
    if not config_id:
        return jsonify({"success": False, "message": "Missing config_id"})
    data = get_daily_summaries(config_id, days=days)
    return jsonify({"success": True, "daily_summaries": data})
