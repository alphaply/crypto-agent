import sqlite3
from flask import Blueprint, jsonify, request
from routes.utils import DB_NAME, _require_chat_auth_api, logger
from database import get_all_pricing, update_model_pricing

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
    input_p = float(data.get('input_price', 0))
    output_p = float(data.get('output_price', 0))
    
    if not model:
        return jsonify({"success": False, "message": "Missing model name"})
    
    try:
        update_model_pricing(model, input_p, output_p)
        return jsonify({"success": True})
    except Exception as e:
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
        stats = get_agent_trade_stats(config_id)
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@stats_bp.route('/api/stats/position/<config_id>', methods=['GET'])
def get_position_stats(config_id):
    """获取仓位 + 真实盈亏统计 (支持 REAL 和 STRATEGY 模式)"""
    try:
        from config import config as global_config
        from utils.market_data import MarketTool
        from database import save_trade_history, get_mock_account, get_db_conn

        cfg = global_config.get_config_by_id(config_id)
        if not cfg:
            return jsonify({"success": False, "message": f"未找到配置: {config_id}"})

        mode = cfg.get('mode', 'STRATEGY').upper()
        symbol = cfg.get('symbol')

        if mode not in ['REAL', 'STRATEGY']:
            return jsonify({"success": True, "mode": mode, "positions": [], "summary": None,
                            "message": "仅 REAL/STRATEGY 模式支持实时仓位查询"})

        positions = []
        balance = 0
        recent_trades = []
        trade_summary = {"total_trades": 0, "realized_pnl": 0, "win_count": 0,
                         "lose_count": 0, "win_rate": 0}

        mt = MarketTool(config_id=config_id)

        if mode == 'REAL':
            # 1. 获取当前仓位
            try:
                all_pos = mt.exchange.fetch_positions([symbol])
                for p in all_pos:
                    contracts = float(p.get('contracts', 0))
                    if contracts > 0:
                        entry = float(p.get('entryPrice', 0))
                        unrealized = float(p.get('unrealizedPnl', 0))
                        notional = float(p.get('notional', 0)) or (entry * contracts)
                        pnl_pct = (unrealized / abs(notional) * 100) if notional != 0 else 0
                        leverage = p.get('leverage', cfg.get('leverage', 1))
                        positions.append({
                            'symbol': p.get('symbol', symbol),
                            'side': str(p.get('side', '')).upper(),
                            'contracts': contracts,
                            'entry_price': entry,
                            'mark_price': float(p.get('markPrice', 0)),
                            'unrealized_pnl': round(unrealized, 4),
                            'pnl_pct': round(pnl_pct, 2),
                            'leverage': leverage,
                            'notional': round(abs(notional), 2),
                        })
            except Exception as e:
                logger.warning(f"Fetch positions error: {e}")

            # 2. 获取账户余额
            try:
                bal = mt.exchange.fetch_balance()
                balance = float(bal.get('USDT', {}).get('total', 0) or
                                bal.get('total', {}).get('USDT', 0) or 0)
            except Exception as e:
                logger.warning(f"Fetch balance error: {e}")

            # 3. 获取最近成交 & 同步到数据库
            try:
                raw_trades = mt.exchange.fetch_my_trades(symbol, limit=100)
                if raw_trades:
                    save_trade_history(raw_trades)

                    for t in raw_trades:
                        pnl = t.get('realizedPnl')
                        if pnl is None and 'info' in t:
                            pnl = t['info'].get('realizedPnl')
                        if pnl is not None:
                            pnl = float(pnl)
                            if pnl > 0:
                                trade_summary["win_count"] += 1
                            elif pnl < 0:
                                trade_summary["lose_count"] += 1
                            trade_summary["realized_pnl"] += pnl

                    trade_summary["total_trades"] = len(raw_trades)
                    total_decided = trade_summary["win_count"] + trade_summary["lose_count"]
                    trade_summary["win_rate"] = round(
                        trade_summary["win_count"] / total_decided * 100, 1
                    ) if total_decided > 0 else 0
                    trade_summary["realized_pnl"] = round(trade_summary["realized_pnl"], 4)

                    recent_trades = [{
                        'time': t.get('datetime', ''),
                        'side': t.get('side', ''),
                        'price': float(t.get('price', 0)),
                        'amount': float(t.get('amount', 0)),
                        'pnl': float(t.get('info', {}).get('realizedPnl', 0) or 0),
                    } for t in raw_trades[-5:]]
            except Exception as e:
                logger.warning(f"Fetch trades error: {e}")

        else:
            # STRATEGY 模式
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

            # 2. 模拟持仓 (即 status='OPEN' 的 mock_orders，如果是 limit 但未触发，则不应算作持仓，但暂统一显示并标注)
            with get_db_conn() as conn:
                c = conn.cursor()
                open_mocks = c.execute("SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND status='OPEN'", (config_id, symbol)).fetchall()
                for om in open_mocks:
                    is_filled = int(om['is_filled'] or 0)
                    if not is_filled: continue # 只显示已入场的
                    
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
                    pnl_pct = (unrealized / notional * 100) if notional > 0 else 0
                    
                    positions.append({
                        'symbol': symbol,
                        'side': 'LONG' if 'BUY' in side else 'SHORT',
                        'contracts': amount,
                        'entry_price': entry,
                        'mark_price': current_price,
                        'unrealized_pnl': round(unrealized, 4),
                        'pnl_pct': round(pnl_pct, 2),
                        'leverage': cfg.get('leverage', 1),
                        'notional': round(notional, 2),
                    })

                # 3. 模拟历史胜率
                closed_mocks = c.execute("SELECT * FROM mock_orders WHERE config_id=? AND symbol=? AND status='CLOSED' AND realized_pnl IS NOT NULL", (config_id, symbol)).fetchall()
                for cm in closed_mocks:
                    pnl = float(cm['realized_pnl'] or 0)
                    trade_summary["realized_pnl"] += pnl
                    if pnl > 0:
                        trade_summary["win_count"] += 1
                    elif pnl < 0:
                        trade_summary["lose_count"] += 1

                trade_summary["total_trades"] = len(closed_mocks)
                total_decided = trade_summary["win_count"] + trade_summary["lose_count"]
                trade_summary["win_rate"] = round(
                    trade_summary["win_count"] / total_decided * 100, 1
                ) if total_decided > 0 else 0
                trade_summary["realized_pnl"] = round(trade_summary["realized_pnl"], 4)

                recent_closed = closed_mocks[-5:]
                for t in recent_closed:
                    recent_trades.append({
                        'time': t['close_time'] or t['timestamp'],
                        'side': t['side'],
                        'price': float(t['close_price'] or 0),
                        'amount': float(t['amount']),
                        'pnl': float(t['realized_pnl'] or 0)
                    })

        return jsonify({
            "success": True,
            "mode": mode,
            "positions": positions,
            "balance": round(balance, 2),
            "recent_trades": recent_trades,
            "summary": trade_summary,
        })
    except Exception as e:
        import traceback
        logger.error(f"Position stats error: {traceback.format_exc()}")
        return jsonify({"success": False, "message": str(e)})

