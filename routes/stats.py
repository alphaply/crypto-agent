import sqlite3
from flask import Blueprint, jsonify, request
from routes.utils import DB_NAME, _require_chat_auth_api
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
