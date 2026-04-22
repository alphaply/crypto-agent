import os
from datetime import datetime, timedelta, timezone

import jwt


JWT_SECRET = os.getenv("JWT_SECRET") or os.getenv("FLASK_SECRET_KEY") or os.getenv("ADMIN_PASSWORD", "dev-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "8"))


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
