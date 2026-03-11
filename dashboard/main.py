"""
Japan 225 Bot — Dashboard API
FastAPI backend. Bind: 127.0.0.1:8080. Exposed via tunnel (ngrok / Cloudflare).
Auth: Bearer DASHBOARD_TOKEN header on every request except /api/health.
CORS: GitHub Pages origin only.
"""
import hmac
import os
import time
from collections import defaultdict
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
ALLOWED_ORIGIN  = "https://mostafaiq.github.io"

# Rate limiting: block IPs after too many failed auth attempts
_fail_counts: dict[str, list[float]] = defaultdict(list)  # ip -> [timestamps]
_RATE_WINDOW = 60       # seconds
_RATE_MAX_FAILS = 10    # max failures per window before blocking

app = FastAPI(title="Japan 225 Dashboard API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN, "http://localhost:3000"],  # localhost for local dev
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"],
)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # CORS preflight — no auth needed
    if request.method == "OPTIONS":
        return await call_next(request)
    # Health endpoint — no auth needed
    if request.url.path == "/api/health":
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"

    # Check if IP is rate-limited
    now = time.time()
    fails = _fail_counts[ip]
    # Prune old entries
    _fail_counts[ip] = [t for t in fails if now - t < _RATE_WINDOW]
    if len(_fail_counts[ip]) >= _RATE_MAX_FAILS:
        return JSONResponse({"detail": "Too many requests"}, status_code=429)

    # All other endpoints require Bearer token
    auth = request.headers.get("Authorization", "")
    if not DASHBOARD_TOKEN:
        return JSONResponse({"detail": "DASHBOARD_TOKEN not configured on server"}, status_code=500)
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], DASHBOARD_TOKEN):
        _fail_counts[ip].append(now)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    # Successful auth — clear any failure history for this IP
    _fail_counts.pop(ip, None)
    return await call_next(request)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

# ── Routers ──
from dashboard.routers import status, config, history, logs, chat, controls, stream

app.include_router(status.router)
app.include_router(config.router)
app.include_router(history.router)
app.include_router(logs.router)
app.include_router(chat.router)
app.include_router(controls.router)
app.include_router(stream.router)
