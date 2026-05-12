import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock

import jwt


JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("FLASK_SECRET_KEY") or os.getenv("ADMIN_PASSWORD", "dev-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "8"))

# 防爆破：5分钟内失败5次，锁定10分钟
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300   # 5分钟滑动窗口
_LOCKOUT_SECONDS = 600  # 锁定10分钟

# {ip: {"attempts": deque([timestamp, ...]), "locked_until": float}}
_login_attempts: dict[str, dict] = {}
_login_lock = Lock()


def _get_attempt_record(ip: str) -> dict:
    if ip not in _login_attempts:
        _login_attempts[ip] = {"attempts": deque(), "locked_until": 0.0}
    return _login_attempts[ip]


def check_login_allowed(ip: str) -> tuple[bool, str]:
    """检查该IP是否允许尝试登录。返回 (allowed, reason)"""
    now = time.time()
    with _login_lock:
        record = _get_attempt_record(ip)
        if now < record["locked_until"]:
            remaining = int(record["locked_until"] - now)
            return False, f"登录尝试过多，请在 {remaining} 秒后重试"
        # 清理窗口外的记录
        while record["attempts"] and now - record["attempts"][0] > _WINDOW_SECONDS:
            record["attempts"].popleft()
        return True, ""


def record_login_failure(ip: str) -> None:
    """记录登录失败，若达到阈值则锁定"""
    now = time.time()
    with _login_lock:
        record = _get_attempt_record(ip)
        # 清理过期记录
        while record["attempts"] and now - record["attempts"][0] > _WINDOW_SECONDS:
            record["attempts"].popleft()
        record["attempts"].append(now)
        if len(record["attempts"]) >= _MAX_ATTEMPTS:
            record["locked_until"] = now + _LOCKOUT_SECONDS
            record["attempts"].clear()


def reset_login_attempts(ip: str) -> None:
    """登录成功后清除该IP的失败记录"""
    with _login_lock:
        if ip in _login_attempts:
            _login_attempts[ip]["attempts"].clear()
            _login_attempts[ip]["locked_until"] = 0.0


def get_expected_password() -> str:
    return os.getenv("CHAT_PASSWORD") or os.getenv("ADMIN_PASSWORD") or ""


def authenticate_password(password: str) -> bool:
    expected = get_expected_password()
    return bool(expected and password == expected)


def create_access_token(subject: str = "admin") -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRE_HOURS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
