from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
import sqlite3
import threading
import math
import json
import os
import uuid
import time
from datetime import datetime
import re
import pytz
from database import (
    DB_NAME, init_db, 
    get_paginated_summaries, get_summary_count, delete_summaries_by_symbol,
    get_balance_history, get_trade_history, clean_financial_data,
    get_active_agents, create_chat_session, get_chat_sessions, get_chat_session,
    touch_chat_session, delete_chat_session, delete_chat_sessions
)
from main_scheduler import run_smart_scheduler, get_next_run_settings
from dotenv import load_dotenv
from utils.logger import setup_logger
from config import config as global_config
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage
from agent.chat_graph import invoke_chat, resume_chat, get_chat_state, delete_chat_threads, stream_chat

load_dotenv(dotenv_path='.env', override=True)
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.getenv("ADMIN_PASSWORD", "dev-secret"))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
TZ_CN = pytz.timezone('Asia/Shanghai')
logger = setup_logger("Dashboard")


def _chat_password():
    return os.getenv("CHAT_PASSWORD") or os.getenv("ADMIN_PASSWORD")


def _chat_authed() -> bool:
    return bool(session.get("chat_authed", False))


def _require_chat_auth_api():
    if not _chat_authed():
        return jsonify({"success": False, "message": "未授权，请先输入密码"}), 401
    return None


def _serialize_message(msg):
    role = "assistant"
    if isinstance(msg, HumanMessage):
        role = "user"
    elif isinstance(msg, ToolMessage):
        role = "tool"
    elif isinstance(msg, SystemMessage):
        role = "system"

    payload = {
        "role": role,
        "content": msg.content,
    }
    if isinstance(msg, AIMessage):
        payload["tool_calls"] = getattr(msg, "tool_calls", []) or []
    return payload


def _extract_interrupt(result):
    interrupts = result.get("__interrupt__", []) if isinstance(result, dict) else []
    if not interrupts:
        return None
    intr = interrupts[0]
    value = getattr(intr, "value", {}) or {}
    return {
        "id": getattr(intr, "id", ""),
        "value": value,
    }


def _latest_ai_text(messages):
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            return msg.get("content", "") or ""
    return ""

def get_scheduler_status():
    """获取调度器状态，根据环境变量决定是否运行调度器"""
    scheduler_enabled = os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true'
    return scheduler_enabled


def get_dashboard_data(symbol, page=1, per_page=10):
    try:
        conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        conn.row_factory = sqlite3.Row 
        
        # 1. 获取该币种下活跃的所有 Agent 的最新一条分析
        agents_query = "SELECT DISTINCT agent_name FROM summaries WHERE symbol = ?"
        agents = [row['agent_name'] for row in conn.execute(agents_query, (symbol,)).fetchall()]
        
        agent_summaries = []
        for agent in agents:
            latest_summary = conn.execute(
                "SELECT * FROM summaries WHERE symbol = ? AND agent_name = ? ORDER BY id DESC LIMIT 1",
                (symbol, agent)
            ).fetchone()
            if latest_summary:
                summary_dict = dict(latest_summary)

                # 🔥 新增：通过 config_id 获取配置信息，添加友好的显示名称
                config_id = agent  # agent_name 就是 config_id
                config = global_config.get_config_by_id(config_id)

                # 向后兼容：如果通过 config_id 找不到，尝试通过 model 名称匹配
                if not config:
                    for cfg in global_config.get_all_symbol_configs():
                        if cfg.get('symbol') == symbol and cfg.get('model') == agent:
                            config = cfg
                            break

                if config:
                    summary_dict['model'] = config.get('model', 'Unknown')
                    summary_dict['mode'] = config.get('mode', 'STRATEGY')
                    summary_dict['leverage'] = global_config.get_leverage(config.get('config_id'))
                    # 优化display_name，加入config_id后缀以便区分相同model+mode的配置
                    summary_dict['display_name'] = f"{config.get('model', 'Unknown')} ({config.get('mode', 'STRATEGY')})"
                else:
                    # 完全找不到配置，使用默认值
                    summary_dict['model'] = agent  # 直接显示 agent_name
                    summary_dict['mode'] = 'Unknown'
                    summary_dict['leverage'] = global_config.leverage
                    summary_dict['display_name'] = agent

                # 获取该 Agent 最近的 5 条决策记录
                recent_agent_orders = conn.execute(
                    "SELECT * FROM orders WHERE symbol = ? AND agent_name = ? ORDER BY id DESC LIMIT 5",
                    (symbol, agent)
                ).fetchall()

                # 🔥 修改处：提取 validity 字段
                processed_orders = []
                for o in recent_agent_orders:
                    d = dict(o)
                    # 从 reason 中提取 (Valid: Xh)
                    match = re.search(r"\(Valid:\s*(\d+h)\)", d.get('reason', ''))
                    d['validity'] = match.group(1) if match else None
                    processed_orders.append(d)

                summary_dict['recent_orders'] = processed_orders

                agent_summaries.append(summary_dict)

        # 2. 获取订单 (保持不变)
        offset = (page - 1) * per_page
        total_count = conn.execute("SELECT COUNT(*) FROM orders WHERE symbol = ?", (symbol,)).fetchone()[0]
        
        cursor = conn.execute(
            "SELECT * FROM orders WHERE symbol = ? ORDER BY id DESC LIMIT ? OFFSET ?", 
            (symbol, per_page, offset)
        )
        orders = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        return agent_summaries, orders, total_count
    except Exception as e:
        logger.error(f"Error: {e}")
        return [], [], 0
def get_all_configs():
    """读取所有配置的辅助函数（使用统一配置管理）"""
    try:
        return global_config.get_all_symbol_configs()
    except Exception as e:
        logger.error(f"❌ 配置获取失败: {e}")
        return []

def get_configured_symbols():
    configs = get_all_configs()
    symbols = [cfg['symbol'] for cfg in configs if 'symbol' in cfg]
    # 去重
    seen = set()
    unique = []
    for s in symbols:
        if s not in seen:
            unique.append(s)
            seen.add(s)
    if not unique: return ["BTC/USDT", "ETH/USDT"]
    return unique

def get_symbol_specific_status(symbol):
    """
    计算特定币种的当前运行状态和频率
    支持多配置显示
    """
    configs = get_all_configs()
    # 找到当前币种的所有配置
    symbol_configs = [c for c in configs if c.get('symbol') == symbol]

    if not symbol_configs:
        return "未知", "N/A"

    # 收集所有模式
    modes = set()
    has_real = False
    has_strategy = False

    for config in symbol_configs:
        mode = config.get('mode', 'STRATEGY').upper()
        modes.add(mode)
        if mode == 'REAL':
            has_real = True
        else:
            has_strategy = True

    # 构建模式文本
    if has_real and has_strategy:
        mode_text = "🔵 策略 + 🔴 实盘"
        freq_text = "混合 (15m/1h)"
    elif has_real:
        mode_text = "🔴 实盘模式 (Real)"
        freq_text = "15m (高频执行)"
    else:
        mode_text = "🔵 策略模式 (Strategy)"
        freq_text = "1h (低频执行)"

    return mode_text, freq_text

@app.route('/')
def index():
    symbols = get_configured_symbols()
    symbol = request.args.get('symbol', symbols[0] if symbols else 'BTC/USDT')
    page = int(request.args.get('page', 1))
    per_page = 10
    
    agent_summaries, orders, total_count = get_dashboard_data(symbol, page, per_page)
    
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1

    # 1. 获取特定币种的状态 (新增)
    symbol_mode, symbol_freq = get_symbol_specific_status(symbol)
    
    # 2. 获取调度器状态
    scheduler_enabled = get_scheduler_status()

    # 获取资金曲线数据 (新增)
    balance_history = get_balance_history(symbol, limit=200)
    
    # 获取历史成交记录 (新增)
    trade_history = get_trade_history(symbol, limit=50)

    # 处理资金曲线数据给前端 Chart.js 使用
    chart_labels = [row['timestamp'][5:16] for row in balance_history] # 只取 MM-DD HH:MM
    chart_data = [row['total_equity'] for row in balance_history]

    return render_template(
        'dashboard.html', 
        agent_summaries=agent_summaries, 
        orders=orders, 
        symbols=symbols, 
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        total_orders=total_count,
        # 传给前端的变量改了
        symbol_mode=symbol_mode,
        symbol_freq=symbol_freq,
        scheduler_enabled=scheduler_enabled,
        balance_history=balance_history,
        trade_history=trade_history,
        chart_labels=chart_labels,
        chart_data=chart_data,
    )




@app.route('/history')
def history_view():
    symbol = request.args.get('symbol', 'BTC/USDT')
    agent_filter = request.args.get('agent', 'ALL') # 获取筛选参数，默认为 ALL
    
    page = int(request.args.get('page', 1))
    per_page = 10 
    
    # 1. 获取数据 (传入筛选参数)
    summaries = get_paginated_summaries(symbol, page, per_page, agent_name=agent_filter)
    total_count = get_summary_count(symbol, agent_name=agent_filter)
    total_pages = math.ceil(total_count / per_page) if total_count > 0 else 1
    
    # 2. 获取筛选器列表
    active_agents = get_active_agents(symbol)
    
    return render_template(
        'history.html', 
        summaries=summaries,
        current_symbol=symbol,
        current_page=page,
        total_pages=total_pages,
        total_count=total_count,
        active_agents=active_agents, # 传给前端生成按钮
        current_agent=agent_filter   # 传给前端标记当前选中状态
    )

# 3. 新增路由：删除历史 (API)
@app.route('/api/clean_history', methods=['POST'])
def clean_history():
    data = request.json
    password = data.get('password')
    symbol = data.get('symbol')
    
    # 验证密码
    admin_pass = os.getenv('ADMIN_PASSWORD')
    if not admin_pass:
        return jsonify({'success': False, 'message': '服务端未配置 ADMIN_PASSWORD'})
        
    if password != admin_pass:
        return jsonify({'success': False, 'message': '密码错误，拒绝操作'})
        
    try:
        # 删除分析记录
        count_summary = delete_summaries_by_symbol(symbol)
        
        # 删除资金和成交记录 (新增)
        count_financial = clean_financial_data(symbol)
        
        logger.info(f"🗑️ [Dashboard] Cleaned all data for {symbol}")
        return jsonify({'success': True, 'message': f'已删除 {count_summary} 条分析, {count_financial} 条财务记录'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/scheduler-status', methods=['GET'])
def get_scheduler_status_api():
    """API接口：返回调度器状态"""
    status = get_scheduler_status()
    return jsonify({"enabled": status})


@app.route('/api/toggle-scheduler', methods=['POST'])
def toggle_scheduler():
    """API接口：切换调度器状态"""
    data = request.json
    enable = data.get('enable', None)
    if enable is not None:
        # 注意：这里只是模拟设置，实际需要重启调度器
        logger.info(f"调度器状态切换请求: {'启用' if enable else '禁用'}")
        return jsonify({"success": True, "enabled": enable})
    else:
        return jsonify({"success": False, "message": "参数错误"})


@app.route('/api/configs', methods=['GET'])
def get_configs_api():
    """API接口：获取所有配置列表"""
    try:
        configs = global_config.get_all_symbol_configs()
        # 返回配置信息，包括 config_id、symbol、model、mode 等
        config_list = []
        for cfg in configs:
            config_list.append({
                'config_id': cfg.get('config_id', 'unknown'),
                'symbol': cfg.get('symbol'),
                'model': cfg.get('model'),
                'mode': cfg.get('mode', 'STRATEGY'),
                'temperature': cfg.get('temperature', 0.5)
            })
        return jsonify({'success': True, 'configs': config_list})
    except Exception as e:
        logger.error(f"获取配置列表失败: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/chat')
def chat_view():
    return render_template('chat.html', authed=_chat_authed())


@app.route('/api/chat/auth', methods=['POST'])
def chat_auth():
    data = request.json or {}
    password = data.get("password", "")
    expected = _chat_password()
    if not expected:
        return jsonify({"success": False, "message": "服务端未配置聊天密码"}), 500
    if password != expected:
        return jsonify({"success": False, "message": "密码错误"}), 401
    session["chat_authed"] = True
    return jsonify({"success": True})


@app.route('/api/chat/bootstrap', methods=['GET'])
def chat_bootstrap():
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    configs = []
    for cfg in global_config.get_all_symbol_configs():
        configs.append({
            "config_id": cfg.get("config_id", ""),
            "symbol": cfg.get("symbol", ""),
            "model": cfg.get("model", ""),
            "mode": cfg.get("mode", "STRATEGY"),
        })
    sessions = get_chat_sessions(limit=200)
    return jsonify({"success": True, "configs": configs, "sessions": sessions})


@app.route('/api/chat/sessions', methods=['POST'])
def create_chat_session_api():
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    data = request.json or {}
    config_id = data.get("config_id")
    if not config_id:
        return jsonify({"success": False, "message": "缺少 config_id"}), 400

    cfg = global_config.get_config_by_id(config_id)
    if not cfg:
        return jsonify({"success": False, "message": "配置不存在"}), 404

    session_id = uuid.uuid4().hex
    symbol = cfg.get("symbol", "")
    title = data.get("title") or f"{symbol} · {cfg.get('mode', 'STRATEGY')}"
    create_chat_session(session_id, config_id, symbol, title)
    return jsonify({
        "success": True,
        "session": {
            "session_id": session_id,
            "title": title,
            "config_id": config_id,
            "symbol": symbol,
        },
    })


@app.route('/api/chat/sessions/<session_id>/messages', methods=['GET'])
def get_chat_messages_api(session_id):
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    chat_meta = get_chat_session(session_id)
    if not chat_meta:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    state = get_chat_state(session_id) or {}
    messages = [_serialize_message(m) for m in state.get("messages", [])]
    return jsonify({"success": True, "session": chat_meta, "messages": messages})


@app.route('/api/chat/sessions/<session_id>', methods=['DELETE'])
def delete_chat_session_api(session_id):
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    deleted = delete_chat_session(session_id)
    delete_chat_threads([session_id])
    if deleted <= 0:
        return jsonify({"success": False, "message": "会话不存在"}), 404
    return jsonify({"success": True, "deleted": deleted})


@app.route('/api/chat/sessions', methods=['DELETE'])
def delete_chat_sessions_api():
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    data = request.json or {}
    session_ids = data.get("session_ids") or []
    if not isinstance(session_ids, list) or not session_ids:
        return jsonify({"success": False, "message": "session_ids 不能为空"}), 400

    deleted_meta = delete_chat_sessions(session_ids)
    delete_chat_threads(session_ids)
    return jsonify({"success": True, "deleted": deleted_meta})


@app.route('/api/chat/sessions/<session_id>/stream', methods=['GET'])
def stream_chat_message_api(session_id):
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    chat_meta = get_chat_session(session_id)
    if not chat_meta:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    message = (request.args.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "message": "message 不能为空"}), 400

    cfg = global_config.get_config_by_id(chat_meta["config_id"])
    if not cfg:
        return jsonify({"success": False, "message": "配置不存在"}), 404

    def sse(payload):
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    @stream_with_context
    def generate():
        try:
            payload = {
                "messages": [HumanMessage(content=message)],
                "config_id": chat_meta["config_id"],
                "symbol": chat_meta["symbol"],
                "agent_config": cfg,
            }
            
            final_message = None
            for chunk in stream_chat(session_id, payload):
                # We can get AIMessageChunk objects here
                if chunk.content:
                    yield sse({"type": "chunk", "content": chunk.content})
                if final_message is None:
                    final_message = chunk
                else:
                    final_message += chunk

            # After streaming, check for tool calls
            if final_message and final_message.tool_calls:
                # The current UI expects a pending approval flow.
                # We need to replicate the data structure for the frontend.
                # For now, we will just send a 'done' message. A more complex UI change would be needed to handle this.
                yield sse({"type": "done", "has_tool_calls": True})
            else:
                yield sse({"type": "done", "has_tool_calls": False})

            touch_chat_session(session_id)

        except Exception as e:
            logger.error(f"chat stream failed: {e}", exc_info=True)
            yield sse({"type": "error", "message": str(e)})

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route('/api/chat/sessions/<session_id>/send', methods=['POST'])
def send_chat_message_api(session_id):
    auth_resp = _require_chat_auth_api()
    if auth_resp:
        return auth_resp

    chat_meta = get_chat_session(session_id)
    if not chat_meta:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    data = request.json or {}
    message = (data.get("message") or "").strip()
    approval = data.get("approval")

    cfg = global_config.get_config_by_id(chat_meta["config_id"])
    if not cfg:
        return jsonify({"success": False, "message": "配置不存在"}), 404

    try:
        if approval is not None:
            result = resume_chat(session_id, bool(approval))
        else:
            if not message:
                return jsonify({"success": False, "message": "message 不能为空"}), 400
            payload = {
                "messages": [HumanMessage(content=message)],
                "config_id": chat_meta["config_id"],
                "symbol": chat_meta["symbol"],
                "agent_config": cfg,
            }
            result = invoke_chat(session_id, payload)

        state = get_chat_state(session_id) or {}
        messages = [_serialize_message(m) for m in state.get("messages", [])]
        pending = _extract_interrupt(result)
        touch_chat_session(session_id)
        return jsonify({
            "success": True,
            "messages": messages,
            "pending_approval": pending,
        })
    except Exception as e:
        logger.error(f"chat send failed: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


if __name__ == "__main__":
    init_db() 
    # 检查是否启用调度器
    if get_scheduler_status():
        scheduler_thread = threading.Thread(target=run_smart_scheduler, daemon=True)
        scheduler_thread.start()
        print("✅ 定时任务已启动")
    else:
        print("❌ 定时任务已被禁用，仅运行网页服务")
    app.run(host='0.0.0.0', port=7860, debug=False)
