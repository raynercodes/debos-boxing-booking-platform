import os
from fastapi import FastAPI
from mangum import Mangum

from src.api.routes import health, bookings, leads, auth

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")
ROOT_PATH = "" if ENVIRONMENT == "prod" else f"/{ENVIRONMENT}"

app = FastAPI(
    title="Debo's Boxing and Fitness — Booking API",
    description="""
## Debo's Boxing and Fitness — Booking & Automation Platform

Serverless booking and lead capture system built on AWS Lambda, DynamoDB, SES, and EventBridge.

---

## Architecture
- **Compute:** AWS Lambda + FastAPI + Mangum
- **Database:** DynamoDB — bookings table (GSI on session date) + leads table
- **Email:** SES — automated confirmations, gym owner notifications, 24hr reminders
- **Scheduling:** EventBridge — daily reminder job
- **Deployment:** GitHub Actions CI/CD
""",
    version="1.0.0",
    root_path=ROOT_PATH,
)

app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(bookings.router, prefix="/bookings", tags=["Bookings"])
app.include_router(leads.router, prefix="/leads", tags=["Leads"])

handler = Mangum(app)
