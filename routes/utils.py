import os
import time
import re
import sqlite3
import json
import pytz
from flask import session, jsonify
from dotenv import load_dotenv
from database import DB_NAME
from config import config as global_config
from utils.logger import setup_logger
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage, SystemMessage

# 全局初始化
load_dotenv(dotenv_path='.env', override=True)
logger = setup_logger("Dashboard")
TZ_CN = pytz.timezone(getattr(global_config, 'timezone', 'Asia/Shanghai'))

# --- 认证辅助 ---

def _chat_password():
    return os.getenv("CHAT_PASSWORD") or os.getenv("ADMIN_PASSWORD")

def _admin_authed() -> bool:
    return bool(session.get("admin_authed", False) or session.get("chat_authed", False))

def _chat_authed() -> bool:
    return _admin_authed()

def _require_chat_auth_api():
    if not _admin_authed():
        return jsonify({"success": False, "message": "未授权，请先输入密码"}), 401
    return None

def _require_admin_auth_api():
    return _require_chat_auth_api()

# --- 消息序列化辅助 ---

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
        # 提取推理内容
        reasoning = msg.additional_kwargs.get("reasoning_content") or \
                    msg.response_metadata.get("reasoning_content") or ""
        if reasoning:
            payload["reasoning_content"] = reasoning
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

# --- 数据获取辅助 ---

def get_scheduler_status():
    """获取调度器状态"""
    return os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true'

def get_all_configs():
    """读取所有配置"""
    try:
        return global_config.get_all_symbol_configs()
    except:
        return []

def get_symbol_specific_status(symbol):
    """计算特定币种的运行状态"""
    configs = get_all_configs()
    symbol_configs = [c for c in configs if c.get('symbol') == symbol]
    if not symbol_configs: return "未知", "N/A", False

    has_real = False
    has_strategy = False
    has_dca = False
    is_any_enabled = False

    for config in symbol_configs:
        if config.get('enabled', True):
            is_any_enabled = True
            mode = config.get('mode', 'STRATEGY').upper()
            if mode == 'REAL': has_real = True
            elif mode == 'SPOT_DCA': has_dca = True
            else: has_strategy = True

    if not is_any_enabled: return "🚫 已禁用", "无执行任务", False
    
    # 优先级显示逻辑
    status_parts = []
    freq_parts = []
    
    if has_real:
        status_parts.append("🔴 实盘")
        freq_parts.append("15m")
    if has_dca:
        status_parts.append("🟡 定投")
        freq_parts.append("Daily")
    if has_strategy:
        status_parts.append("🔵 策略")
        freq_parts.append("1h")
        
    status_text = " + ".join(status_parts)
    freq_text = "混合 (" + "/".join(freq_parts) + ")" if len(freq_parts) > 1 else freq_parts[0] + (" (高频)" if has_real else (" (定投)" if has_dca else " (低频)"))
    
    return status_text, freq_text, True
