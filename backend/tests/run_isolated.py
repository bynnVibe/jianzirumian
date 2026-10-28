"""正式离线回归入口：不加载真实 .env，不连接业务数据库或外网。"""
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / ".dev-logs" / "production-assessment"
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="ab-", dir=OUT))
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HOME=str(WORK / "hf"))
sys.path.insert(0, str(ROOT / "backend"))


def blocked(*args, **kwargs):
    raise RuntimeError("隔离回归禁止真实网络连接")


socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
with patch("dotenv.load_dotenv", return_value=False), patch("os.makedirs"), patch.dict(os.environ, {}, clear=True):
    from app import config

config.BASE_DIR = WORK
config.DATA_DIR = WORK / "data"
for attr, suffix in (("UPLOAD_DIR", "uploads"), ("KNOWLEDGE_DIR", "knowledge"),
                     ("WIKI_DIR", "wiki"), ("VECTOR_STORE_PATH", "vector_store/faiss_index")):
    path = config.DATA_DIR / suffix
    path.mkdir(parents=True, exist_ok=True)
    setattr(config.settings, attr, str(path))
config.settings.FILE_CACHE_ENABLED = False
config.settings.WIKI_COMPILE_ENABLED = False
from app.core import db, log_setup

db.DB_PATH = WORK / "app.db"
db._conn = None
log_setup.setup_logging = lambda *a, **k: None
original_connect = sqlite3.connect


def isolated_connect(database, *args, **kwargs):
    if str(database) != ":memory:" and not Path(database).resolve().is_relative_to(OUT):
        raise RuntimeError("拒绝访问隔离目录以外的 SQLite")
    return original_connect(database, *args, **kwargs)


sqlite3.connect = isolated_connect
import pytest

args = sys.argv[1:] or [str(ROOT / "backend/tests")]
args += ["-v", "--tb=short", "-p", "no:cacheprovider", "--basetemp", str(WORK / "pytest"),
         f"--junitxml={WORK / 'results.xml'}"]
print(f"隔离目录及测试结果: {WORK}", flush=True)
raise SystemExit(pytest.main(args))
