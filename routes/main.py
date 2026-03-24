import os
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
    get_active_agents, get_paginated_orders, get_db_conn, get_daily_summaries,
    get_history_pnl_stats, get_mock_account, get_mock_equity_history
)
from agent.agent_graph import generate_manual_daily_summary

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

def calculate_dca_stats(config_id, force_sync=False):
    """
    计算 SPOT_DCA 模式的定投统计信息。
    完全基于 CCXT API，获取最新资产余额并通过历史成交记录倒推计算均价。
    由于不再依赖数据库，不再需要 force_sync (参数保留以防兼容问题)。
    """
    try:
        from config import config as global_config
        from utils.market_data import MarketTool
        
        cfg = global_config.get_config_by_id(config_id)
        if not cfg: return None
        
        symbol = cfg.get('symbol')
        if not symbol: return None
        
        base_asset = symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT', '')
        
        mt = MarketTool(config_id=config_id)
        
        # 1. 获取当前现货真实余额
        balances = mt.exchange.fetch_balance()
        current_qty = 0
        if base_asset in balances:
            current_qty = float(balances[base_asset].get('total', 0))
        elif base_asset.lower() in balances:
            current_qty = float(balances[base_asset.lower()].get('total', 0))
        elif 'total' in balances and base_asset in balances['total']:
            current_qty = float(balances['total'].get(base_asset, 0))
            
        initial_cost = float(cfg.get('initial_cost', 0))
        initial_qty = float(cfg.get('initial_qty', 0))
        
        # 2. 匹配当前余额的真实成本
        accumulated_qty = 0
        total_cost = 0
        matched_buy_count = 0
        first_buy_ts = None
        last_buy_ts = None
        
        target_qty_to_match = max(0, current_qty - initial_qty)
        
        if target_qty_to_match > 0:
            try:
                # 获取最新的成交记录
                trades = mt.exchange.fetch_my_trades(symbol, limit=1000)
                buy_trades = [t for t in trades if str(t.get('side', '')).lower() == 'buy']
                # 倒序（最新在前），FIFO倒推剩下的持仓成本
                buy_trades.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                
                for t in buy_trades:
                    if accumulated_qty >= target_qty_to_match:
                        break
                        
                    trade_qty = float(t.get('amount', 0))
                    # 去除手续费影响的极小量（如果币安扣除到了基础币种）
                    # 简单处理：全量计入成本计算
                    trade_price = float(t.get('price', 0))
                    
                    remains = target_qty_to_match - accumulated_qty
                    matched_qty = min(trade_qty, remains)
                    matched_cost = matched_qty * trade_price
                    
                    accumulated_qty += matched_qty
                    total_cost += matched_cost
                    matched_buy_count += 1
                    
                    if not last_buy_ts:
                        last_buy_ts = t.get('timestamp')
                    first_buy_ts = t.get('timestamp') # 一直往下刷，最后记录的是最老的有效成交
                        
            except Exception as e:
                logger.warning(f"Fetch my_trades failed for {symbol}: {e}")
                
        # 3. 汇总
        final_qty = accumulated_qty + initial_qty
        final_invested = total_cost + initial_cost
        
        avg_cost = (final_invested / final_qty) if final_qty > 0 else 0
        
        # 格式化时间
        def fmt_ts(ts):
            from datetime import datetime
            return datetime.fromtimestamp(ts/1000).strftime('%Y-%m-%d %H:%M:%S') if ts else None
            
        return {
            "buy_count": matched_buy_count,
            "total_invested": round(final_invested, 2),
            "total_qty": round(final_qty, 6),
            "avg_cost": round(avg_cost, 4),
            "dca_amount_per": cfg.get('dca_amount', cfg.get('dca_budget', 0)),
            "has_legacy": initial_qty > 0,
            "first_buy": fmt_ts(first_buy_ts),
            "last_buy": fmt_ts(last_buy_ts),
            "actual_balance": round(current_qty, 6)
        }
    except Exception as e:
        import traceback
        logger.error(f"Error calculating CCXT DCA stats for {config_id}: {e}\n{traceback.format_exc()}")
        return None

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
                    summary_dict['dca_stats'] = calculate_dca_stats(config_id)
                else:
                    default_int = 60 if mode == 'STRATEGY' else 15
                    interval = config.get('run_interval', default_int)
                    summary_dict['freq'] = f"{interval}m ({'高频' if int(interval) <= 15 else '定期'})"
                    
                summary_dict['leverage'] = global_config.get_leverage(config_id)
                summary_dict['display_name'] = f"{model_name} ({mode})"
                
                # 默认获取第一页订单
                orders, total = get_paginated_orders(config_id, page=1, per_page=20)
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
        summaries = get_paginated_summaries(symbol, page, per_page, config_id=agent_filter)
        total_count = get_summary_count(symbol, config_id=agent_filter)
        total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
        active_agents = get_active_agents(symbol)
        pnl_stats = get_history_pnl_stats(symbol, config_id=agent_filter)
        
        # 获取模拟账本信息与资金曲线
        mock_config_id = agent_filter if agent_filter != 'ALL' else ""

        agent_mode = 'STRATEGY'
        if agent_filter != 'ALL':
            cfg = global_config.get_config_by_id(agent_filter)
            if cfg:
                agent_mode = cfg.get('mode', 'STRATEGY').upper()

        # 只有在策略模式或全部查看时，才展示模拟盘的图表
        if agent_mode == 'STRATEGY' or agent_filter == 'ALL':
            mock_acc = get_mock_account(mock_config_id, symbol)
            mock_chart_data = get_mock_equity_history(mock_config_id)
        else:
            mock_acc = None
            mock_chart_data = []

        # 实盘模式资金曲线 (从 balance_history 按天聚合)
        real_chart_data = []
        real_balance = None
        if agent_mode == 'REAL' and agent_filter != 'ALL':
            try:
                with get_db_conn() as conn:
                    rows = conn.execute("""
                        SELECT day, total_equity FROM (
                            SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                                   row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                            FROM balance_history WHERE symbol = ?
                        ) WHERE rn = 1 ORDER BY day ASC
                    """, (symbol,)).fetchall()
                    real_chart_data = [{"date": r["day"], "equity": r["total_equity"]} for r in rows]
            except Exception as e:
                logger.warning(f"Failed to load real chart data: {e}")

            # 实时获取交易所余额 (与 Dashboard 仓位卡片逻辑一致)
            try:
                from utils.market_data import MarketTool
                mt = MarketTool(config_id=agent_filter)
                bal = mt.exchange.fetch_balance()
                real_balance = float(bal.get('USDT', {}).get('total', 0) or
                                    bal.get('total', {}).get('USDT', 0) or 0)
            except Exception as e:
                logger.warning(f"Failed to fetch live balance for REAL mode: {e}")

        # SPOT_DCA 模式统计 (成本价、累计投入、持仓数量)
        dca_stats = None
        if agent_mode == 'SPOT_DCA' and agent_filter != 'ALL':
            dca_stats = calculate_dca_stats(agent_filter)

    except Exception as e:
        logger.error(f"Failed to load history page: symbol={symbol}, agent={agent_filter}, page={page}, error={e}")
        summaries = []
        total_count = 0
        total_pages = 1
        active_agents = []
        pnl_stats = {"total_trades": 0, "total_pnl": 0, "win_rate": 0, "win_count": 0, "lose_count": 0}
        mock_acc = None
        mock_chart_data = []
        agent_mode = 'STRATEGY'
        real_chart_data = []
        real_balance = None
        dca_stats = None

    return render_template(
        'history.html',
        summaries=summaries,
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        total_count=total_count,
        active_agents=active_agents,
        current_agent=agent_filter,
        pnl_stats=pnl_stats,
        mock_acc=mock_acc,
        mock_chart_data=mock_chart_data,
        agent_mode=agent_mode,
        real_chart_data=real_chart_data,
        real_balance=real_balance,
        dca_stats=dca_stats
    )
@main_bp.route('/chat')
def chat_view():
    return render_template('chat.html', authed=_chat_authed())

@main_bp.route('/api/generate_daily_summary', methods=['POST'])
def manual_generate_summary():
    data = request.json
    password = data.get('password')
    captcha = data.get('captcha', '').upper()
    config_id = data.get('config_id')
    date_str = data.get('date') # YYYY-MM-DD
    
    ok, msg, need_captcha, status_code = verify_admin_action(password, captcha)
    if not ok:
        return jsonify({"success": False, "error": msg, "need_captcha": need_captcha}), status_code
        
    if not config_id or not date_str:
        return jsonify({"success": False, "error": "Missing params", "need_captcha": False}), 400
        
    success = generate_manual_daily_summary(config_id, date_str)
    return jsonify({"success": success, "need_captcha": False})

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
    per_page = int(request.args.get('per_page', 20))
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
