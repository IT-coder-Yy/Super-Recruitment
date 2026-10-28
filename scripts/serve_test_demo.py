"""仅用于浏览器验收：创建全新隔离目录，以显式 Mock 启动虚构 Demo。"""
import argparse
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path, help="必须为尚不存在的新目录")
parser.add_argument("--port", type=int, default=8012)
parser.add_argument("--resume", action="store_true", help="仅重启本脚本标记的虚构验收目录")
args = parser.parse_args()
root = args.directory.resolve()
marker = root/"synthetic-demo.marker"
if args.resume:
    if not marker.is_file() or marker.read_text() != "synthetic-only":
        parser.error("只能恢复本脚本创建的虚构验收目录")
    from dotenv import dotenv_values
    saved = dotenv_values(root/"runtime.env")
    if saved.get("LLM_PROVIDER") != "mock" or saved.get("SMTP_HOST") or saved.get("WECOM_WEBHOOK_URL"):
        parser.error("验收目录必须保持 Mock，禁止外呼")
else:
    root.mkdir(parents=True, exist_ok=False)
    marker.write_text("synthetic-only")
os.environ.update({
    "APP_DATA_DIR": str(root), "APP_RUNTIME_ENV": str(root/"runtime.env"),
    "DATABASE_URL": f"sqlite:///{(root/'demo.db').as_posix()}",
    "APP_SECRET_KEY": "synthetic-ui-test-secret", "LLM_PROVIDER": "mock", "LLM_BASE_URL": "", "LLM_API_KEY": "", "LLM_MODEL": "",
    "SMTP_HOST": "", "SMTP_PASSWORD": "", "WECOM_WEBHOOK_URL": "", "FOLLOWUP_ENABLED": "false",
    "APP_PUBLIC_BASE_URL": f"http://127.0.0.1:{args.port}",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import app, startup
from app.db import SessionLocal
from app.models import User
from app.services.auth import hash_password
from sqlalchemy import select
startup()
if not args.resume:
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.normalized_username == "demo-admin"))
        if user:
            user.password_hash = hash_password("demo123456")
        else:
            session.add(User(username="demo-admin", normalized_username="demo-admin", password_hash=hash_password("demo123456")))
        session.commit()
import uvicorn
uvicorn.run(app, host="127.0.0.1", port=args.port)
