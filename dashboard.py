from flask import Flask, render_template_string, request
import pandas as pd
import sqlite3
import threading
import time
import os
from database import DB_NAME, init_db
from main_scheduler import job 
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

app = Flask(__name__)

def get_db_data(symbol, limit=20):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        df_summary = pd.read_sql_query(
            "SELECT timestamp, content, strategy_logic FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT 1", 
            conn, params=(symbol,)
        )
        # 获取操作日志
        df_orders = pd.read_sql_query(
            "SELECT * FROM orders WHERE symbol = ? ORDER BY id DESC LIMIT ?", 
            conn, params=(symbol, limit)
        )
        conn.close()
        return df_summary, df_orders
    except Exception as e:
        print(f"Database error: {e}")
        return pd.DataFrame(), pd.DataFrame()

# --- 电脑+手机自适应布局模板 ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Crypto Agent Terminal v2</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.1.3/css/bootstrap.min.css" rel="stylesheet">
    <style>
        :root { --bg: #0b0e11; --card: #181a20; --text: #eaecef; --accent: #f0b90b; --border: #2b2f36; }
        body { background-color: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; }
        
        /* 响应式 Grid：电脑 2 列，手机 1 列 */
        .main-grid {
            display: grid;
            grid-template-columns: 1.2fr 0.8fr;
            gap: 20px;
            padding: 20px;
        }
        @media (max-width: 992px) {
            .main-grid { grid-template-columns: 1fr; padding: 10px; }
        }

        .header-bar { 
            background: var(--card); border-bottom: 1px solid var(--border); 
            padding: 12px 20px; position: sticky; top: 0; z-index: 1000;
            display: flex; justify-content: space-between; align-items: center;
        }

        .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; height: 100%; }
        .card-header { border-bottom: 1px solid var(--border); font-weight: bold; color: var(--accent); background: none; }
        
        /* 状态样式 */
        .buy { color: #0ecb81; font-weight: bold; }
        .sell { color: #f6465d; font-weight: bold; }
        
        /* 撤单特效：整行变灰+中划线 */
        .cancel-row { 
            color: #5d6673 !important; 
            text-decoration: line-through; 
            opacity: 0.5;
        }
        .cancel-label { 
            background: #2b2f36; color: #848e9c; padding: 2px 6px; 
            border-radius: 4px; text-decoration: none !important; display: inline-block; font-size: 0.7rem;
        }

        pre { background: #1e2329; padding: 15px; border-radius: 8px; color: #b7bdc6; white-space: pre-wrap; font-size: 0.85rem; }
        .time-tag { font-size: 0.75rem; color: #848e9c; margin-bottom: 4px; display: block; }
        
        .table { color: var(--text); border-color: var(--border); }
        .table td { vertical-align: middle; border-bottom: 1px solid var(--border); }
        
        /* 手机端理由列宽度限制 */
        .reason-cell { font-size: 0.8rem; line-height: 1.4; max-width: 200px; }
    </style>
</head>
<body>
    <div class="header-bar">
        <h5 class="mb-0">🛡️ Agent Dashboard</h5>
        <div class="d-flex gap-2">
            <select class="form-select form-select-sm bg-dark text-light border-secondary" onchange="window.location.href='?symbol='+this.value">
                {% for sym in symbols %}
                <option value="{{sym}}" {% if sym == current_symbol %}selected{% endif %}>{{sym}}</option>
                {% endfor %}
            </select>
            <button class="btn btn-sm btn-warning fw-bold" onclick="location.reload()">刷新</button>
        </div>
    </div>

    <div class="main-grid">
        <div class="analysis-section">
            <div class="card shadow-sm">
                <div class="card-header">📊 核心分析与共振指标</div>
                <div class="card-body">
                    {% if not summary.empty %}
                    <div class="mb-3">
                        <span class="time-tag">最后更新: {{summary.iloc[0]['timestamp']}}</span>
                        <pre>{{summary.iloc[0]['content']}}</pre>
                    </div>
                    <div class="p-3 rounded" style="background: rgba(240,185,11,0.05); border-left: 4px solid var(--accent);">
                        <h6 class="text-warning small mb-2">🧠 Agent 策略思维：</h6>
                        <div class="small">{{summary.iloc[0]['strategy_logic']}}</div>
                    </div>
                    {% else %}
                    <div class="text-center py-5 text-muted">等待第一次行情抓取完成...</div>
                    {% endif %}
                </div>
            </div>
        </div>

        <div class="log-section">
            <div class="card shadow-sm">
                <div class="card-header">📝 操作日志 (含撤单同步)</div>
                <div class="table-responsive">
                    <table class="table table-dark table-hover mb-0">
                        <thead class="text-muted" style="font-size: 0.75rem;">
                            <tr>
                                <th>动作/时间</th>
                                <th>入场价</th>
                                <th>理由</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for index, row in orders.iterrows() %}
                            <tr class="{% if row['side'].upper() == 'CANCEL' %}cancel-row{% endif %}">
                                <td>
                                    <span class="time-tag">{{row['timestamp'].split(' ')[1]}}</span>
                                    {% if row['side'].upper() == 'CANCEL' %}
                                        <span class="cancel-label">CANCEL</span>
                                    {% else %}
                                        <span class="{{row['side'].lower()}}">{{row['side'].upper()}}</span>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if row['side'].upper() != 'CANCEL' %}
                                        {{row['entry_price']}}
                                    {% else %}
                                        --
                                    {% endif %}
                                </td>
                                <td class="reason-cell text-muted">
                                    {{row['reason']}}
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    symbol = request.args.get('symbol', 'BTC/USDT')
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
    summary, orders = get_db_data(symbol)
    return render_template_string(HTML_TEMPLATE, summary=summary, orders=orders, symbols=symbols, current_symbol=symbol)

# 修改 dashboard.py 中的 run_scheduler 函数
def run_scheduler():
    import schedule
    print("--- [System] Scheduler Thread Started ---")
    # 延迟 5 秒执行，确保 Flask 已经绑定端口并正常响应请求
    time.sleep(5) 
    job() 
    schedule.every(15).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    init_db() 
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=7860, debug=False)