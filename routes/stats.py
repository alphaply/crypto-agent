import sqlite3
from flask import Blueprint, jsonify
from routes.utils import DB_NAME, _require_chat_auth_api

stats_bp = Blueprint('stats', __name__)

@stats_bp.route('/api/stats/tokens', methods=['GET'])
def get_token_stats():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    
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
