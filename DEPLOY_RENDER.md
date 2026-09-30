# Deploy Anajak Host on Render.com

## Important limits

| Item | Reality |
|------|---------|
| Free Web Service | Sleeps after idle — first request ~30s |
| Free disk | Ephemeral — `user_projects` / logs may reset on redeploy |
| 24/7 bots / games | Prefer a **VPS** (Hetzner, Contabo, DigitalOcean) |
| Control panel only | Render Free is OK for testing the UI + DB |

## Steps

### 1. Push code to GitHub

Upload folder with correct structure:

```text
repo/
  app.py
  requirements.txt
  render.yaml
  README.md
  DEPLOY_RENDER.md
  app/
  templates/
  static/
```

Do **not** upload `.env`, `database.db`, `user_projects/`, `logs/`.

### 2. Create PostgreSQL (optional but recommended)

Render Dashboard → New → PostgreSQL (Free) → copy **Internal Database URL**.

### 3. Create Web Service

- New → Web Service → connect GitHub repo
- Runtime: Python 3
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Plan: Free

### 4. Environment variables

| Key | Value |
|-----|--------|
| `SECRET_KEY` | long random string |
| `DATABASE_URL` | Internal Postgres URL (or leave empty for SQLite — not persistent on free) |
| `APP_URL` | `https://YOUR-SERVICE.onrender.com` |
| `DEBUG` | `false` |
| `DOCKER_ENABLED` | `false` |
| `ADMIN_EMAIL` | your admin email |
| `ADMIN_PASSWORD` | strong password |

### 5. Deploy

Wait for build. Open the URL. Login as admin and change password.

## Blueprint

If repo has `render.yaml`, use **New → Blueprint** for auto Web + DB.

## Root Directory

If files are inside a subfolder `anajak-host/`, set:

**Settings → Root Directory** = `anajak-host`

## After deploy

1. Login admin  
2. Create plans if needed  
3. Test create project  
4. For real bot/game hosting 24/7 → move panel to a VPS
