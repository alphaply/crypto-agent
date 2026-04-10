import os
import json
import re
from flask import Blueprint, request, jsonify, Response
from routes.utils import (
    global_config, _require_chat_auth_api, logger
)
from datetime import datetime
from database import get_config_dependency_counts, purge_config_all_data


def _write_symbol_configs_to_env(new_configs):
    with open('.env', 'r', encoding='utf-8') as f:
        content = f.read()

    val = json.dumps(new_configs, ensure_ascii=False)
    pattern = re.compile(r'^SYMBOL_CONFIGS=.*?(?=\n\w+=|\n#|$)', re.MULTILINE | re.DOTALL)
    new_entry = f"SYMBOL_CONFIGS='{val}'"
    if pattern.search(content):
        content = pattern.sub(new_entry, content)
    else:
        content += f"\n{new_entry}\n"

    with open('.env', 'w', encoding='utf-8') as f:
        f.write(content.strip() + '\n')

config_bp = Blueprint('config', __name__)

PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent", "prompts")
BLOCKED_PROMPT_FILES = set()

@config_bp.route('/api/config/raw', methods=['GET'])
def get_raw_config():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    try:
        configs = global_config.get_all_symbol_configs()
        return jsonify({"success": True, "configs": configs, "global": {
            "leverage": global_config.leverage,
            "enable_scheduler": os.getenv('ENABLE_SCHEDULER', 'true').lower() == 'true',
            "trading_mode": getattr(global_config, 'trading_mode', 'MIXED')
        }})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@config_bp.route('/api/config/save', methods=['POST'])
def save_config_api():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    
    data = request.json
    new_configs = data.get('configs')
    global_settings = data.get('global', {})

    if new_configs is None:
        return jsonify({"success": False, "message": "配置不能为空"}), 400

    try:
        with open('.env', 'r', encoding='utf-8') as f:
            content = f.read()

        updates = {
            'SYMBOL_CONFIGS': json.dumps(new_configs, ensure_ascii=False),
            'LEVERAGE': str(global_settings.get('leverage', global_config.leverage)),
            'ENABLE_SCHEDULER': 'true' if global_settings.get('enable_scheduler', True) else 'false'
        }

        for key, val in updates.items():
            # 修复处的正则，使用单行字符串定义
            pattern = re.compile(rf'^{key}=.*?(?=\n\w+=|\n#|$)', re.MULTILINE | re.DOTALL)
            new_entry = f"{key}='{val}'"
            if pattern.search(content):
                content = pattern.sub(new_entry, content)
            else:
                content += f"\n{new_entry}\n"

        with open('.env', 'w', encoding='utf-8') as f:
            f.write(content.strip() + '\n')

        global_config.reload_config()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"❌ 保存配置失败: {e}")
        return jsonify({"success": False, "message": str(e)})

@config_bp.route('/api/config/export', methods=['GET'])
def export_config():
    auth_err = _require_chat_auth_api()
    if auth_err: return "Unauthorized", 401
    configs = global_config.get_all_symbol_configs()
    content = json.dumps(configs, indent=4, ensure_ascii=False)
    return Response(
        content,
        mimetype="application/json",
        headers={"Content-disposition": f"attachment; filename=crypto_configs_{datetime.now().strftime('%Y%m%d')}.json"}
    )

# --- Prompt 模板管理 ---

@config_bp.route('/api/prompts/list', methods=['GET'])
def list_prompts():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    try:
        if not os.path.exists(PROMPT_DIR): os.makedirs(PROMPT_DIR)
        files = [
            f for f in os.listdir(PROMPT_DIR)
            if f.endswith('.txt') and f not in BLOCKED_PROMPT_FILES
        ]
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@config_bp.route('/api/prompts/read', methods=['GET'])
def read_prompt():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    name = request.args.get('name')
    if not name or '..' in name: return jsonify({"success": False, "message": "无效文件名"}), 400
    if name in BLOCKED_PROMPT_FILES:
        return jsonify({"success": False, "message": "该模板已下线"}), 403
    try:
        path = os.path.join(PROMPT_DIR, name)
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({"success": True, "content": content})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@config_bp.route('/api/prompts/save', methods=['POST'])
def save_prompt():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    data = request.json
    name = data.get('name')
    content = data.get('content') or ''
    if not name or '..' in name or not name.endswith('.txt'):
        return jsonify({"success": False, "message": "格式非法"}), 400
    if name in BLOCKED_PROMPT_FILES:
        return jsonify({"success": False, "message": "该模板已下线"}), 403
    try:
        path = os.path.join(PROMPT_DIR, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

@config_bp.route('/api/prompts/delete', methods=['POST'])
def delete_prompt():
    auth_err = _require_chat_auth_api()
    if auth_err: return auth_err
    name = request.json.get('name')
    if name in BLOCKED_PROMPT_FILES:
        return jsonify({"success": False, "message": "该模板已下线"}), 403
    if not name or name in ['real.txt', 'strategy.txt']:
        return jsonify({"success": False, "message": "受保护文件"}), 400
    try:
        path = os.path.join(PROMPT_DIR, name)
        if os.path.exists(path): os.remove(path)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@config_bp.route('/api/config/dependencies/<config_id>', methods=['GET'])
def config_dependencies(config_id):
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err
    try:
        cfg = global_config.get_config_by_id(config_id)
        if not cfg:
            return jsonify({"success": False, "message": f"配置不存在: {config_id}"}), 404

        counts = get_config_dependency_counts(config_id)
        return jsonify({
            "success": True,
            "config_id": config_id,
            "counts": counts,
        })
    except Exception as e:
        logger.error(f"❌ 获取配置依赖失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


@config_bp.route('/api/config/delete/<config_id>', methods=['POST'])
def delete_config(config_id):
    auth_err = _require_chat_auth_api()
    if auth_err:
        return auth_err

    try:
        configs = global_config.get_all_symbol_configs()
        target = None
        remaining = []
        for cfg in configs:
            if cfg.get('config_id') == config_id:
                target = cfg
            else:
                remaining.append(cfg)

        if not target:
            return jsonify({"success": False, "message": f"配置不存在: {config_id}"}), 404

        dependencies_before = get_config_dependency_counts(config_id)
        cleanup_result = purge_config_all_data(config_id)

        _write_symbol_configs_to_env(remaining)
        global_config.reload_config()

        return jsonify({
            "success": True,
            "message": f"配置 {config_id} 已删除（已清理关联历史数据）",
            "removed_config": {
                "config_id": target.get('config_id'),
                "symbol": target.get('symbol'),
                "mode": target.get('mode'),
            },
            "dependencies_before": dependencies_before,
            "cleanup": cleanup_result,
        })
    except Exception as e:
        logger.error(f"❌ 删除配置失败: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
