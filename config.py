import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "anajak-dev-secret-change-in-production")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR}/database/database.db")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
DOCKER_ENABLED = os.getenv("DOCKER_ENABLED", "false").lower() == "true"
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@anajakhost.local")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")

USER_PROJECTS_DIR = BASE_DIR / "user_projects"
LOGS_DIR = BASE_DIR / "logs"
UPLOADS_DIR = BASE_DIR / "uploads"
BACKUPS_DIR = BASE_DIR / "backups"
DATA_DIR = BASE_DIR / "data"

for d in (USER_PROJECTS_DIR, LOGS_DIR, UPLOADS_DIR, BACKUPS_DIR, DATA_DIR, BASE_DIR / "database"):
    d.mkdir(parents=True, exist_ok=True)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
