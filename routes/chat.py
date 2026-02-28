import uuid
import json
from flask import Blueprint, request, jsonify, Response, stream_with_context
from routes.utils import (
    global_config, _require_chat_auth_api, _serialize_message, 
    _extract_interrupt, _latest_ai_text, logger
)
from database import (
    create_chat_session, get_chat_sessions, get_chat_session,
    touch_chat_session, delete_chat_session, delete_chat_sessions
)
from agent.chat_graph import (
    invoke_chat, resume_chat, get_chat_state, get_chat_interrupt,
    delete_chat_threads, stream_chat, stream_resume_chat
)

chat_bp = Blueprint('chat', __name__)

@chat_bp.route('/api/chat/bootstrap', methods=['GET'])
def chat_bootstrap():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err

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

@chat_bp.route('/api/chat/sessions', methods=['POST'])
def create_chat_session_api():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err

    data = request.json or {}
    config_id = data.get("config_id")
    if not config_id: return jsonify({"success": False, "message": "缺少 config_id"}), 400

    cfg = global_config.get_config_by_id(config_id)
    if not cfg: return jsonify({"success": False, "message": "配置不存在"}), 404

    session_id = uuid.uuid4().hex
    symbol = cfg.get("symbol", "")
    title = data.get("title") or f"{symbol} · {cfg.get('mode', 'STRATEGY')}"
    create_chat_session(session_id, config_id, symbol, title)
    return jsonify({"success": True, "session_id": session_id})

@chat_bp.route('/api/chat/sessions/<session_id>', methods=['GET'])
def get_chat_session_api(session_id):
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err

    sess = get_chat_session(session_id)
    if not sess: return jsonify({"success": False, "message": "会话不存在"}), 404

    state = get_chat_state(session_id)
    messages = state.get("messages", []) if state else []
    
    return jsonify({
        "success": True,
        "session": dict(sess),
        "messages": [_serialize_message(m) for m in messages],
        "pending_approval": _extract_interrupt(get_chat_interrupt(session_id)),
    })

@chat_bp.route('/api/chat/sessions/<session_id>/stream', methods=['GET'])
def stream_chat_api(session_id):
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err

    sess = get_chat_session(session_id)
    if not sess: return jsonify({"success": False, "message": "会话不存在"}), 404

    user_input = request.args.get("q")
    approval = request.args.get("approval")

    def generate():
        try:
            if approval:
                gen = stream_resume_chat(session_id, approval == "true")
            else:
                gen = stream_chat(session_id, user_input, sess["config_id"])

            for event in gen:
                yield f"data: {json.dumps(event)}

"

            # 结束后同步状态
            touch_chat_session(session_id)
            state = get_chat_state(session_id)
            final_messages = [_serialize_message(m) for m in state.get("messages", [])]
            yield f"data: {json.dumps({'type': 'done', 'messages': final_messages, 'pending_approval': _extract_interrupt(get_chat_interrupt(session_id))})}

"
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}

"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@chat_bp.route('/api/chat/sessions', methods=['DELETE'])
def delete_sessions_api():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    ids = request.json.get("ids", [])
    if not ids: return jsonify({"success": True, "deleted": 0})
    deleted = delete_chat_sessions(ids)
    delete_chat_threads(ids)
    return jsonify({"success": True, "deleted": deleted})
