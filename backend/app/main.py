from __future__ import annotations
import hmac
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from backend.app.db import init_db
from backend.app.routers import jobs, applications, outreach, runs, stats, platforms, settings, profile as profile_router, questions as questions_router
from backend.core import config

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="Job Autopilot", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def local_access(request: Request, call_next):
    from urllib.parse import urlparse
    host = request.url.hostname
    local = {"localhost", "127.0.0.1", "::1", "testserver"}
    if host not in local: return JSONResponse({"detail": "Local access only"}, status_code=403)
    peer = request.client.host if request.client else None
    if peer and peer not in local and peer != "testclient": return JSONResponse({"detail": "Local access only"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in local: return JSONResponse({"detail": "Cross-origin writes denied"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site": return JSONResponse({"detail": "Cross-site writes denied"}, status_code=403)
    return await call_next(request)

for router in (jobs, applications, outreach, runs, stats, platforms, settings, profile_router, questions_router):
    app.include_router(router.router, prefix="/api")

@app.get("/api/health")
def health(): return {"ok": True, "execution": "separate_worker"}

app.mount("/artifacts", StaticFiles(directory=str(config.ARTIFACTS)), name="artifacts")
app.mount("/screenshots", StaticFiles(directory=str(config.SCREENSHOTS)), name="screenshots")
dist = config.ROOT / "frontend" / "dist"

@app.get("/{path:path}")
def frontend(path: str):
    from fastapi import HTTPException
    if path.startswith(("api/", "artifacts/", "screenshots/", "data/")): raise HTTPException(404)
    requested = (dist / path).resolve()
    if dist.resolve() not in requested.parents and requested != dist.resolve(): raise HTTPException(404)
    if requested.is_file(): return FileResponse(requested)
    if dist.joinpath("index.html").exists(): return FileResponse(dist / "index.html")
    raise HTTPException(404, "Build the frontend or use the Vite development server")
