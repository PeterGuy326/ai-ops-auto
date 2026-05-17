"""初始化数据库。"""
from ai_ops.core.db import init_db

if __name__ == "__main__":
    init_db()
    print("OK: database initialized")
