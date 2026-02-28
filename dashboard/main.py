"""
Japan 225 Bot — Dashboard API
FastAPI backend. Bind: 127.0.0.1:8080. Exposed via tunnel (ngrok / Cloudflare).
Auth: Bearer DASHBOARD_TOKEN header on every request except /api/health.
CORS: GitHub Pages origin only.
"""
import os
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
ALLOWED_ORIGIN  = "https://mostafaiq.github.io"

app = FastAPI(title="Japan 225 Dashboard API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN, "http://localhost:3000"],  # localhost for local dev
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # CORS preflight — no auth needed
    if request.method == "OPTIONS":
        return await call_next(request)
    # Health endpoint — no auth needed
    if request.url.path == "/api/health":
        return await call_next(request)
    # All other endpoints require Bearer token
    auth = request.headers.get("Authorization", "")
    if not DASHBOARD_TOKEN:
        return JSONResponse({"detail": "DASHBOARD_TOKEN not configured on server"}, status_code=500)
    if not auth.startswith("Bearer ") or auth[7:] != DASHBOARD_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

# ── Routers ──
from dashboard.routers import status, config, history, logs, chat, controls

app.include_router(status.router)
app.include_router(config.router)
app.include_router(history.router)
app.include_router(logs.router)
app.include_router(chat.router)
app.include_router(controls.router)
