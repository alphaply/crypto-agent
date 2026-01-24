from flask import Flask, render_template_string, request, jsonify
import pandas as pd
import sqlite3
import threading
import schedule
import time
from database import DB_NAME

app = Flask(__name__)

# --- 数据查询逻辑 ---
def get_db_data(symbol, limit=20):
    conn = sqlite3.connect(DB_NAME)
    df_summary = pd.read_sql_query(
        "SELECT timestamp, content, strategy_logic FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT 1", 
        conn, params=(symbol,)
    )
    df_orders = pd.read_sql_query(
        f"SELECT timestamp, side, entry_price, take_profit, stop_loss, reason FROM orders WHERE symbol = ? ORDER BY id DESC LIMIT {limit}", 
        conn, params=(symbol,)
    )
    conn.close()
    return df_summary, df_orders

# --- 移动端优化版 HTML 模板 ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Crypto Agent Mobile</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.1.3/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root { --bg-color: #0f111a; --card-bg: #1a1d2e; --accent-color: #3d5afe; }
        body { background-color: var(--bg-color); color: #cfd8dc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        
        /* 顶部导航美化 */
        .header-bar { background: linear-gradient(135deg, #1a1d2e 0%, #0f111a 100%); padding: 15px; border-bottom: 1px solid #2d324d; position: sticky; top: 0; z-index: 100; }
        .brand-title { font-size: 1.2rem; font-weight: 800; color: #fff; margin: 0; display: flex; align-items: center; }
        
        /* 卡片美化 */
        .card { background-color: var(--card-bg); border: none; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); margin-bottom: 15px; overflow: hidden; }
        .card-header { background-color: rgba(255,255,255,0.03); border-bottom: 1px solid rgba(255,255,255,0.05); padding: 12px 15px; font-weight: 600; color: #90caf9; }
        
        /* 语义化颜色 */
        .buy { color: #00e676 !important; font-weight: bold; }
        .sell { color: #ff5252 !important; font-weight: bold; }
        
        /* 移动端选择器和按钮 */
        .form-select { background-color: #262a42; border: 1px solid #3f4461; color: white; border-radius: 8px; }
        .btn-refresh { border-radius: 8px; background: var(--accent-color); border: none; font-weight: 600; }

        /* 表格容器：手机端横向滚动 */
        .table-responsive { border-radius: 8px; overflow: hidden; }
        .table { margin-bottom: 0; font-size: 0.85rem; }
        .table th { background-color: #262a42; color: #8088a2; border-none; font-weight: 500; }
        .table td { border-color: #2d324d; vertical-align: middle; }

        /* 内容文本 */
        pre { white-space: pre-wrap; font-size: 0.85rem; color: #b0bec5; margin-bottom: 0; }
        blockquote { border-left: 3px solid var(--accent-color); background: rgba(61, 90, 254, 0.05); padding: 10px; font-size: 0.85rem; border-radius: 0 8px 8px 0; }
        
        /* 针对超小屏幕微调 */
        @media (max-width: 576px) {
            .container { padding-left: 10px; padding-right: 10px; }
            .brand-title { font-size: 1.1rem; }
        }
    </style>
</head>
<body>
    <div class="header-bar mb-3">
        <div class="container d-flex justify-content-between align-items-center">
            <h1 class="brand-title">🚀 Agent Monitor</h1>
            <button class="btn btn-sm btn-primary btn-refresh" onclick="location.reload()">刷新</button>
        </div>
    </div>

    <div class="container">
        <div class="card p-2 mb-3">
            <select id="symbolSelect" class="form-select" onchange="window.location.href='?symbol='+this.value">
                {% for sym in symbols %}
                <option value="{{sym}}" {% if sym == current_symbol %}selected{% endif %}>{{sym}}</option>
                {% endfor %}
            </select>
        </div>

        <div class="card">
            <div class="card-header d-flex justify-content-between">
                <span>📈 市场分析 ({{current_symbol}})</span>
                <small class="text-muted" style="font-size: 0.7rem;">
                    {% if not summary.empty %}{{summary.iloc[0]['timestamp'].split(' ')[1]}}{% endif %}
                </small>
            </div>
            <div class="card-body">
                {% if not summary.empty %}
                <pre>{{summary.iloc[0]['content']}}</pre>
                {% else %}
                <div class="text-center py-3 text-muted">等待数据抓取...</div>
                {% endif %}
            </div>
        </div>

        <div class="card">
            <div class="card-header">🧠 Agent 思考过程</div>
            <div class="card-body">
                {% if not summary.empty %}
                <blockquote class="mb-0">
                    {{summary.iloc[0]['strategy_logic']}}
                </blockquote>
                {% endif %}
            </div>
        </div>

        <div class="card">
            <div class="card-header">📝 最近操作日志</div>
            <div class="table-responsive">
                <table class="table table-dark">
                    <thead>
                        <tr>
                            <th>方向</th>
                            <th>价格</th>
                            <th>止盈/损</th>
                            <th>理由</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for index, row in orders.iterrows() %}
                        <tr>
                            <td class="{{row['side'].lower()}}">{{row['side'].upper()}}</td>
                            <td>{{row['entry_price']}}</td>
                            <td>
                                <div class="text-success" style="font-size: 0.7rem;">T:{{row['take_profit']}}</div>
                                <div class="text-danger" style="font-size: 0.7rem;">S:{{row['stop_loss']}}</div>
                            </td>
                            <td style="max-width: 120px; font-size: 0.75rem;">{{row['reason']}}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        
        <div class="text-center text-muted mt-4 mb-5" style="font-size: 0.7rem;">
            © 2026 Crypto Multi-Agent System<br>
            Powered by Flask & Binance API
        </div>
    </div>

    <script>
        // 自动刷新逻辑（可选，每60秒刷新一次）
        // setInterval(() => { location.reload(); }, 60000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    symbol = request.args.get('symbol', 'BTC/USDT')
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    summary, orders = get_db_data(symbol)
    return render_template_string(HTML_TEMPLATE, summary=summary, orders=orders, symbols=symbols, current_symbol=symbol)

def run_scheduler():
    import schedule
    # 注意：确保 main_scheduler.py 里的 job 函数可以被导入
    from main_scheduler import job 
    schedule.every(15).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    threading.Thread(target=run_scheduler, daemon=True).start()
    # 端口 7860，生产环境建议配合 Nginx
    app.run(host='0.0.0.0', port=7860, debug=False)