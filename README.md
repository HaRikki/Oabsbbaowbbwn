# Anajak Host

Universal hosting control panel for bots, websites, APIs, game servers and custom projects.

**Brand:** Anajak Host · Logo: **AJ**  
Original product — not affiliated with other hosting brands.

## Features

- User dashboard & multi-step create hosting
- Real process start / stop / restart (subprocess + psutil)
- File manager (sandboxed, ZIP slip protected)
- Logs & live metrics (real values only)
- Environment variables, domains field, backups (ZIP)
- Billing (ABA / Bakong / bank) — admin confirms payment
- **Admin free purchase** — instant paid, $0
- Support tickets, notifications, settings
- Admin panel: users, projects, orders, plans, games, system, broadcast, audit
- Auto-start & auto-restart with restart limits

## Requirements

- Python 3.11+
- Linux host recommended (for real process management)
- Render.com can host the **control panel** (Free tier sleeps; not ideal for 24/7 bots)

## Quick start

```bash
cd anajak-host
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open: http://localhost:8000

**Default admin**

- Email: `admin@anajakhost.local`
- Password: `admin123456`

Change password after first login.

## Deploy on Render

See `DEPLOY_RENDER.md` and `render.yaml`.

```text
Build:  pip install -r requirements.txt
Start:  uvicorn app:app --host 0.0.0.0 --port $PORT
```

Set `SECRET_KEY`, `DATABASE_URL` (PostgreSQL recommended), `APP_URL`, `DEBUG=false`.

## Project structure

```text
anajak-host/
├── app.py              # FastAPI entry
├── app/                # config, models, auth, process_manager
├── templates/          # HTML pages
├── static/             # CSS / JS
├── user_projects/      # isolated per-user project files
├── logs/
├── requirements.txt
├── render.yaml
└── .env.example
```

## Security notes

- JWT cookie auth, bcrypt passwords, ownership checks
- Path traversal & ZIP slip protection on file manager
- Secrets never logged
- Payment never marked paid from frontend alone (except admin free path)

## License

Use and modify for your own hosting business.
