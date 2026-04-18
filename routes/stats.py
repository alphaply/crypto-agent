import sqlite3
import pandas as pd
from flask import Blueprint, jsonify, request
from routes.utils import DB_NAME, _require_chat_auth_api, logger
from database import get_all_pricing, update_model_pricing, delete_model_pricing
from config import config as global_config
from utils.indicators import calc_ema

stats_bp = Blueprint('stats', __name__)

@stats_bp.route('/api/stats/tokens', methods=['GET'])
def get_token_stats():
    """获取 Token 消耗统计 (公开)"""
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        pricing = get_all_pricing()

        daily_stats = c.execute("""
            SELECT strftime('%Y-%m-%d', timestamp) as day, 
                   SUM(prompt_tokens) as prompt, 
                   SUM(completion_tokens) as completion,
                   SUM(total_tokens) as total
            FROM token_usage 
            GROUP BY day 
            ORDER BY day DESC LIMIT 14
        """).fetchall()

        # 计算每日成本
        # 这里的 daily 计算成本比较复杂，因为一天内可能有多个模型。
        # 为了简单起见，我们直接获取每条记录并计算
        daily_costs = {}
        all_usages = c.execute("SELECT timestamp, model, prompt_tokens, completion_tokens FROM token_usage").fetchall()
        for u in all_usages:
            day = u['timestamp'][:10]
            m_price = pricing.get(u['model'], {'input_price_per_m': 0, 'output_price_per_m': 0})
            cost = (u['prompt_tokens'] / 1000000 * m_price['input_price_per_m']) + \
                   (u['completion_tokens'] / 1000000 * m_price['output_price_per_m'])
            daily_costs[day] = daily_costs.get(day, 0) + cost

        model_stats = c.execute("""
            SELECT model, 
                   SUM(prompt_tokens) as prompt, 
                   SUM(completion_tokens) as completion,
                   SUM(total_tokens) as total 
            FROM token_usage 
            GROUP BY model
        """).fetchall()

        # 为模型统计添加成本信息
        model_stats_list = []
        for m in model_stats:
            d = dict(m)
            m_price = pricing.get(d['model'], {'input_price_per_m': 0, 'output_price_per_m': 0})
            d['cost'] = (d['prompt'] / 1000000 * m_price['input_price_per_m']) + \
                        (d['completion'] / 1000000 * m_price['output_price_per_m'])
            model_stats_list.append(d)

        agent_stats = c.execute("""
            SELECT config_id, symbol, 
                   SUM(prompt_tokens) as prompt, 
                   SUM(completion_tokens) as completion,
                   SUM(total_tokens) as total 
            FROM token_usage 
            GROUP BY config_id
        """).fetchall()

        conn.close()
        
        # 格式化 daily 数据，带上成本
        daily_formatted = []
        for r in daily_stats:
            d = dict(r)
            d['cost'] = round(daily_costs.get(d['day'], 0), 4)
            daily_formatted.append(d)

        return jsonify({
            "success": True,
            "daily": daily_formatted,
            "models": model_stats_list,
            "agents": [dict(r) for r in agent_stats],
            "pricing": pricing
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@stats_bp.route('/api/stats/pricing', methods=['POST'])
def save_pricing():
    """保存模型计价配置 (需要认证)"""
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    
    data = request.json
    model = data.get('model')
    try:
        input_p = float(data.get('input_price', 0))
        output_p = float(data.get('output_price', 0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "价格参数格式错误"}), 400
    
    if not model:
        return jsonify({"success": False, "message": "Missing model name"})
    
    try:
        update_model_pricing(model, input_p, output_p)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@stats_bp.route('/api/stats/pricing', methods=['GET'])
def list_pricing():
    """获取模型计价列表 (需要认证)"""
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    try:
        pricing = get_all_pricing()
        items = []
        for model, row in pricing.items():
            items.append({
                "model": model,
                "input_price_per_m": row.get('input_price_per_m', 0),
                "output_price_per_m": row.get('output_price_per_m', 0),
                "currency": row.get('currency', 'USD'),
            })
        items.sort(key=lambda x: x['model'])
        return jsonify({"success": True, "pricing": items})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@stats_bp.route('/api/stats/pricing', methods=['DELETE'])
def delete_pricing():
    """删除模型计价配置 (需要认证)"""
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    data = request.json or {}
    model = (data.get('model') or '').strip()
    if not model:
        return jsonify({"success": False, "message": "Missing model name"}), 400

    try:
        deleted = delete_model_pricing(model)
        if not deleted:
            return jsonify({"success": False, "message": "模型不存在"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to delete pricing for {model}: {e}")
        return jsonify({"success": False, "message": str(e)})

@stats_bp.route('/api/stats/financial', methods=['GET'])
def get_financial_stats():
    """公开的财务统计接口"""
    symbol = request.args.get('symbol', 'BTC/USDT')
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # 1. 资金曲线 (分时，用于展示近期细节)
        balance_history = c.execute(
            "SELECT timestamp, total_equity, total_balance FROM balance_history WHERE symbol = ? ORDER BY id ASC LIMIT 200", 
            (symbol,)
        ).fetchall()

        # 1.1 资金曲线 (按天聚合，取每日最后一笔记录作为收盘净值)
        daily_equity = c.execute("""
            SELECT day, total_equity FROM (
                SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                       row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                FROM balance_history WHERE symbol = ?
            ) WHERE rn = 1 ORDER BY day ASC
        """, (symbol,)).fetchall()
        
        # 2. 统计概览
        trades = c.execute(
            "SELECT realized_pnl FROM trade_history WHERE symbol = ?", 
            (symbol,)
        ).fetchall()
        
        total_pnl = sum(t['realized_pnl'] for t in trades)
        win_trades = [t for t in trades if t['realized_pnl'] > 0]
        lose_trades = [t for t in trades if t['realized_pnl'] < 0]
        
        win_rate = (len(win_trades) / len(trades) * 100) if trades else 0

        # 获取当前最新的资产状况
        latest_equity = 0
        latest_balance = 0
        if balance_history:
            latest_equity = balance_history[-1]['total_equity']
            latest_balance = balance_history[-1]['total_balance']
        
        conn.close()
        return jsonify({
            "success": True,
            "balance_history": [dict(r) for r in balance_history],
            "daily_equity": [dict(r) for r in daily_equity],
            "summary": {
                "total_trades": len(trades),
                "total_pnl": round(total_pnl, 2),
                "win_rate": round(win_rate, 2),
                "win_count": len(win_trades),
                "lose_count": len(lose_trades),
                "latest_equity": round(latest_equity, 2),
                "latest_balance": round(latest_balance, 2)
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@stats_bp.route('/api/stats/agent/<config_id>', methods=['GET'])
def get_agent_stats(config_id):
    """获取指定 Agent 的做单统计 (通用，适用所有模式)"""
    try:
        from database import get_agent_trade_stats
        from config import config as global_config
        from routes.main import calculate_dca_stats
        
        # 基础成交统计 (所有模式)
        stats = get_agent_trade_stats(config_id)
        
        # 定投专项统计 (SPOT_DCA 模式)
        cfg = global_config.get_config_by_id(config_id)
        if cfg and cfg.get('mode') == 'SPOT_DCA':
            stats['dca_stats'] = calculate_dca_stats(config_id)
            stats['mode'] = 'SPOT_DCA'
        else:
            stats['mode'] = cfg.get('mode', 'STRATEGY') if cfg else 'STRATEGY'
            
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.error(f"Failed to get agent stats for {config_id}: {e}")
        return jsonify({"success": False, "message": str(e)})


def _calculate_win_rate(trade_summary):
    """计算胜率并格式化 trade_summary"""
    total_decided = trade_summary["win_count"] + trade_summary["lose_count"]
    trade_summary["win_rate"] = round(
        trade_summary["win_count"] / total_decided * 100, 1
    ) if total_decided > 0 else 0
    trade_summary["realized_pnl"] = round(trade_summary["realized_pnl"], 4)
    return trade_summary


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _resolve_leverage(position, cfg, fallback_leverage):
    """多路径解析杠杆，避免交易所字段差异导致误显示 1x。"""
    info = position.get('info', {}) or {}

    candidates = [
        position.get('leverage'),
        position.get('info', {}).get('leverage') if isinstance(position.get('info'), dict) else None,
        info.get('leverage'),
        info.get('bracketLeverage'),
    ]

    for item in candidates:
        val = _safe_float(item, 0)
        if val > 0:
            return val

    notional = abs(_safe_float(position.get('notional'), 0))
    initial_margin = abs(_safe_float(position.get('initialMargin'), 0))
    if notional > 0 and initial_margin > 0:
        inferred = notional / initial_margin
        if inferred > 0:
            return inferred

    cfg_lev = _safe_float(cfg.get('leverage') if isinstance(cfg, dict) else None, 0)
    if cfg_lev > 0:
        return cfg_lev

    fb = _safe_float(fallback_leverage, 1)
    return fb if fb > 0 else 1


def _fetch_real_position_data(mt, symbol, cfg):
    """获取实盘仓位、余额和最近成交"""
    from database import save_trade_history

    positions = []
    balance = 0
    recent_trades = []
    fetch_errors = []
    trade_summary = {"total_trades": 0, "realized_pnl": 0, "win_count": 0,
                     "lose_count": 0, "win_rate": 0}
    from config import config as global_config
    fallback_leverage = global_config.get_leverage(cfg.get('config_id') if isinstance(cfg, dict) else None)

    # 1. 获取当前仓位
    try:
        all_pos = mt.exchange.fetch_positions([symbol])
        for p in all_pos:
            contracts = float(p.get('contracts', 0))
            if contracts > 0:
                entry = float(p.get('entryPrice', 0))
                unrealized = float(p.get('unrealizedPnl', 0))
                notional = float(p.get('notional', 0)) or (entry * contracts)
                leverage = _resolve_leverage(p, cfg, fallback_leverage)
                pnl_pct = (unrealized / abs(notional) * 100) if notional != 0 else 0
                roi_pct = pnl_pct * leverage
                margin_used = abs(notional) / leverage if leverage > 0 else abs(notional)
                positions.append({
                    'symbol': p.get('symbol', symbol),
                    'side': str(p.get('side', '')).upper(),
                    'contracts': contracts,
                    'qty': contracts,
                    'entry_price': entry,
                    'mark_price': float(p.get('markPrice', 0)),
                    'unrealized_pnl': round(unrealized, 4),
                    'pnl_pct': round(pnl_pct, 2),
                    'roi_pct': round(roi_pct, 2),
                    'leverage': leverage,
                    'notional': round(abs(notional), 2),
                    'margin_used': round(margin_used, 2),
                })
    except Exception as e:
        err = f"fetch_positions_failed({type(e).__name__}): {e}"
        fetch_errors.append(err)
        logger.error(
            f"REAL 持仓获取失败 config_id={cfg.get('config_id')} symbol={symbol} "
            f"exchange={getattr(mt.exchange, 'id', 'unknown')}: {e}",
            exc_info=True,
        )

    # 2. 获取账户余额
    try:
        bal = mt.exchange.fetch_balance()
        balance = float(
            bal.get('USDT', {}).get('total', 0)
            or bal.get('USDT', {}).get('free', 0)
            or bal.get('total', {}).get('USDT', 0)
            or 0
        )
    except Exception as e:
        err = f"fetch_balance_failed({type(e).__name__}): {e}"
        fetch_errors.append(err)
        logger.error(
            f"REAL 余额获取失败 config_id={cfg.get('config_id')} symbol={symbol} "
            f"exchange={getattr(mt.exchange, 'id', 'unknown')}: {e}",
            exc_info=True,
        )

    # 3. 获取最近成交 & 同步到数据库
    try:
        raw_trades = mt.exchange.fetch_my_trades(symbol, limit=100)
        if raw_trades:
            save_trade_history(raw_trades)

            # 聚合相同 order_id 的成交 (解决分批成交导致的“乱”)
            aggregated = {}
            for t in raw_trades:
                pnl = float(t.get('info', {}).get('realizedPnl', 0) or 0)
                if pnl == 0: continue
                
                oid = str(t.get('order', t.get('order_id', 'unknown')))
                if oid not in aggregated:
                    aggregated[oid] = {
                        'time': t.get('datetime', ''),
                        'side': t.get('side', ''),
                        'price': float(t.get('price', 0)),
                        'amount': float(t.get('amount', 0)),
                        'pnl': pnl,
                        'count': 1
                    }
                else:
                    # 累加数量和盈亏，计算均价
                    old = aggregated[oid]
                    new_total_amount = old['amount'] + float(t.get('amount', 0))
                    if new_total_amount > 0:
                        old['price'] = (old['price'] * old['amount'] + float(t.get('price', 0)) * float(t.get('amount', 0))) / new_total_amount
                    old['amount'] = new_total_amount
                    old['pnl'] += pnl
                    old['count'] += 1
                    old['time'] = t.get('datetime', old['time']) # 保留最后一次成交时间

            def _approximate_entry(t_side, t_price, t_amount, t_pnl):
                if t_amount <= 0: return 0
                if str(t_side).lower() == 'sell':
                    return round(t_price - (t_pnl / t_amount), 4)
                else:
                    return round(t_price + (t_pnl / t_amount), 4)

            # 转换为前端格式
            display_trades = []
            for oid, data in aggregated.items():
                side = data['side'].upper()
                # 优化方向显示：如果卖出平仓(SELL)，通常是平多(LONG)
                label_side = "LONG (Closed)" if side == "SELL" else "SHORT (Closed)"
                
                display_trades.append({
                    'time': data['time'],
                    'side': label_side,
                    'price': round(data['price'], 4),
                    'amount': round(data['amount'], 4),
                    'pnl': round(data['pnl'], 4),
                    'entry_price': _approximate_entry(
                        data['side'],
                        data['price'],
                        data['amount'],
                        data['pnl']
                    )
                })
            
            # 按时间倒序排列，取最近 5 个
            recent_trades = sorted(display_trades, key=lambda x: x['time'], reverse=True)[:5]
    except Exception as e:
        logger.warning(
            f"Fetch trades error config_id={cfg.get('config_id')} symbol={symbol} "
            f"exchange={getattr(mt.exchange, 'id', 'unknown')}: {e}"
        )

    # 4. 从数据库获取完整的历史盈亏统计
    try:
        from database import get_history_pnl_stats
        config_id = cfg.get('config_id')
        pnl_stats = get_history_pnl_stats(symbol, config_id)
        if pnl_stats:
            trade_summary["win_count"] = pnl_stats.get("win_count", 0)
            trade_summary["lose_count"] = pnl_stats.get("lose_count", 0)
            trade_summary["total_trades"] = pnl_stats.get("total_trades", 0)
            trade_summary["realized_pnl"] = pnl_stats.get("total_pnl", 0)
    except Exception as e:
        logger.warning(f"Failed to load full PnL stats from DB: {e}")

    trade_summary = _calculate_win_rate(trade_summary)

    return positions, balance, recent_trades, trade_summary, fetch_errors


def _fetch_strategy_position_data(mt, config_id, symbol, cfg):
    """获取策略模式的模拟仓位和盈亏统计"""
    from database import get_mock_account, get_db_conn

    positions = []
    recent_trades = []
    trade_summary = {"total_trades": 0, "realized_pnl": 0, "win_count": 0,
                     "lose_count": 0, "win_rate": 0}
    from config import config as global_config
    fallback_leverage = global_config.get_leverage(config_id)

    # 获取当前市场价格
    current_price = 0
    try:
        ticker = mt.exchange.fetch_ticker(symbol)
        current_price = float(ticker.get('last', 0))
    except Exception as e:
        logger.warning(f"Fetch ticker error in STRATEGY mode: {e}")

    # 1. 模拟余额
    mock_acc = get_mock_account(config_id, symbol)
    balance = mock_acc.get('balance', 10000.0)

    # 2. 模拟持仓
    with get_db_conn() as conn:
        c = conn.cursor()
        open_mocks = c.execute(
            "SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN'",
            (config_id, symbol)
        ).fetchall()

        for om in open_mocks:
            if not int(om['is_filled'] or 0):
                continue  # 只显示已入场的

            entry = float(om['price'])
            amount = float(om['amount'])
            side = str(om['side']).upper()

            unrealized = 0
            if current_price > 0:
                if 'BUY' in side:
                    unrealized = (current_price - entry) * amount
                else:
                    unrealized = (entry - current_price) * amount

            notional = entry * amount
            leverage = _safe_float(cfg.get('leverage'), 0)
            if leverage <= 0:
                leverage = _safe_float(fallback_leverage, 1)
            if leverage <= 0:
                leverage = 1
            pnl_pct = (unrealized / notional * 100) if notional > 0 else 0
            roi_pct = pnl_pct * leverage
            margin_used = notional / leverage if leverage > 0 else notional

            positions.append({
                'symbol': symbol,
                'side': 'LONG' if 'BUY' in side else 'SHORT',
                'contracts': amount,
                'qty': amount,
                'entry_price': entry,
                'mark_price': current_price,
                'unrealized_pnl': round(unrealized, 4),
                'pnl_pct': round(pnl_pct, 2),
                'roi_pct': round(roi_pct, 2),
                'leverage': leverage,
                'notional': round(notional, 2),
                'margin_used': round(margin_used, 2),
            })

        # 3. 模拟历史胜率
        closed_mocks = c.execute(
            "SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND status='CLOSED' AND realized_pnl IS NOT NULL",
            (config_id, symbol)
        ).fetchall()

        for cm in closed_mocks:
            pnl = float(cm['realized_pnl'] or 0)
            trade_summary["realized_pnl"] += pnl
            if pnl > 0:
                trade_summary["win_count"] += 1
            elif pnl < 0:
                trade_summary["lose_count"] += 1

        trade_summary["total_trades"] = len(closed_mocks)
        _calculate_win_rate(trade_summary)

        recent_trades = [{
            'time': t['close_time'] or t['timestamp'],
            'side': t['side'],
            'entry_price': float(t['price'] or 0),
            'price': float(t['close_price'] or 0),
            'amount': float(t['amount']),
            'pnl': float(t['realized_pnl'] or 0)
        } for t in closed_mocks[-5:]]

    return positions, balance, recent_trades, trade_summary


@stats_bp.route('/api/stats/position/<config_id>', methods=['GET'])
def get_position_stats(config_id):
    """获取仓位 + 真实盈亏统计 (支持 REAL 和 STRATEGY 模式)"""
    try:
        from config import config as global_config
        from utils.market_data import MarketTool

        cfg = global_config.get_config_by_id(config_id)
        if not cfg:
            return jsonify({"success": False, "message": f"未找到配置: {config_id}"})

        mode = cfg.get('mode', 'STRATEGY').upper()
        symbol = cfg.get('symbol')

        if not symbol:
            return jsonify({"success": False, "message": f"配置 {config_id} 缺少 symbol"}), 400

        if mode not in ['REAL', 'STRATEGY']:
            return jsonify({"success": True, "mode": mode, "positions": [], "summary": None,
                            "message": "仅 REAL/STRATEGY 模式支持实时仓位查询"})

        try:
            mt = MarketTool(config_id=config_id)
        except ValueError as e:
            logger.error(f"初始化交易所失败 config_id={config_id}: {e}")
            return jsonify({"success": False, "message": str(e)}), 400

        if mode == 'REAL':
            positions, balance, recent_trades, trade_summary, fetch_errors = _fetch_real_position_data(mt, symbol, cfg)
            if fetch_errors and not positions:
                return jsonify({
                    "success": False,
                    "mode": mode,
                    "positions": [],
                    "balance": round(balance, 2),
                    "recent_trades": recent_trades,
                    "summary": trade_summary,
                    "message": "实盘仓位获取失败，请检查交易所凭证与权限",
                    "errors": fetch_errors,
                }), 502
        else:
            positions, balance, recent_trades, trade_summary = _fetch_strategy_position_data(mt, config_id, symbol, cfg)
            fetch_errors = []

        return jsonify({
            "success": True,
            "mode": mode,
            "positions": positions,
            "balance": round(balance, 2),
            "recent_trades": recent_trades,
            "summary": trade_summary,
            "errors": fetch_errors,
        })
    except Exception as e:
        import traceback
        logger.error(f"Position stats error: {traceback.format_exc()}")
        return jsonify({"success": False, "message": str(e)})


@stats_bp.route('/api/stats/equity_compare', methods=['GET'])
def get_equity_compare():
    """多 config 净值对比接口，返回按 config_id 分组的时间序列。"""
    symbol = request.args.get('symbol', 'BTC/USDT')
    raw_ids = request.args.get('config_ids', '').strip()

    configs = [c for c in global_config.get_all_symbol_configs() if c.get('symbol') == symbol]
    if raw_ids:
        wanted = {x.strip() for x in raw_ids.split(',') if x.strip()}
        configs = [c for c in configs if c.get('config_id') in wanted]

    # 防止一次请求过大
    configs = configs[:12]

    series = []
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        for cfg in configs:
            config_id = cfg.get('config_id')
            mode = (cfg.get('mode') or 'STRATEGY').upper()
            label = f"{config_id} ({mode})"
            points = []

            if mode == 'REAL':
                rows = c.execute(
                    """
                    SELECT day, total_equity FROM (
                        SELECT strftime('%Y-%m-%d', timestamp) as day, total_equity,
                               row_number() OVER (PARTITION BY strftime('%Y-%m-%d', timestamp) ORDER BY timestamp DESC) as rn
                        FROM balance_history WHERE symbol = ?
                    ) WHERE rn = 1 ORDER BY day ASC
                    """,
                    (symbol,),
                ).fetchall()
                points = [{"date": r['day'], "equity": r['total_equity']} for r in rows]
            else:
                rows = c.execute(
                    """
                    SELECT date(timestamp) as day, MAX(balance) as equity
                    FROM mock_balance_history
                    WHERE config_id = ?
                    GROUP BY date(timestamp)
                    ORDER BY day ASC
                    """,
                    (config_id,),
                ).fetchall()
                points = [{"date": r['day'], "equity": r['equity']} for r in rows]

            if points:
                series.append({
                    "config_id": config_id,
                    "label": label,
                    "mode": mode,
                    "points": points,
                })

        conn.close()
        return jsonify({
            "success": True,
            "symbol": symbol,
            "series": series,
        })
    except Exception as e:
        logger.error(f"Failed to load equity compare data: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# ---------------------------------------------------------------------------
#  K 线图数据 API
# ---------------------------------------------------------------------------

_KLINE_ALLOWED_TF = {'15m', '1h', '4h', '1d'}


@stats_bp.route('/api/kline/<config_id>', methods=['GET'])
def get_kline_data(config_id):
    """获取 K 线 + EMA + 当前有效仓位/挂单（公开，不返回任何凭证）"""

    timeframe = request.args.get('timeframe', '1h')
    if timeframe not in _KLINE_ALLOWED_TF:
        return jsonify({"success": False, "message": f"不支持的周期: {timeframe}"}), 400

    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        return jsonify({"success": False, "message": f"未找到配置: {config_id}"}), 404

    symbol = cfg.get('symbol')
    mode = (cfg.get('mode') or 'STRATEGY').upper()

    try:
        from utils.market_data import MarketTool
        mt = MarketTool(config_id=config_id)
    except Exception as e:
        logger.error(f"Kline: 初始化交易所失败 config_id={config_id}: {e}")
        return jsonify({"success": False, "message": str(e)}), 400

    # --- 1. OHLCV ---
    try:
        raw = mt.exchange.fetch_ohlcv(symbol, timeframe, limit=300)
    except Exception as e:
        logger.error(f"Kline: fetch_ohlcv 失败: {e}")
        return jsonify({"success": False, "message": f"获取K线失败: {e}"}), 502

    if not raw:
        return jsonify({"success": True, "candles": [], "volume": [], "emas": {},
                        "orders": [], "position": None, "positions": [], "pending_orders": [], "risk_lines": []})

    candles = []
    volumes = []
    closes = []
    for r in raw:
        ts = int(r[0] / 1000)  # lightweight-charts 使用秒级 UTC 时间戳
        o, h, l, c, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c})
        color = "rgba(38,166,154,0.5)" if c >= o else "rgba(239,83,80,0.5)"
        volumes.append({"time": ts, "value": v, "color": color})
        closes.append(c)

    # --- 2. EMA 20/50/100/200 ---
    close_series = pd.Series(closes)
    emas = {}
    for span in (20, 50, 100, 200):
        ema_vals = calc_ema(close_series, span)
        ema_data = []
        for i, val in enumerate(ema_vals):
            if pd.notna(val) and i >= span - 1:
                ema_data.append({"time": candles[i]["time"], "value": round(float(val), 6)})
        emas[str(span)] = ema_data

    # --- 3. 当前持仓 ---
    positions = []
    position = None
    risk_lines = []
    try:
        if mode == 'REAL':
            all_pos = mt.exchange.fetch_positions([symbol])
            for p in all_pos:
                if float(p.get('contracts', 0)) > 0:
                    current_position = {
                        "side": str(p.get('side', '')).upper(),
                        "entry_price": float(p.get('entryPrice', 0)),
                        "amount": float(p.get('contracts', 0)),
                    }
                    positions.append(current_position)
            if positions:
                position = positions[0]
        elif mode == 'STRATEGY':
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT side, price, amount, stop_loss, take_profit, order_id FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN' AND is_filled=1 ORDER BY timestamp ASC",
                (config_id, symbol),
            ).fetchall()
            conn.close()
            for row in rows:
                side_raw = str(row['side']).upper()
                current_position = {
                    "side": "LONG" if 'BUY' in side_raw else "SHORT",
                    "entry_price": float(row['price']),
                    "amount": float(row['amount']),
                    "order_id": row['order_id'],
                    "stop_loss": float(row['stop_loss'] or 0),
                    "take_profit": float(row['take_profit'] or 0),
                }
                positions.append(current_position)
                if float(row['take_profit'] or 0) > 0:
                    risk_lines.append({
                        "price": float(row['take_profit']),
                        "type": "take_profit",
                        "label": "止盈",
                        "amount": float(row['amount']),
                        "side": current_position['side'],
                        "order_id": row['order_id'],
                    })
                if float(row['stop_loss'] or 0) > 0:
                    risk_lines.append({
                        "price": float(row['stop_loss']),
                        "type": "stop_loss",
                        "label": "止损",
                        "amount": float(row['amount']),
                        "side": current_position['side'],
                        "order_id": row['order_id'],
                    })
            if positions:
                position = positions[0]
        elif mode == 'SPOT_DCA':
            from routes.main import calculate_dca_stats
            dca = calculate_dca_stats(config_id)
            if dca and dca.get('avg_cost', 0) > 0:
                position = {
                    "side": "LONG",
                    "entry_price": dca['avg_cost'],
                    "amount": dca.get('total_qty', 0),
                }
                positions.append(position)
    except Exception as e:
        logger.warning(f"Kline: 获取持仓失败: {e}")

    # --- 4. 当前挂单 ---
    pending_orders = []
    try:
        if mode == 'REAL':
            open_orders = mt.exchange.fetch_open_orders(symbol)
            current_side = (position or {}).get('side', '').upper()
            for o in open_orders:
                s = str(o.get('side', '')).upper()
                info = o.get('info', {}) or {}
                reduce_only = bool(
                    o.get('reduceOnly')
                    or o.get('reduce_only')
                    or info.get('reduceOnly')
                    or info.get('closePosition')
                )
                if reduce_only:
                    otype = 'close_long' if s == 'SELL' else 'close_short'
                elif current_side == 'LONG' and s == 'SELL':
                    otype = 'close_long'
                elif current_side == 'SHORT' and s == 'BUY':
                    otype = 'close_short'
                else:
                    otype = 'open_long' if s == 'BUY' else 'open_short'
                pending_orders.append({
                    "price": float(o.get('price', 0)),
                    "side": s,
                    "amount": float(o.get('amount', 0)),
                    "order_id": o.get('id', ''),
                    "type": otype,
                })
        elif mode == 'STRATEGY':
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT order_id, side, price, amount FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN' AND is_filled=0",
                (config_id, symbol),
            ).fetchall()
            conn.close()
            for row in rows:
                s = str(row['side']).upper()
                pending_orders.append({
                    "price": float(row['price']),
                    "side": s,
                    "amount": float(row['amount']),
                    "order_id": row['order_id'],
                    "type": 'open_long' if 'BUY' in s else 'open_short',
                })
        elif mode == 'SPOT_DCA':
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT o.order_id, o.side, o.entry_price, o.amount
                   FROM orders o LEFT JOIN spot_order_fills f ON o.order_id = f.order_id
                   WHERE o.config_id=? AND o.trade_mode='SPOT_DCA' AND o.status='OPEN'
                     AND (f.status IS NULL OR f.status NOT IN ('FILLED','CANCELED'))""",
                (config_id,),
            ).fetchall()
            conn.close()
            for row in rows:
                pending_orders.append({
                    "price": float(row['entry_price'] or 0),
                    "side": "BUY",
                    "amount": float(row['amount'] or 0),
                    "order_id": row['order_id'],
                    "type": "buy_spot",
                })
    except Exception as e:
        logger.warning(f"Kline: 获取挂单失败: {e}")

    return jsonify({
        "success": True,
        "candles": candles,
        "volume": volumes,
        "emas": emas,
        "orders": [],
        "position": position,
        "positions": positions,
        "pending_orders": pending_orders,
        "risk_lines": risk_lines,
    })
