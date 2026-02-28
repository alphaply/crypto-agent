import sqlite3
from flask import Blueprint, jsonify, request
from routes.utils import DB_NAME, _require_chat_auth_api

stats_bp = Blueprint('stats', __name__)

@stats_bp.route('/api/stats/tokens', methods=['GET'])
def get_token_stats():
    """获取 Token 消耗统计 (公开)"""
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        daily_stats = c.execute("""
            SELECT strftime('%Y-%m-%d', timestamp) as day, 
                   SUM(prompt_tokens) as prompt, 
                   SUM(completion_tokens) as completion,
                   SUM(total_tokens) as total
            FROM token_usage 
            GROUP BY day 
            ORDER BY day DESC LIMIT 14
        """).fetchall()

        model_stats = c.execute("""
            SELECT model, SUM(total_tokens) as total 
            FROM token_usage 
            GROUP BY model
        """).fetchall()

        agent_stats = c.execute("""
            SELECT config_id, symbol, SUM(total_tokens) as total 
            FROM token_usage 
            GROUP BY config_id
        """).fetchall()

        conn.close()
        return jsonify({
            "success": True,
            "daily": [dict(r) for r in daily_stats],
            "models": [dict(r) for r in model_stats],
            "agents": [dict(r) for r in agent_stats]
        })
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
