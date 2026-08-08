import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mangum import Mangum

from src.api.routes import health, bookings, leads, auth, webhooks
from src.api.core.exceptions import (
    AppError,
    InvalidCredentialsError,
    LockedOutError,
    InvalidTokenError,
    ExternalServiceError,
    BookingNotFoundError,
    BookingAlreadyCancelledError,
    SlotProcessingError,
    SlotTakenError,
    TooManyProcessingBookingsError,
    WebhookSignatureError,
)
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
ROOT_PATH = "" if ENVIRONMENT == "prod" else f"/{ENVIRONMENT}"

# /docs isn't the real testing surface for this project — Framer is the
# actual frontend, and /docs is closer to a staging/dev convenience for us
# than a demo interface for anyone else. Keeping the description short on
# purpose instead of the heavy walkthrough-style docs fintech has, since
# nobody but us is expected to open this page.
app = FastAPI(
    title="Debo's Boxing and Fitness — Booking Platform",
    description="Booking and lead automation backend for Debo's Boxing and Fitness. "
                "Internal/staging use — the real frontend is the Framer site.",
    version="1.0.0",
    root_path=ROOT_PATH,
    # Disabled deliberately, after a real production issue: FastAPI/Starlette's
    # default trailing-slash auto-redirect (307) is invisible in our own test
    # suite (TestClient follows redirects silently), but real external callers
    # often don't — most critically, Stripe's webhook delivery and some fetch()
    # configurations don't reliably follow redirects on POST. With this off,
    # a path either matches exactly or returns a clean 404 — no silent
    # redirect that could make a real booking/lead/webhook call quietly fail.
    redirect_slashes=False,
)

# CORS — required because Framer's site runs on a completely different
# domain than this API. Without this, the browser blocks the booking
# form's request entirely before it ever reaches us — invisible via curl
# or /docs, since neither is subject to browser CORS enforcement, only
# real cross-origin requests from an actual browser are.
#
# allow_origins="*" is deliberately broad for now — dev environment,
# Framer's preview URL isn't final yet, and none of our public endpoints
# (bookings, leads) rely on cookies (admin auth uses an explicit Bearer
# token, not a browser-managed cookie), so allow_credentials=False is
# both correct and required — the CORS spec forbids combining a wildcard
# origin with credentials=True. TIGHTEN THIS before staging/prod: once
# the real custom domain exists, replace "*" with an explicit allowlist
# of exactly debosboxingandfitness.com and www.debosboxingandfitness.com.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Global exception handlers ---------------------------------------------
# Translate domain-level exceptions (raised from service classes in src/api/core/)
# into HTTP responses in ONE place, instead of scattering try/except HTTPException
# across every route. Routes stay focused on request/response shape; error
# translation lives here.

def _format_retry_after(seconds: int) -> str:
    """Renders a lockout duration as minutes for short windows or hours for
    long ones — a 24-hour lockout showing '1440 minute(s)' is technically
    correct but unreadable and unhelpful to whoever's staring at that error
    message. Anything under 60 minutes shows minutes; 60 and up shows hours,
    rounded up so a 61-minute wait says '2 hours' rather than '1 hours'
    (which would understate how long is actually left)."""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute(s)"
    hours = (minutes + 59) // 60  # ceiling division, not floor
    return f"{hours} hour(s)"


@app.exception_handler(LockedOutError)
async def locked_out_handler(request: Request, exc: LockedOutError):
    wait_str = _format_retry_after(exc.retry_after_seconds)
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many failed login attempts. Try again in {wait_str}."},
    )


@app.exception_handler(InvalidCredentialsError)
async def invalid_credentials_handler(request: Request, exc: InvalidCredentialsError):
    return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})


@app.exception_handler(InvalidTokenError)
async def invalid_token_handler(request: Request, exc: InvalidTokenError):
    return JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(ExternalServiceError)
async def external_service_error_handler(request: Request, exc: ExternalServiceError):
    """The underlying AWS error (with all its specific detail) was already
    logged at the point it was caught, inside the service class that hit it.
    This handler's only job is to return a clean, generic response — it
    deliberately does NOT include str(exc) in the response body, unlike the
    other handlers, because ExternalServiceError messages are written for
    CloudWatch readers (us), not API clients."""
    logger.error("External service error reached the top-level handler: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our end. Please try again shortly."},
    )


@app.exception_handler(BookingNotFoundError)
async def booking_not_found_handler(request: Request, exc: BookingNotFoundError):
    """Registered BEFORE the generic AppError handler below, but order
    doesn't actually matter to FastAPI/Starlette — handler lookup walks the
    exception's MRO to find the most specific registered type regardless of
    registration order. Listed here in logical proximity to its sibling
    error handlers rather than for any ordering reason."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(BookingAlreadyCancelledError)
async def booking_already_cancelled_handler(request: Request, exc: BookingAlreadyCancelledError):
    """409 Conflict — the request is well-formed and the resource exists,
    but the current state (already cancelled) conflicts with the requested
    action. That's a more precise status code than a generic 400, and lets
    the Framer frontend distinguish "this genuinely failed" from "nothing
    to do, it's already in that state" if it ever wants to."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SlotProcessingError)
async def slot_processing_handler(request: Request, exc: SlotProcessingError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SlotTakenError)
async def slot_taken_handler(request: Request, exc: SlotTakenError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(TooManyProcessingBookingsError)
async def too_many_processing_handler(request: Request, exc: TooManyProcessingBookingsError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(WebhookSignatureError)
async def webhook_signature_handler(request: Request, exc: WebhookSignatureError):
    """400, not 401 — see WebhookSignatureError's own docstring for why
    Stripe specifically expects a 4xx here to stop retrying a permanently
    invalid event rather than hammering the endpoint."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(AppError)
async def generic_app_error_handler(request: Request, exc: AppError):
    """Catch-all for any AppError subclass that doesn't have a specific
    handler above — ensures a new exception type added later still returns
    a sane response instead of leaking a 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# health.py declares its route at "/" internally and gets its "/health" path
# from the prefix here — same consistent pattern as auth/bookings/leads below,
# where each router only knows its own relative paths and main.py assembles
# the real URL structure. Previously health.py hardcoded "/health" itself
# while also having no prefix here, which worked but was the odd one out.
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(bookings.router, prefix="/bookings", tags=["Bookings"])
app.include_router(leads.router, prefix="/leads", tags=["Leads"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])

handler = Mangum(app)
