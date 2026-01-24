import gradio as gr
import pandas as pd
import sqlite3
import plotly.graph_objects as go
from database import DB_NAME
from market_data import MarketTool

# 实例化工具 (仅用于画图时的 API 请求)
tool = MarketTool()

# 定义支持的币种列表 (需要和 main_scheduler.py 保持一致)
TARGET_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]

def get_db_data(symbol):
    """
    只读取数据库，不请求 API
    根据 symbol 过滤数据
    """
    conn = sqlite3.connect(DB_NAME)
    
    # 1. 获取该币种最新的总结
    query_summary = "SELECT timestamp, content, strategy_logic FROM summaries WHERE symbol = ? ORDER BY id DESC LIMIT 1"
    df_summary = pd.read_sql_query(query_summary, conn, params=(symbol,))
    
    # 2. 获取该币种的订单记录 (这里读的是历史记录表，或者是你存 log 的 orders 表)
    # 假设你使用的是之前定义的 orders 表用于记录操作日志
    query_orders = "SELECT timestamp, side, entry_price, take_profit, stop_loss, reason FROM orders WHERE symbol = ? ORDER BY id DESC LIMIT 20"
    try:
        df_orders = pd.read_sql_query(query_orders, conn, params=(symbol,))
    except:
        # 兼容性处理：如果表里还没有 symbol 字段 (旧数据)，则不过滤
        df_orders = pd.read_sql_query("SELECT timestamp, side, entry_price, take_profit, stop_loss, reason FROM orders ORDER BY id DESC LIMIT 20", conn)
    
    # 3. (可选) 如果你想看当前的“模拟挂单池” (Mock Orders)，可以加一个查询
    # query_mock = "SELECT order_id, side, price, amount, status FROM mock_orders WHERE symbol = ? AND status='OPEN'"
    # df_mock = pd.read_sql_query(query_mock, conn, params=(symbol,))

    conn.close()
    return df_summary, df_orders

def draw_kline(symbol):
    """
    【耗时操作】仅在用户点击加载 K 线时调用
    请求 Binance API 并画图
    """
    print(f"Drawing chart for {symbol}...")
    try:
        # 获取 1H 数据用于画图
        data_full = tool.get_market_analysis(symbol)
        
        if not data_full or 'analysis' not in data_full or '1h' not in data_full['analysis']:
            return go.Figure().update_layout(title=f"无数据: {symbol}")
            
        analysis_1h = data_full['analysis']['1h']
        if 'df_raw' not in analysis_1h:
            return go.Figure().update_layout(title=f"无 K 线数据: {symbol}")

        df = analysis_1h['df_raw']
        
        # 计算 EMA200 (用于画图)
        df['ema200_line'] = df['close'].ewm(span=200, adjust=False).mean()

        fig = go.Figure(data=[go.Candlestick(x=df['time'],
                    open=df['open'], high=df['high'],
                    low=df['low'], close=df['close'], name=f'{symbol} 1H')])
        
        fig.add_trace(go.Scatter(x=df['time'], y=df['ema200_line'], line=dict(color='orange', width=1), name='EMA 200'))
        
        # 标题和布局
        current_price = df['close'].iloc[-1]
        fig.update_layout(
            title=f'{symbol} 1H Analysis | Price: {current_price}',
            height=600, 
            template='plotly_dark',
            xaxis_rangeslider_visible=False
        )
        return fig
    except Exception as e:
        print(f"Chart Error: {e}")
        return go.Figure().update_layout(title=f"图表加载失败: {e}")

def refresh_text_data(symbol):
    """
    快速刷新：只更新文本和表格，不画图
    """
    df_sum, df_ord = get_db_data(symbol)
    
    if not df_sum.empty:
        latest = df_sum.iloc[0]
        # 顶格写法，确保 Markdown 渲染正确
        markdown_text = f"""### 🕒 {symbol} 更新: {latest['timestamp']}

**📈 市场分析**:
{latest['content']}

**🧠 Agent 思考**:
> {latest['strategy_logic']}"""

    else:
        markdown_text = f"暂无 {symbol} 的分析数据，请等待 Agent 运行..."
    
    return markdown_text, df_ord

# --- UI Layout ---

with gr.Blocks(title="🤖 Crypto Multi-Agent Dashboard", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🚀 Quant Agent 监控面板 (多币种版)")
    
    # 顶部控制栏
    with gr.Row():
        symbol_dropdown = gr.Dropdown(
            choices=TARGET_SYMBOLS, 
            value="BTC/USDT", 
            label="选择币种", 
            interactive=True
        )
        refresh_btn = gr.Button("🔄 刷新数据 (DB)", variant="primary")
        chart_btn = gr.Button("📊 加载/刷新 K线 (API)", variant="secondary")
    
    with gr.Tabs():
        with gr.TabItem("📊 仪表盘"):
            with gr.Row():
                # 左侧：Agent 分析 (Markdown)
                summary_box = gr.Markdown("请点击刷新数据...")
                
                # 右侧：K线图 (Plotly)
                market_chart = gr.Plot(label="Market Chart")
            
            gr.Markdown("### 📝 操作日志 (Order Log)")
            order_table = gr.DataFrame(headers=["Time", "Side", "Entry", "TP", "SL", "Reason"])

        with gr.TabItem("🗄️ 历史数据"):
            gr.Markdown("暂未连接历史存档表")

    # --- 事件绑定 ---
    
    # 1. 点击“刷新数据”：只更新 文本框 和 表格 (速度快)
    refresh_btn.click(
        fn=refresh_text_data, 
        inputs=[symbol_dropdown], 
        outputs=[summary_box, order_table]
    )
    
    # 2. 点击“加载K线”：只更新 图表 (速度慢，消耗API)
    chart_btn.click(
        fn=draw_kline,
        inputs=[symbol_dropdown],
        outputs=[market_chart]
    )
    
    # 3. 切换币种时：自动刷新文本数据 (可选，体验更好)
    symbol_dropdown.change(
        fn=refresh_text_data,
        inputs=[symbol_dropdown],
        outputs=[summary_box, order_table]
    )
    
    # 4. 切换币种时：清空当前K线，防止误导 (可选)
    # symbol_dropdown.change(lambda: go.Figure(), outputs=[market_chart])

    # 初始化加载文本数据
    demo.load(refresh_text_data, inputs=[symbol_dropdown], outputs=[summary_box, order_table])

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)