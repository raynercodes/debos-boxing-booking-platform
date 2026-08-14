# Debo's Boxing and Fitness — Booking Platform

Full-stack serverless booking and payment platform for Debo's Boxing and Fitness, a real
local gym in Baxley/Alma, Georgia. Live in production — handles real customer bookings,
real Stripe payments, and real transactional email end to end.

Built and maintained by **RaynerCodes Cloud Solutions**.

**Live:** https://debosboxingandfitness.com

## Architecture

```
Framer Frontend (React code components)
      ↓
API Gateway (Regional, custom domain)
      ↓
Lambda (FastAPI + Mangum)
      ↓                    ↓                    ↓
DynamoDB              Stripe              SES / Resend
(bookings, leads,     (checkout,          (confirmations,
 security)            webhooks,           reminders,
                       refunds)            admin alerts)
      ↓
EventBridge (daily reminder job, 24hrs before session)
```

Three fully isolated environments — dev, staging, prod — each with its own DynamoDB tables,
Lambda functions, secrets, and (for staging/prod) custom domain. Nothing is shared between
environments.

## Tech Stack

- **Backend:** Python 3.12, FastAPI, AWS Lambda, Mangum
- **Database:** DynamoDB (on-demand), with GSIs for date-range and IP-based queries
- **Payments:** Stripe Checkout — webhook-driven confirmation, automated conditional refunds
- **Email:** AWS SES (primary) with a toggleable Resend bridge (see below)
- **Scheduling:** AWS EventBridge (daily reminder job)
- **Security:** Encrypted secrets via Secrets Manager (6 separate KMS keys), progressive
  brute-force lockout, IP-based rate limiting and blocklisting, least-privilege IAM scoped
  per resource
- **IaC:** AWS SAM / CloudFormation
- **CI/CD:** GitHub Actions, OIDC federation (no stored AWS credentials)
- **Frontend:** Framer, with custom React code components (BookingForm, AdminPanel,
  BookSessionDropdown)
- **Testing:** Pytest, moto — 145 automated tests, run on every push

## Features

- Real-time availability, booking, and Stripe checkout for 5 session types
- Automatic slot-claim mechanism preventing double-booking on concurrent requests
  (atomic DynamoDB conditional writes, not a naive check-then-write)
- Checkout-recovery email if a customer starts checkout but doesn't finish
- Admin panel — login, bookings list, follow-up leads view, cancel with automatic
  conditional refund, blocked-IP management, failed-login-attempt visibility
- No-show vs. emergency cancellation distinction, with real session-duration awareness
  (Adult classes are 2 hours, everything else is 1)
- All session date/time logic is Eastern-timezone-aware (not naive UTC), correctly
  handling daylight saving transitions
- Daily reminder emails, 24 hours before each confirmed session

## Environments

| Environment | Trigger | URL |
|---|---|---|
| **dev** | Auto-deploys on every push to `dev` branch | Raw API Gateway URL (no custom domain) |
| **staging** | Manual, via `workflow_dispatch` | `https://staging-api.debosboxingandfitness.com` |
| **prod** | Manual, via `workflow_dispatch`, requires approval | `https://api.debosboxingandfitness.com` |

## Branch Strategy

- **`dev`** — all active development happens here. Auto-deploys to the dev environment on
  every push.
- **`main`** — production-ready code only. Merge `dev` into `main` before promoting to
  staging or prod, so `main` always reflects what's actually been deployed there.

```bash
git checkout main
git pull origin main
git merge dev --no-edit
git push origin main
```

## Local Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Deployment

All deployment runs through GitHub Actions — never manually via the AWS CLI.

**dev** deploys automatically on every push to `dev`. **staging** and **prod** are manual,
triggered from the Actions tab or CLI, and require deploying from `main`:

```bash
gh workflow run deploy.yml --ref main -f target_environment=staging
gh workflow run deploy.yml --ref main -f target_environment=prod
```

Both staging and prod deploys require manual approval through a GitHub Environment gate
before the change actually applies.

## Email Provider Toggle (temporary)

`EMAIL_PROVIDER` (template parameter, defaults to `ses`) can be set to `resend` as a
short-term bridge if AWS SES production access is ever pending or unavailable. When active,
this routes all outbound email through Resend instead, with zero changes needed to any
calling code — see `src/api/core/email_service.py` for the dispatch logic.

`resend_service.py` and `email_service.py` are both fully isolated, standalone files —
if this bridge is no longer needed, cleanup is: delete both files, and revert the single
import line in `webhooks.py`, `bookings.py`, `lockout.py`, and `reminder_handler.py` back
to importing `get_ses_service` directly from `ses_service.py`.

```bash
gh workflow run deploy.yml --ref main -f target_environment=prod -f email_provider=resend
```

## Status

✅ **Live in production.** Stripe and AWS SES are both fully approved and active. Core
booking, payment, admin, and notification flows are built, tested, and deployed.

## License

Client engagement built and maintained by RaynerCodes Cloud Solutions for
Debo's Boxing and Fitness. Not licensed for reuse, redistribution, or
deployment by parties outside this engagement.
