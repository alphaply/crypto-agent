import json
import uuid

from flask import Blueprint, Response, jsonify, request, stream_with_context

from agent.chat_graph import (
    delete_chat_threads,
    get_chat_interrupt,
    get_chat_state,
    stream_chat,
    stream_resume_chat,
)
from database import (
    create_chat_session,
    delete_chat_session,
    delete_chat_sessions,
    get_chat_session,
    get_chat_sessions,
    touch_chat_session,
)
from routes.utils import global_config, _require_chat_auth_api, _serialize_message, logger

chat_bp = Blueprint("chat", __name__)


@chat_bp.route("/api/chat/bootstrap", methods=["GET"])
def chat_bootstrap():
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    configs = []
    for cfg in global_config.get_all_symbol_configs():
        configs.append(
            {
                "config_id": cfg.get("config_id", ""),
                "symbol": cfg.get("symbol", ""),
                "model": cfg.get("model", ""),
                "mode": cfg.get("mode", "STRATEGY"),
            }
        )
    sessions = get_chat_sessions(limit=200)
    return jsonify({"success": True, "configs": configs, "sessions": sessions})


@chat_bp.route("/api/chat/sessions", methods=["POST"])
def create_chat_session_api():
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

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
    return jsonify({"success": True, "session_id": session_id})


@chat_bp.route("/api/chat/sessions/<session_id>", methods=["GET", "DELETE"])
def chat_session_api(session_id):
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    if request.method == "DELETE":
        deleted = delete_chat_session(session_id)
        delete_chat_threads([session_id])
        return jsonify({"success": True, "deleted": deleted})

    sess = get_chat_session(session_id)
    if not sess:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    state = get_chat_state(session_id)
    messages = state.get("messages", []) if state else []
    return jsonify(
        {
            "success": True,
            "session": dict(sess),
            "messages": [_serialize_message(m) for m in messages],
            "pending_approval": get_chat_interrupt(session_id),
        }
    )


@chat_bp.route("/api/chat/sessions/<session_id>/messages", methods=["GET"])
def get_chat_messages_api(session_id):
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    sess = get_chat_session(session_id)
    if not sess:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    state = get_chat_state(session_id)
    messages = state.get("messages", []) if state else []
    return jsonify(
        {
            "success": True,
            "session": dict(sess),
            "messages": [_serialize_message(m) for m in messages],
        }
    )


@chat_bp.route("/api/chat/sessions/<session_id>/stream", methods=["GET"])
def stream_chat_api(session_id):
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    sess = get_chat_session(session_id)
    if not sess:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    user_input = request.args.get("message") or request.args.get("q")
    approval = request.args.get("approval")

    def generate():
        try:
            if approval:
                gen = stream_resume_chat(session_id, approval == "true")
            else:
                payload = {"q": user_input, "config_id": sess["config_id"]}
                gen = stream_chat(session_id, payload)

            for event in gen:
                if isinstance(event, dict):
                    yield f"data: {json.dumps(event)}\n\n"
                else:
                    # Backward compatibility for old string token events
                    yield f"data: {json.dumps({'type': 'token', 'token': event})}\n\n"

            touch_chat_session(session_id)
            state = get_chat_state(session_id)
            final_messages = [_serialize_message(m) for m in state.get("messages", [])]
            done_payload = {
                "type": "done",
                "messages": final_messages,
                "pending_approval": get_chat_interrupt(session_id),
            }
            yield f"data: {json.dumps(done_payload)}\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@chat_bp.route("/api/chat/sessions/<session_id>/summarize_title", methods=["POST"])
def summarize_session_title_api(session_id):
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    sess = get_chat_session(session_id)
    if not sess:
        return jsonify({"success": False, "message": "会话不存在"}), 404

    state = get_chat_state(session_id)
    messages = state.get("messages", []) if state else []
    if not messages:
        return jsonify({"success": False, "message": "没有消息内容"}), 400

    # 获取前几条对话内容用于总结
    content_to_summarize = ""
    for m in messages[:3]:
        role = "User" if isinstance(m, HumanMessage) else "Assistant"
        content_to_summarize += f"{role}: {m.content[:200]}\n"

    try:
        cfg = global_config.get_config_by_id(sess["config_id"])
        llm = ChatOpenAI(
            model=cfg.get("model"),
            api_key=cfg.get("api_key"),
            base_url=cfg.get("api_base"),
            temperature=0,
        )
        summary_prompt = f"请根据以下对话内容，总结一个极其简短的标题（不超过6个字，不要带标点）：\n\n{content_to_summarize}"
        res = llm.invoke([HumanMessage(content=summary_prompt)])
        new_title = res.content.strip().replace("\"", "").replace("'", "").replace("鏍欓", "").replace("锛?", "")
        if len(new_title) > 10: new_title = new_title[:10]
        
        # 更新数据库
        from database import update_chat_session_title
        update_chat_session_title(session_id, new_title)
        return jsonify({"success": True, "title": new_title})
    except Exception as e:
        logger.error(f"Title summary error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@chat_bp.route("/api/chat/sessions", methods=["DELETE"])
def delete_sessions_api():
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    ids = request.json.get("ids", [])
    if not ids:
        return jsonify({"success": True, "deleted": 0})
    deleted = delete_chat_sessions(ids)
    delete_chat_threads(ids)
    return jsonify({"success": True, "deleted": deleted})
