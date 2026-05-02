import os
import sqlite3
import math
from datetime import datetime, timedelta
import pytz
import time
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from routes.utils import (
    DB_NAME, global_config, get_scheduler_status, get_symbol_specific_status,
    _chat_authed, _require_admin_auth_api, _chat_password, logger, TZ_CN
)
from database import (
    get_paginated_summaries, get_summary_count, delete_summaries_by_symbol,
    get_balance_history, get_trade_history, clean_financial_data,
    get_active_agents, get_paginated_orders, get_db_conn, get_daily_summaries,
    get_history_pnl_stats, get_mock_account, get_mock_equity_history,
    save_trade_history, update_order_fill_status, upsert_spot_order_fill,
    save_dca_daily_snapshot, get_dca_daily_snapshot_history
)
from agent.agent_graph import generate_manual_daily_summary

main_bp = Blueprint('main', __name__)

DCA_STATS_CACHE = {}
DCA_STATS_CACHE_TTL = 300


def _resolve_symbol(default_symbol='BTC/USDT'):
    raw = (request.args.get('symbol') or '').strip()
    return raw or default_symbol


def _redirect_if_empty_symbol(endpoint):
    if 'symbol' not in request.args:
        return None
    if (request.args.get('symbol') or '').strip():
        return None

    args = request.args.to_dict(flat=True)
    args.pop('symbol', None)
    return redirect(url_for(endpoint, **args))

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

    expected_password = _chat_password() or global_config.admin_password
    if not expected_password:
        return False, "服务端未配置管理员密码", False, 500

    if password != expected_password:
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
        cache_key = str(config_id)
        now_ts = time.time()
        if not force_sync:
            cached = DCA_STATS_CACHE.get(cache_key)
            if cached and now_ts - cached['timestamp'] < DCA_STATS_CACHE_TTL:
                return cached['data']

        from config import config as global_config
        from utils.market_data import MarketTool

        cfg = global_config.get_config_by_id(config_id)
        if not cfg:
            return None

        symbol = cfg.get('symbol')
        if not symbol:
            return None

        base_asset = symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT', '')
        initial_cost = float(cfg.get('initial_cost', 0) or 0)
        initial_qty = float(cfg.get('initial_qty', 0) or 0)
        mt = MarketTool(config_id=config_id)

        # 1) 先同步 OPEN 订单状态，确保系统自动识别已成交/部分成交/撤单
        try:
            with get_db_conn() as conn:
                open_rows = conn.execute(
                    '''
                    SELECT order_id
                    FROM orders
                    WHERE config_id = ?
                      AND trade_mode = 'SPOT_DCA'
                      AND status = 'OPEN'
                    ORDER BY id DESC
                    LIMIT 100
                    ''',
                    (config_id,),
                ).fetchall()

            for row in open_rows:
                order_id = str(row['order_id'])
                try:
                    od = mt.exchange.fetch_order(order_id, symbol)
                    exch_status = str(od.get('status', '') or '').lower()
                    filled_qty = float(od.get('filled', 0) or 0)
                    filled_cost = float(od.get('cost', 0) or 0)
                    avg_price = float(od.get('average', 0) or 0)

                    if filled_qty > 0 and filled_cost <= 0 and avg_price > 0:
                        filled_cost = filled_qty * avg_price
                    if avg_price <= 0 and filled_qty > 0 and filled_cost > 0:
                        avg_price = filled_cost / filled_qty

                    fill_ts = od.get('lastTradeTimestamp') or od.get('timestamp')
                    filled_at = None
                    if fill_ts:
                        filled_at = datetime.fromtimestamp(float(fill_ts) / 1000).strftime('%Y-%m-%d %H:%M:%S')

                    local_status = 'OPEN'
                    if exch_status in ('closed', 'filled'):
                        local_status = 'FILLED'
                    elif exch_status in ('canceled', 'cancelled', 'expired', 'rejected'):
                        local_status = 'CANCELLED'
                    elif filled_qty > 0:
                        local_status = 'PARTIAL'

                    update_order_fill_status(order_id, local_status, filled_qty, filled_cost, avg_price, filled_at)
                    upsert_spot_order_fill(order_id, config_id, symbol, local_status, filled_qty, filled_cost, avg_price, filled_at)
                except Exception as one_error:
                    logger.debug(f"Skip spot order sync: {order_id} => {one_error}")
        except Exception as sync_error:
            logger.warning(f"DCA order sync failed for {config_id}: {sync_error}")

        # 2) 拉取成交并落库，统计只看已成交 BUY
        try:
            trades = mt.exchange.fetch_my_trades(symbol, limit=1000)
            if trades:
                save_trade_history(trades)
        except Exception as trade_error:
            logger.warning(f"Fetch my_trades failed for {symbol}: {trade_error}")

        with get_db_conn() as conn:
            agg = conn.execute(
                '''
                SELECT
                    COALESCE(SUM(t.cost), 0) AS traded_cost,
                    COALESCE(SUM(t.amount), 0) AS traded_qty,
                    COUNT(DISTINCT t.order_id) AS buy_count,
                    MIN(t.timestamp) AS first_buy,
                    MAX(t.timestamp) AS last_buy
                FROM trade_history t
                INNER JOIN orders o ON o.order_id = t.order_id
                WHERE o.config_id = ?
                  AND o.trade_mode = 'SPOT_DCA'
                  AND LOWER(t.side) = 'buy'
                ''',
                (config_id,),
            ).fetchone()

            pending = conn.execute(
                '''
                SELECT COUNT(*) AS pending_count
                FROM orders
                WHERE config_id = ?
                  AND trade_mode = 'SPOT_DCA'
                  AND status IN ('OPEN', 'PARTIAL')
                ''',
                (config_id,),
            ).fetchone()

        traded_cost = float(agg['traded_cost'] or 0)
        traded_qty = float(agg['traded_qty'] or 0)
        buy_count = int(agg['buy_count'] or 0)

        # 3) 读取真实余额用于展示持仓
        balances = mt.exchange.fetch_balance()
        current_qty = 0
        if base_asset in balances:
            current_qty = float(balances[base_asset].get('total', 0) or 0)
        elif base_asset.lower() in balances:
            current_qty = float(balances[base_asset.lower()].get('total', 0) or 0)
        elif 'total' in balances and base_asset in balances['total']:
            current_qty = float(balances['total'].get(base_asset, 0) or 0)

        final_qty = traded_qty + initial_qty
        final_invested = traded_cost + initial_cost
        avg_cost = (final_invested / final_qty) if final_qty > 0 else 0

        result = {
            "buy_count": buy_count,
            "total_invested": round(final_invested, 2),
            "total_qty": round(final_qty, 6),
            "avg_cost": round(avg_cost, 4),
            "dca_amount_per": cfg.get('dca_amount', cfg.get('dca_budget', 0)),
            "has_legacy": initial_qty > 0,
            "first_buy": agg['first_buy'],
            "last_buy": agg['last_buy'],
            "actual_balance": round(current_qty, 6),
            "pending_orders": int(pending['pending_count'] or 0),
            "sync_status": "synced",
            "last_sync": datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')
        }

        save_dca_daily_snapshot(config_id, symbol, result)

        DCA_STATS_CACHE[cache_key] = {
            'timestamp': now_ts,
            'data': result
        }
        return result
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
                
                # 默认获取第一页订单（首页决策流水固定 10 条）
                orders, total = get_paginated_orders(config_id, page=1, per_page=10)
                summary_dict['all_orders'] = orders
                summary_dict['order_total'] = total
                summary_dict['order_page'] = 1

                # 每日策略汇总
                summary_dict['daily_summaries'] = get_daily_summaries(config_id, days=5)
                
                agent_summaries.append(summary_dict)

        return agent_summaries, [], len(agent_summaries)
    except Exception as e:
        logger.error(f"❌ 获取仪表盘数据失败: {e}")
        return [], [], 0

@main_bp.route('/')
def index():
    symbol_redirect = _redirect_if_empty_symbol('main.index')
    if symbol_redirect:
        return symbol_redirect

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
    
    current_symbol = _resolve_symbol(symbols[0] if symbols else 'BTC/USDT')
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
    symbol_redirect = _redirect_if_empty_symbol('main.history_view')
    if symbol_redirect:
        return symbol_redirect

    symbol = _resolve_symbol('BTC/USDT')
    agent_filter = request.args.get('agent', 'ALL')
    compare_ids_raw = request.args.get('agents', '').strip()  # 多选对比模式：逗号分隔的 config_id 列表
    try:
        page = int(request.args.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)
    per_page = 20

    try:
        symbol_configs = [
            cfg for cfg in global_config.get_all_symbol_configs()
            if cfg.get('symbol') == symbol and cfg.get('config_id')
        ]
        config_map = {cfg.get('config_id'): cfg for cfg in symbol_configs}
        if agent_filter != 'ALL' and agent_filter not in config_map:
            agent_filter = 'ALL'

        summaries = get_paginated_summaries(symbol, page, per_page, config_id=agent_filter)
        total_count = get_summary_count(symbol, config_id=agent_filter)
        total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
        active_agents = [aid for aid in get_active_agents(symbol) if aid in config_map]
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

        # 实盘模式资金曲线 (从 balance_history 按天聚合，过滤 0 值异常点)
        real_chart_data = []
        real_balance = None
        if agent_mode == 'REAL' and agent_filter != 'ALL':
            try:
                with get_db_conn() as conn:
                    rows = conn.execute("""
                        SELECT day, total_equity FROM (
                            SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                                   row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                            FROM balance_history WHERE symbol = ? AND total_equity > 0
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
        dca_chart_data = []
        if agent_mode == 'SPOT_DCA' and agent_filter != 'ALL':
            dca_stats = calculate_dca_stats(agent_filter)
            dca_chart_data = get_dca_daily_snapshot_history(agent_filter, days=30)

        # 解析多选对比参数
        compare_ids = []
        if compare_ids_raw:
            compare_ids = [a.strip() for a in compare_ids_raw.split(',')
                           if a.strip() and a.strip() in config_map]

        history_compare_series = []
        if agent_filter == 'ALL' or compare_ids:
            target_cfgs = (
                [config_map[cid] for cid in compare_ids if cid in config_map]
                if compare_ids
                else symbol_configs
            )
            with get_db_conn() as conn:
                for cfg in target_cfgs:
                    config_id = cfg.get('config_id')
                    mode = str(cfg.get('mode', 'STRATEGY')).upper()
                    if not config_id or mode == 'SPOT_DCA':
                        continue

                    if mode == 'REAL':
                        rows = conn.execute("""
                            SELECT day, total_equity FROM (
                                SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                                       row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                                FROM balance_history WHERE symbol = ? AND total_equity > 0
                            ) WHERE rn = 1 ORDER BY day ASC
                        """, (symbol,)).fetchall()
                        points = [{"date": r["day"], "equity": r["total_equity"]} for r in rows]
                    else:
                        strategy_points = get_mock_equity_history(config_id)
                        points = [
                            {"date": p.get("date"), "equity": p.get("balance")}
                            for p in strategy_points
                            if p.get("date") is not None and p.get("balance") is not None
                        ]

                    if points:
                        history_compare_series.append({
                            "config_id": config_id,
                            "mode": mode,
                            "label": f"{config_id} ({mode})",
                            "points": points,
                        })

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
        dca_chart_data = []
        history_compare_series = []
        compare_ids = []

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
        dca_stats=dca_stats,
        dca_chart_data=dca_chart_data,
        history_compare_series=history_compare_series,
        compare_ids=compare_ids,
    )
@main_bp.route('/chat')
def chat_view():
    return render_template('chat.html', authed=_chat_authed())


@main_bp.route('/admin')
def admin_view():
    configs = global_config.get_all_symbol_configs()
    seen = set()
    symbols = []
    for c in configs:
        s = c.get('symbol')
        if s and s not in seen:
            symbols.append(s)
            seen.add(s)
    return render_template('admin.html', authed=_chat_authed(), symbols=symbols)


@main_bp.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('chat_authed', None)
    session.pop('admin_authed', None)
    return jsonify({"success": True})

@main_bp.route('/api/generate_daily_summary', methods=['POST'])
def manual_generate_summary():
    auth_err = _require_admin_auth_api()
    if auth_err:
        return auth_err

    data = request.json
    config_id = data.get('config_id')
    date_str = data.get('date') # YYYY-MM-DD
        
    if not config_id or not date_str:
        return jsonify({"success": False, "error": "Missing params", "need_captcha": False}), 400
        
    success = generate_manual_daily_summary(config_id, date_str)
    return jsonify({"success": success, "need_captcha": False})

@main_bp.route('/api/clean_history', methods=['POST'])
def clean_history():
    auth_err = _require_admin_auth_api()
    if auth_err:
        return auth_err

    data = request.json
    symbol = data.get('symbol')

    if not symbol:
        return jsonify({"success": False, "message": "缺少币种参数", "need_captcha": False}), 400

    delete_summaries_by_symbol(symbol)
    # 同时清除资金统计数据，让公开看板重新开始采样
    clean_financial_data(symbol)
    return jsonify({"success": True, "message": f"已成功重置 {symbol} 的所有历史及财务统计数据", "need_captcha": False})

@main_bp.route('/api/daily_summary/update', methods=['POST'])
def update_daily_summary_api():
    data = request.json or {}
    if not _chat_authed():
        ok, message, need_captcha, status = verify_admin_action(
            data.get('password', ''),
            (data.get('captcha', '') or '').upper()
        )
        if not ok:
            return jsonify({"success": False, "message": message, "need_captcha": need_captcha}), status
        session["chat_authed"] = True
        session["admin_authed"] = True

    date_str = data.get('date')
    config_id = data.get('config_id')
    summary_content = data.get('summary')

    if not date_str or not config_id or summary_content is None:
        return jsonify({"success": False, "message": "参数不完整", "need_captcha": False}), 400

    try:
        from database import update_daily_summary as db_update_ds
        updated = db_update_ds(date_str, config_id, summary_content)
        if not updated:
            return jsonify({"success": False, "message": "未找到对应的每日总结记录", "need_captcha": False}), 404
        return jsonify({"success": True, "message": "每日总结已更新", "need_captcha": False})
    except Exception as e:
        logger.error(f"更新每日总结错误: {e}")
        return jsonify({"success": False, "message": "数据库更新失败", "need_captcha": False}), 500

@main_bp.route('/stats/public')
def public_stats_view():
    symbol_redirect = _redirect_if_empty_symbol('main.public_stats_view')
    if symbol_redirect:
        return symbol_redirect

    configs = global_config.get_all_symbol_configs()
    seen = set()
    symbols = []
    for c in configs:
        s = c.get('symbol')
        if s and s not in seen:
            symbols.append(s)
            seen.add(s)
    current_symbol = _resolve_symbol(symbols[0] if symbols else 'BTC/USDT')
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
    days = int(request.args.get('days', 5))
    if not config_id:
        return jsonify({"success": False, "message": "Missing config_id"})
    data = get_daily_summaries(config_id, days=days)
    return jsonify({"success": True, "daily_summaries": data})
