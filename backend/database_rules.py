"""Versioned, task-scoped trading guidance shared by humans and agents."""
from __future__ import annotations
from backend import storage

from datetime import datetime
from uuid import uuid4
import hashlib
import json

import pytz


class RuleConflictError(ValueError):
    """The caller edited an outdated revision."""


class RuleLockedError(ValueError):
    """An agent tried to change a human-locked rule."""


def initialize_trading_rules_schema(cursor):
    """Structure is installed by the explicit PostgreSQL migration."""


def _rule(row):
    result = storage.record(row)
    result.pop('storage_id', None)
    result['enabled'] = bool(result['enabled'])
    result['locked'] = bool(result['locked'])
    return result


def list_trading_rules(config_id: str, *, enabled_only: bool = False) -> list[dict]:
    from backend.database import get_db_conn

    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trading_rules WHERE config_id = %s" +
            (" AND enabled = 1" if enabled_only else "") + " ORDER BY created_at, rule_id",
            (config_id,),
        ).fetchall()
    return [_rule(row) for row in rows]


def trading_rule_history(config_id: str, rule_id: str) -> list[dict]:
    from backend.database import get_db_conn

    with get_db_conn() as conn:
        if not conn.execute('SELECT 1 FROM trading_rules WHERE config_id = %s AND rule_id = %s', (config_id, rule_id)).fetchone():
            raise FileNotFoundError('Trading rule not found')
        rows = conn.execute(
            'SELECT * FROM trading_rule_revisions WHERE config_id = %s AND rule_id = %s ORDER BY revision DESC',
            (config_id, rule_id),
        ).fetchall()
    return [_rule(row) for row in rows]


def change_trading_rules(config_id: str, changes: list[dict], *, actor: str, operation_id: str | None = None) -> list[dict]:
    """Apply the whole batch atomically; revisions protect concurrent human/model edits."""
    from backend.database import get_db_conn

    if actor not in {'human', 'model'} or not config_id:
        raise ValueError('A valid task and actor are required')
    if not 1 <= len(changes) <= 20:
        raise ValueError('Provide 1 to 20 rule changes')
    now = datetime.now(pytz.timezone('Asia/Shanghai')).isoformat(timespec='microseconds')
    results = []
    fingerprint = hashlib.sha256(json.dumps([actor, changes], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with get_db_conn() as conn:
        storage.lock_scope(conn, 'trading-rules', config_id)
        try:
            if operation_id:
                receipt = conn.execute('SELECT request_hash,result FROM trading_rule_operations WHERE config_id = %s AND operation_id = %s',
                                       (config_id, operation_id)).fetchone()
                if receipt:
                    if receipt['request_hash'] != fingerprint:
                        raise RuleConflictError('Operation ID has already been used for a different rule change')
                    conn.rollback()
                    return json.loads(receipt['result'])
            for change in changes:
                action = change.get('action')
                if action not in {'add', 'update', 'disable'}:
                    raise ValueError('Unknown rule action')
                if action == 'add':
                    rule_id = str(uuid4())
                    row = dict(rule_id=rule_id, config_id=config_id, content='', enabled=True,
                               locked=actor == 'human', revision=0, created_by=actor, created_at=now)
                else:
                    rule_id = change.get('rule_id')
                    stored = conn.execute('SELECT * FROM trading_rules WHERE rule_id = %s AND config_id = %s',
                                          (rule_id, config_id)).fetchone()
                    if stored is None:
                        raise FileNotFoundError('Trading rule not found')
                    row = _rule(stored)
                    if actor == 'model' and row['locked']:
                        raise RuleLockedError('Human-locked trading rules cannot be changed by the model')
                    if change.get('expected_revision') != row['revision']:
                        raise RuleConflictError('Trading rule changed; reload the current revision before editing')
                if actor == 'model' and 'locked' in change:
                    raise RuleLockedError('Only humans can change rule locks')
                for key in ('content', 'enabled', 'locked'):
                    if key in change:
                        row[key] = change[key]
                row['content'] = str(row['content'] or '').strip()
                if not row['content'] or len(row['content']) > 1200:
                    raise ValueError('Rule content must contain 1 to 1200 characters')
                if action == 'disable':
                    row['enabled'] = False
                row.update(revision=row['revision'] + 1, updated_by=actor, updated_at=now)
                conn.execute('''INSERT INTO trading_rules
                    (rule_id,config_id,content,enabled,locked,revision,created_by,updated_by,created_at,updated_at)
                    VALUES (%(rule_id)s,%(config_id)s,%(content)s,%(enabled)s,%(locked)s,%(revision)s,%(created_by)s,%(updated_by)s,%(created_at)s,%(updated_at)s)
                    ON CONFLICT(rule_id) DO UPDATE SET content=excluded.content, enabled=excluded.enabled,
                    locked=excluded.locked, revision=excluded.revision, updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at''', {**row, 'enabled': int(row['enabled']), 'locked': int(row['locked'])})
                reason = str(change.get('reason') or '').strip()
                if len(reason) > 1000:
                    raise ValueError('Change reason must contain at most 1000 characters')
                conn.execute('''INSERT INTO trading_rule_revisions
                    (rule_id,config_id,revision,content,enabled,locked,actor,reason,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    (rule_id, config_id, row['revision'], row['content'], int(row['enabled']), int(row['locked']), actor, reason, now))
                results.append(row)
            active = conn.execute('SELECT COUNT(*) FROM trading_rules WHERE config_id = %s AND enabled = 1', (config_id,)).fetchone()[0]
            if active > 30:
                raise ValueError('A task can have at most 30 enabled trading rules')
            if operation_id:
                conn.execute('INSERT INTO trading_rule_operations(config_id,operation_id,request_hash,result) VALUES(%s,%s,%s,%s)',
                             (config_id, operation_id, fingerprint, json.dumps(results, ensure_ascii=False)))
            conn.commit()
        except storage.Error:
            raise
        except Exception:
            conn.rollback()
            raise
    return results


def format_trading_rules_context(config_id: str) -> str:
    rules = list_trading_rules(config_id, enabled_only=True)
    preface = ('规则是可持续修订的交易指引；账户与行情以实时事实为准，不能覆盖系统风控、'
               '工具权限或交易所约束。人工锁定规则不可由模型修改；修改其他规则时提交当前版本号。')
    lines = [f"[{rule['rule_id']} v{rule['revision']} {'人工锁定' if rule['locked'] else '可修订'}] {rule['content']}" for rule in rules]
    return preface + '\n' + ('\n'.join(lines) if lines else '(暂无启用的长期交易规则)')
