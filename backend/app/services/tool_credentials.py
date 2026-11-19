"""只在服务端解密个人工具凭据；主密钥独立于登录与缓存密钥，不进入运行时配置导出。"""
import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import db
from app.services.personal_tool_specs import canonical


def _master_key():
    value = os.getenv("TOOL_CREDENTIAL_MASTER_KEY", "")
    filename = os.getenv("TOOL_CREDENTIAL_MASTER_KEY_FILE", "")
    try:
        if filename:
            with Path(filename).open("r") as source:
                value = source.read(256).strip()
        key = base64.b64decode(value, validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except Exception:
        raise ValueError("工具凭据主密钥未配置或无效，请由管理员通过部署 secret 配置 32 字节 Base64 密钥") from None


def available():
    try:
        _master_key()
        return True
    except ValueError:
        return False


def encrypt(owner, tool_id, version, value):
    if not isinstance(value, str) or not 8 <= len(value.encode()) <= 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("凭据长度必须为 8–8192 字节且不能含控制字符")
    nonce = secrets.token_bytes(12)
    aad = canonical(["personal-tool-credential-v1", owner, tool_id, version]).encode()
    return base64.b64encode(nonce + AESGCM(_master_key()).encrypt(nonce, value.encode(), aad)).decode()


def load(owner, tool_id, version, credential_id):
    row = db.query_one("SELECT encrypted_value FROM personal_tool_credentials WHERE id=? AND owner_id=? AND tool_id=? AND version=?",
                       (credential_id, owner, tool_id, version))
    if not row:
        raise ValueError("未配置本版本的个人凭据")
    try:
        value = base64.b64decode(row["encrypted_value"], validate=True)
        aad = canonical(["personal-tool-credential-v1", owner, tool_id, version]).encode()
        return AESGCM(_master_key()).decrypt(value[:12], value[12:], aad).decode()
    except Exception:
        raise ValueError("凭据无法解密，请重新配置本版本凭据") from None
