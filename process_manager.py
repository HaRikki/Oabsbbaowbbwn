"""Local process manager for user projects. Uses subprocess + psutil for real status/metrics."""
from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

import psutil

from app.config import USER_PROJECTS_DIR, LOGS_DIR


class ProcessManager:
    """Manages isolated user project processes on the host."""

    def __init__(self):
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._meta: dict[int, dict] = {}

    def project_dir(self, user_id: int, slug: str) -> Path:
        d = USER_PROJECTS_DIR / f"user{user_id}" / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_path(self, user_id: int, slug: str) -> Path:
        d = LOGS_DIR / f"user{user_id}" / slug
        d.mkdir(parents=True, exist_ok=True)
        return d / "app.log"

    async def start(
        self,
        project_id: int,
        user_id: int,
        slug: str,
        command: str,
        env: dict | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        if project_id in self._procs:
            proc = self._procs[project_id]
            if proc.returncode is None:
                return {"ok": False, "error": "Already running", "pid": proc.pid}

        work = cwd or self.project_dir(user_id, slug)
        log_file = self.log_path(user_id, slug)
        env_vars = os.environ.copy()
        if env:
            env_vars.update({str(k): str(v) for k, v in env.items()})

        log_f = open(log_file, "a", encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(work),
                env=env_vars,
                stdout=log_f,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            log_f.close()
            return {"ok": False, "error": str(e)}

        self._procs[project_id] = proc
        self._meta[project_id] = {
            "pid": proc.pid,
            "user_id": user_id,
            "slug": slug,
            "started_at": time.time(),
            "log_handle": log_f,
        }
        return {"ok": True, "pid": proc.pid, "status": "running"}

    async def stop(self, project_id: int) -> dict[str, Any]:
        proc = self._procs.get(project_id)
        meta = self._meta.get(project_id, {})
        if not proc:
            # try kill by stored pid
            pid = meta.get("pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            return {"ok": True, "status": "stopped"}

        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            lh = meta.get("log_handle")
            if lh:
                try:
                    lh.close()
                except Exception:
                    pass
            self._procs.pop(project_id, None)
            self._meta.pop(project_id, None)
        return {"ok": True, "status": "stopped"}

    async def restart(self, project_id: int, user_id: int, slug: str, command: str, env: dict | None = None) -> dict:
        await self.stop(project_id)
        await asyncio.sleep(1)
        return await self.start(project_id, user_id, slug, command, env)

    def status(self, project_id: int) -> dict[str, Any]:
        proc = self._procs.get(project_id)
        meta = self._meta.get(project_id, {})
        if proc and proc.returncode is None:
            return {
                "status": "running",
                "pid": proc.pid,
                "uptime_sec": int(time.time() - meta.get("started_at", time.time())),
            }
        # check if pid still alive externally
        pid = meta.get("pid")
        if pid:
            try:
                p = psutil.Process(pid)
                if p.is_running():
                    return {"status": "running", "pid": pid, "uptime_sec": int(time.time() - meta.get("started_at", time.time()))}
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return {"status": "stopped", "pid": None, "uptime_sec": 0}

    def metrics(self, project_id: int) -> dict[str, Any]:
        st = self.status(project_id)
        if st["status"] != "running" or not st.get("pid"):
            return {
                "cpu_percent": None,
                "ram_mb": None,
                "status": "stopped",
                "message": "Metric unavailable",
            }
        try:
            p = psutil.Process(st["pid"])
            with p.oneshot():
                cpu = p.cpu_percent(interval=0.1)
                mem = p.memory_info().rss / (1024 * 1024)
            return {
                "cpu_percent": round(cpu, 1),
                "ram_mb": round(mem, 1),
                "status": "running",
                "uptime_sec": st.get("uptime_sec", 0),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {"cpu_percent": None, "ram_mb": None, "status": "stopped", "message": "Metric unavailable"}

    def read_logs(self, user_id: int, slug: str, lines: int = 200) -> str:
        path = self.log_path(user_id, slug)
        if not path.exists():
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.readlines()
            return "".join(content[-lines:])
        except Exception:
            return ""


process_manager = ProcessManager()
