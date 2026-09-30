"""
Anajak Host - Universal Hosting Control Panel
Entry point: uvicorn app:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Depends, HTTPException, Form, UploadFile, File, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import (
    BASE_DIR, SECRET_KEY, DEBUG, ADMIN_EMAIL, ADMIN_PASSWORD,
    USER_PROJECTS_DIR, BACKUPS_DIR, APP_URL,
)
from app.database import init_db, get_db, AsyncSessionLocal
from app.models import (
    User, HostingPlan, Project, Order, SupportTicket, TicketMessage,
    Notification, GameTemplate, AuditLog, Backup,
)
from app.auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, get_current_user_optional, require_admin,
)
from app.process_manager import process_manager

app = FastAPI(title="Anajak Host", docs_url="/api/docs" if DEBUG else None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ─── Helpers ───────────────────────────────────────────────

def utcnow():
    return datetime.now(timezone.utc)


async def audit(db: AsyncSession, user_id: int | None, action: str, detail: str = ""):
    db.add(AuditLog(user_id=user_id, action=action, detail=detail))
    await db.commit()


async def notify(db: AsyncSession, user_id: int, title: str, body: str):
    db.add(Notification(user_id=user_id, title=title, body=body))
    await db.commit()


def safe_slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())
    return s[:80] or "project"


async def seed_data():
    async with AsyncSessionLocal() as db:
        # Admin
        r = await db.execute(select(User).where(User.email == ADMIN_EMAIL))
        if not r.scalar_one_or_none():
            admin = User(
                email=ADMIN_EMAIL,
                username="admin",
                hashed_password=hash_password(ADMIN_PASSWORD),
                full_name="Administrator",
                role="admin",
            )
            db.add(admin)

        # Plans
        plans = [
            ("Basic", "basic", 512, 1.0, 5, 1, 0.50, "Starter resources"),
            ("Starter", "starter", 1024, 1.0, 10, 3, 1.50, "Good for bots"),
            ("Pro", "pro", 2048, 2.0, 25, 10, 4.00, "Websites & APIs"),
            ("Advanced", "advanced", 4096, 4.0, 50, 20, 8.00, "Heavy workloads"),
        ]
        for name, slug, ram, cpu, storage, max_h, price, desc in plans:
            r = await db.execute(select(HostingPlan).where(HostingPlan.slug == slug))
            if not r.scalar_one_or_none():
                db.add(HostingPlan(
                    name=name, slug=slug, ram_mb=ram, cpu=cpu,
                    storage_gb=storage, max_hostings=max_h,
                    price_monthly=price, description=desc,
                ))

        # Game templates
        games = [
            ("Minecraft Java", "minecraft", "itzg/minecraft-server", 25565, 2048, 2.0, 10),
            ("Custom Game", "custom-game", None, 25565, 1024, 1.0, 5),
        ]
        for name, slug, img, port, ram, cpu, storage in games:
            r = await db.execute(select(GameTemplate).where(GameTemplate.slug == slug))
            if not r.scalar_one_or_none():
                db.add(GameTemplate(
                    name=name, slug=slug, docker_image=img,
                    default_port=port, ram_mb=ram, cpu=cpu, storage_gb=storage,
                ))
        await db.commit()


@app.on_event("startup")
async def on_startup():
    await init_db()
    await seed_data()
    # Auto-start projects
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Project).where(Project.auto_start == True))
        for p in r.scalars().all():
            if p.start_command:
                await process_manager.start(
                    p.id, p.user_id, p.slug, p.start_command, p.env_vars or {}
                )
                p.status = "running"
        await db.commit()
    asyncio.create_task(_monitor_loop())


async def _monitor_loop():
    """Auto-restart crashed projects."""
    while True:
        await asyncio.sleep(30)
        try:
            async with AsyncSessionLocal() as db:
                r = await db.execute(
                    select(Project).where(Project.auto_restart == True, Project.status == "running")
                )
                for p in r.scalars().all():
                    st = process_manager.status(p.id)
                    if st["status"] != "running":
                        if p.restart_count < p.max_restarts and p.start_command:
                            p.restart_count += 1
                            await process_manager.start(
                                p.id, p.user_id, p.slug, p.start_command, p.env_vars or {}
                            )
                            p.status = "running"
                        else:
                            p.status = "error"
                            await notify(db, p.user_id, "Project stopped", f"{p.name} crashed or exceeded restart limit.")
                await db.commit()
        except Exception:
            pass


# ─── Page routes ───────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: Optional[User] = Depends(get_current_user_optional)):
    return templates.TemplateResponse("index.html", {"request": request, "user": user})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Project).where(Project.user_id == user.id).order_by(desc(Project.created_at)))
    projects = r.scalars().all()
    unread = await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id, Notification.is_read == False
        )
    )
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "projects": projects,
        "unread": unread.scalar() or 0,
    })


@app.get("/create", response_class=HTMLResponse)
async def create_page(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    plans = (await db.execute(select(HostingPlan).where(HostingPlan.is_active == True))).scalars().all()
    games = (await db.execute(select(GameTemplate).where(GameTemplate.is_active == True))).scalars().all()
    return templates.TemplateResponse("create.html", {
        "request": request, "user": user, "plans": plans, "games": games,
    })


@app.get("/project/{project_id}", response_class=HTMLResponse)
async def project_page(project_id: int, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    st = process_manager.status(p.id)
    metrics = process_manager.metrics(p.id)
    logs = process_manager.read_logs(p.user_id, p.slug)
    backups = (await db.execute(select(Backup).where(Backup.project_id == p.id).order_by(desc(Backup.created_at)))).scalars().all()
    return templates.TemplateResponse("project.html", {
        "request": request, "user": user, "project": p,
        "status": st, "metrics": metrics, "logs": logs, "backups": backups,
    })


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    plans = (await db.execute(select(HostingPlan).where(HostingPlan.is_active == True))).scalars().all()
    orders = (await db.execute(select(Order).where(Order.user_id == user.id).order_by(desc(Order.created_at)))).scalars().all()
    projects = (await db.execute(select(Project).where(Project.user_id == user.id))).scalars().all()
    return templates.TemplateResponse("billing.html", {
        "request": request, "user": user, "plans": plans, "orders": orders, "projects": projects,
    })


@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    tickets = (await db.execute(
        select(SupportTicket).where(SupportTicket.user_id == user.id).order_by(desc(SupportTicket.updated_at))
    )).scalars().all()
    return templates.TemplateResponse("support.html", {"request": request, "user": user, "tickets": tickets})


@app.get("/support/{ticket_id}", response_class=HTMLResponse)
async def support_detail(ticket_id: int, request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(SupportTicket, ticket_id, options=[selectinload(SupportTicket.messages)])
    if not t or (t.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    return templates.TemplateResponse("support_detail.html", {"request": request, "user": user, "ticket": t})


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    notes = (await db.execute(
        select(Notification).where(Notification.user_id == user.id).order_by(desc(Notification.created_at))
    )).scalars().all()
    return templates.TemplateResponse("notifications.html", {"request": request, "user": user, "notes": notes})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "msg": None})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    stats = {
        "users": (await db.execute(select(func.count()).select_from(User))).scalar() or 0,
        "projects": (await db.execute(select(func.count()).select_from(Project))).scalar() or 0,
        "running": (await db.execute(select(func.count()).select_from(Project).where(Project.status == "running"))).scalar() or 0,
        "pending_orders": (await db.execute(select(func.count()).select_from(Order).where(Order.status.in_(["pending", "awaiting_confirm"])))).scalar() or 0,
    }
    users = (await db.execute(select(User).order_by(desc(User.created_at)).limit(50))).scalars().all()
    projects = (await db.execute(select(Project).order_by(desc(Project.created_at)).limit(50))).scalars().all()
    orders = (await db.execute(select(Order).order_by(desc(Order.created_at)).limit(50))).scalars().all()
    plans = (await db.execute(select(HostingPlan))).scalars().all()
    games = (await db.execute(select(GameTemplate))).scalars().all()
    tickets = (await db.execute(select(SupportTicket).order_by(desc(SupportTicket.updated_at)).limit(30))).scalars().all()
    audits = (await db.execute(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(50))).scalars().all()
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "stats": stats,
        "users": users, "projects": projects, "orders": orders,
        "plans": plans, "games": games, "tickets": tickets, "audits": audits,
    })


# ─── Auth API ──────────────────────────────────────────────

@app.post("/api/auth/register")
async def api_register(
    email: str = Form(...), username: str = Form(...), password: str = Form(...),
    full_name: str = Form(""), db: AsyncSession = Depends(get_db),
):
    if len(password) < 6:
        raise HTTPException(400, "Password min 6 characters")
    exists = await db.execute(select(User).where((User.email == email) | (User.username == username)))
    if exists.scalar_one_or_none():
        raise HTTPException(400, "Email or username already exists")
    user = User(
        email=email.strip().lower(),
        username=username.strip(),
        hashed_password=hash_password(password),
        full_name=full_name or username,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token({"sub": str(user.id)})
    resp = JSONResponse({"ok": True, "token": token})
    resp.set_cookie("access_token", token, httponly=True, max_age=60 * 60 * 24 * 7, samesite="lax")
    return resp


@app.post("/api/auth/login")
async def api_login(
    email: str = Form(...), password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(select(User).where(User.email == email.strip().lower()))
    user = r.scalar_one_or_none()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(401, "Invalid email or password")
    if user.is_suspended:
        raise HTTPException(403, "Account suspended")
    token = create_access_token({"sub": str(user.id)})
    await audit(db, user.id, "login")
    resp = JSONResponse({"ok": True, "token": token, "role": user.role})
    resp.set_cookie("access_token", token, httponly=True, max_age=60 * 60 * 24 * 7, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("access_token")
    return resp


@app.post("/api/settings/password")
async def change_password(
    current: str = Form(...), new_password: str = Form(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    if not verify_password(current, user.hashed_password):
        raise HTTPException(400, "Current password wrong")
    if len(new_password) < 6:
        raise HTTPException(400, "New password min 6 characters")
    user.hashed_password = hash_password(new_password)
    await db.commit()
    return {"ok": True}


@app.post("/api/settings/profile")
async def update_profile(
    full_name: str = Form(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    user.full_name = full_name.strip()
    await db.commit()
    return {"ok": True}


# ─── Projects ──────────────────────────────────────────────

@app.post("/api/projects/create")
async def create_project(
    name: str = Form(...),
    project_type: str = Form(...),
    plan_id: int = Form(...),
    start_command: str = Form(""),
    port: int = Form(0),
    source_type: str = Form("upload"),
    source_url: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.get(HostingPlan, plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(400, "Invalid plan")
    slug = safe_slug(name)
    # unique slug per user
    existing = await db.execute(
        select(Project).where(Project.user_id == user.id, Project.slug == slug)
    )
    if existing.scalar_one_or_none():
        slug = f"{slug}-{int(utcnow().timestamp()) % 10000}"

    p = Project(
        user_id=user.id,
        name=name.strip(),
        slug=slug,
        project_type=project_type,
        plan_id=plan.id,
        ram_mb=plan.ram_mb,
        cpu=plan.cpu,
        storage_gb=plan.storage_gb,
        start_command=start_command.strip() or None,
        port=port or None,
        source_type=source_type,
        source_url=source_url.strip() or None,
        status="stopped",
        env_vars={},
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    process_manager.project_dir(user.id, p.slug)
    await audit(db, user.id, "create_project", f"{p.name} ({p.id})")
    return {"ok": True, "id": p.id}


@app.post("/api/projects/{project_id}/start")
async def start_project(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    if not p.start_command:
        raise HTTPException(400, "No start command configured")
    result = await process_manager.start(p.id, p.user_id, p.slug, p.start_command, p.env_vars or {})
    if result.get("ok"):
        p.status = "running"
        p.pid = result.get("pid")
        p.restart_count = 0
        await db.commit()
    return result


@app.post("/api/projects/{project_id}/stop")
async def stop_project(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    result = await process_manager.stop(p.id)
    p.status = "stopped"
    p.pid = None
    await db.commit()
    return result


@app.post("/api/projects/{project_id}/restart")
async def restart_project(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    if not p.start_command:
        raise HTTPException(400, "No start command")
    result = await process_manager.restart(p.id, p.user_id, p.slug, p.start_command, p.env_vars or {})
    if result.get("ok"):
        p.status = "running"
        p.pid = result.get("pid")
        await db.commit()
    return result


@app.get("/api/projects/{project_id}/logs")
async def get_logs(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    return {"logs": process_manager.read_logs(p.user_id, p.slug)}


@app.get("/api/projects/{project_id}/metrics")
async def get_metrics(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    return process_manager.metrics(p.id)


@app.post("/api/projects/{project_id}/env")
async def set_env(
    project_id: int,
    key: str = Form(...),
    value: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    env = dict(p.env_vars or {})
    env[key.strip()] = value
    p.env_vars = env
    await db.commit()
    return {"ok": True, "env": {k: ("***" if "token" in k.lower() or "key" in k.lower() or "secret" in k.lower() or "password" in k.lower() else v) for k, v in env.items()}}


@app.delete("/api/projects/{project_id}/env/{key}")
async def del_env(project_id: int, key: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    env = dict(p.env_vars or {})
    env.pop(key, None)
    p.env_vars = env
    await db.commit()
    return {"ok": True}


@app.post("/api/projects/{project_id}/settings")
async def project_settings(
    project_id: int,
    start_command: str = Form(""),
    port: int = Form(0),
    auto_start: bool = Form(False),
    auto_restart: bool = Form(True),
    domain: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    p.start_command = start_command.strip() or p.start_command
    p.port = port or p.port
    p.auto_start = auto_start
    p.auto_restart = auto_restart
    p.domain = domain.strip() or None
    await db.commit()
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    await process_manager.stop(p.id)
    # remove files
    d = process_manager.project_dir(p.user_id, p.slug)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    await db.delete(p)
    await db.commit()
    await audit(db, user.id, "delete_project", str(project_id))
    return {"ok": True}


# ─── File Manager (sandboxed) ──────────────────────────────

def _safe_path(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(400, "Path traversal denied")
    return target


@app.get("/api/projects/{project_id}/files")
async def list_files(project_id: int, path: str = "", user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or (p.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    base = process_manager.project_dir(p.user_id, p.slug)
    target = _safe_path(base, path)
    if not target.exists():
        return {"path": path, "items": []}
    items = []
    for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        items.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else 0,
            "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
        })
    return {"path": path, "items": items}


@app.post("/api/projects/{project_id}/files/upload")
async def upload_file(
    project_id: int,
    path: str = Form(""),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    base = process_manager.project_dir(p.user_id, p.slug)
    target_dir = _safe_path(base, path)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / Path(file.filename).name
    content = await file.read()
    # zip slip protection for zip uploads
    if dest.suffix.lower() == ".zip":
        dest.write_bytes(content)
        try:
            with zipfile.ZipFile(dest, "r") as zf:
                for info in zf.infolist():
                    # prevent zip slip
                    member = _safe_path(target_dir, info.filename)
                    if info.is_dir():
                        member.mkdir(parents=True, exist_ok=True)
                    else:
                        member.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(member, "wb") as out:
                            shutil.copyfileobj(src, out)
        except zipfile.BadZipFile:
            raise HTTPException(400, "Invalid ZIP")
    else:
        dest.write_bytes(content)
    return {"ok": True}


@app.post("/api/projects/{project_id}/files/mkdir")
async def mkdir_file(
    project_id: int, path: str = Form(""), name: str = Form(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    base = process_manager.project_dir(p.user_id, p.slug)
    target = _safe_path(base, f"{path}/{name}".strip("/"))
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@app.delete("/api/projects/{project_id}/files")
async def delete_file(
    project_id: int, path: str = "",
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    base = process_manager.project_dir(p.user_id, p.slug)
    target = _safe_path(base, path)
    if target == base:
        raise HTTPException(400, "Cannot delete project root")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    return {"ok": True}


@app.post("/api/projects/{project_id}/backup")
async def create_backup(project_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, project_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404)
    base = process_manager.project_dir(p.user_id, p.slug)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"backup_{p.slug}_{int(utcnow().timestamp())}.zip"
    out = BACKUPS_DIR / fname
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in base.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(base))
    b = Backup(project_id=p.id, filename=fname, size_bytes=out.stat().st_size)
    db.add(b)
    await db.commit()
    return {"ok": True, "filename": fname, "size": b.size_bytes}


# ─── Billing ───────────────────────────────────────────────

@app.post("/api/orders/create")
async def create_order(
    plan_id: int = Form(...),
    project_id: int = Form(0),
    payment_method: str = Form("aba"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.get(HostingPlan, plan_id)
    if not plan:
        raise HTTPException(400, "Invalid plan")

    # Admin gets free instant
    if user.role == "admin":
        order = Order(
            user_id=user.id, plan_id=plan.id,
            project_id=project_id or None,
            amount=0, status="paid", payment_method="free_admin",
            paid_at=utcnow(),
        )
        db.add(order)
        if project_id:
            p = await db.get(Project, project_id)
            if p and p.user_id == user.id:
                p.plan_id = plan.id
                p.ram_mb = plan.ram_mb
                p.cpu = plan.cpu
                p.storage_gb = plan.storage_gb
                p.expires_at = utcnow() + timedelta(days=30)
        await db.commit()
        await notify(db, user.id, "Order completed", f"Admin free plan: {plan.name}")
        return {"ok": True, "status": "paid", "amount": 0}

    order = Order(
        user_id=user.id, plan_id=plan.id,
        project_id=project_id or None,
        amount=plan.price_monthly,
        status="awaiting_confirm",
        payment_method=payment_method,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return {"ok": True, "status": "awaiting_confirm", "order_id": order.id, "amount": order.amount}


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order(order_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404)
    if o.status not in ("pending", "awaiting_confirm"):
        raise HTTPException(400, "Cannot cancel")
    o.status = "cancelled"
    await db.commit()
    return {"ok": True}


# ─── Support ───────────────────────────────────────────────

@app.post("/api/support/create")
async def create_ticket(
    subject: str = Form(...), message: str = Form(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    t = SupportTicket(user_id=user.id, subject=subject.strip())
    db.add(t)
    await db.flush()
    db.add(TicketMessage(ticket_id=t.id, user_id=user.id, message=message.strip(), is_admin=False))
    await db.commit()
    return {"ok": True, "id": t.id}


@app.post("/api/support/{ticket_id}/reply")
async def reply_ticket(
    ticket_id: int, message: str = Form(...),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    t = await db.get(SupportTicket, ticket_id)
    if not t or (t.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    is_admin = user.role == "admin"
    db.add(TicketMessage(ticket_id=t.id, user_id=user.id, message=message.strip(), is_admin=is_admin))
    t.status = "answered" if is_admin else "open"
    t.updated_at = utcnow()
    await db.commit()
    if is_admin:
        await notify(db, t.user_id, "Support reply", f"Ticket: {t.subject}")
    return {"ok": True}


@app.post("/api/support/{ticket_id}/close")
async def close_ticket(ticket_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(SupportTicket, ticket_id)
    if not t or (t.user_id != user.id and user.role != "admin"):
        raise HTTPException(404)
    t.status = "closed"
    await db.commit()
    return {"ok": True}


# ─── Notifications ─────────────────────────────────────────

@app.post("/api/notifications/read-all")
async def read_all_notifications(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Notification).where(Notification.user_id == user.id, Notification.is_read == False))
    for n in r.scalars().all():
        n.is_read = True
    await db.commit()
    return {"ok": True}


@app.post("/api/notifications/{nid}/read")
async def read_notification(nid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    n = await db.get(Notification, nid)
    if n and n.user_id == user.id:
        n.is_read = True
        await db.commit()
    return {"ok": True}


# ─── Admin API ─────────────────────────────────────────────

@app.post("/api/admin/orders/{order_id}/confirm")
async def admin_confirm_order(order_id: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if not o:
        raise HTTPException(404)
    o.status = "paid"
    o.paid_at = utcnow()
    if o.project_id and o.plan_id:
        p = await db.get(Project, o.project_id)
        plan = await db.get(HostingPlan, o.plan_id)
        if p and plan:
            p.plan_id = plan.id
            p.ram_mb = plan.ram_mb
            p.cpu = plan.cpu
            p.storage_gb = plan.storage_gb
            p.expires_at = utcnow() + timedelta(days=30)
    await db.commit()
    await notify(db, o.user_id, "Payment confirmed", f"Order #{o.id} paid. Plan activated.")
    await audit(db, user.id, "confirm_order", str(order_id))
    return {"ok": True}


@app.post("/api/admin/orders/{order_id}/reject")
async def admin_reject_order(order_id: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if not o:
        raise HTTPException(404)
    o.status = "failed"
    await db.commit()
    await notify(db, o.user_id, "Payment rejected", f"Order #{o.id} was rejected.")
    return {"ok": True}


@app.post("/api/admin/users/{uid}/suspend")
async def admin_suspend(uid: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, uid)
    if not u or u.role == "admin":
        raise HTTPException(400)
    u.is_suspended = True
    await db.commit()
    await audit(db, user.id, "suspend_user", str(uid))
    return {"ok": True}


@app.post("/api/admin/users/{uid}/activate")
async def admin_activate(uid: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404)
    u.is_suspended = False
    u.is_active = True
    await db.commit()
    return {"ok": True}


@app.post("/api/admin/plans")
async def admin_create_plan(
    name: str = Form(...), slug: str = Form(...), ram_mb: int = Form(512),
    cpu: float = Form(1.0), storage_gb: int = Form(5), max_hostings: int = Form(1),
    price_monthly: float = Form(1.0), description: str = Form(""),
    user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    db.add(HostingPlan(
        name=name, slug=slug, ram_mb=ram_mb, cpu=cpu, storage_gb=storage_gb,
        max_hostings=max_hostings, price_monthly=price_monthly, description=description or None,
    ))
    await db.commit()
    return {"ok": True}


@app.post("/api/admin/plans/{pid}/toggle")
async def admin_toggle_plan(pid: int, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    plan = await db.get(HostingPlan, pid)
    if not plan:
        raise HTTPException(404)
    plan.is_active = not plan.is_active
    await db.commit()
    return {"ok": True, "is_active": plan.is_active}


@app.post("/api/admin/games")
async def admin_create_game(
    name: str = Form(...), slug: str = Form(...), docker_image: str = Form(""),
    default_port: int = Form(25565), ram_mb: int = Form(1024),
    cpu: float = Form(1.0), storage_gb: int = Form(5),
    user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    db.add(GameTemplate(
        name=name, slug=slug, docker_image=docker_image or None,
        default_port=default_port, ram_mb=ram_mb, cpu=cpu, storage_gb=storage_gb,
    ))
    await db.commit()
    return {"ok": True}


@app.post("/api/admin/broadcast")
async def admin_broadcast(
    title: str = Form(...), body: str = Form(...),
    user: User = Depends(require_admin), db: AsyncSession = Depends(get_db),
):
    users = (await db.execute(select(User))).scalars().all()
    for u in users:
        db.add(Notification(user_id=u.id, title=title, body=body))
    await db.commit()
    await audit(db, user.id, "broadcast", title)
    return {"ok": True, "sent": len(users)}


@app.get("/api/admin/system")
async def admin_system(user: User = Depends(require_admin)):
    import psutil
    disk = psutil.disk_usage("/")
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "ram_percent": mem.percent,
        "ram_used_gb": round(mem.used / (1024**3), 2),
        "ram_total_gb": round(mem.total / (1024**3), 2),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "running_processes": len([k for k, v in process_manager._procs.items() if v.returncode is None]),
    }


# Health
@app.get("/health")
async def health():
    return {"status": "ok", "app": "Anajak Host"}


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=DEBUG)
