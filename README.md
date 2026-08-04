# Debo's Boxing and Fitness — Booking Platform

Serverless booking and lead capture system built for Debo's Boxing and Fitness, eliminating
manual booking and automating client communication end to end.

Built and maintained by **RaynerCodes Cloud Solutions**.

## Architecture

```
Framer Frontend
      ↓
API Gateway (Regional)
      ↓
Lambda (FastAPI + Mangum)
      ↓
DynamoDB (bookings + leads)
      ↓
SES (confirmations, notifications) + EventBridge (24hr reminders)
```

## Tech Stack

- **Backend:** Python, FastAPI, AWS Lambda, Mangum
- **Database:** DynamoDB (on-demand)
- **Email:** AWS SES
- **Scheduling:** AWS EventBridge
- **IaC:** AWS SAM / CloudFormation
- **CI/CD:** GitHub Actions

## Local Development

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## Deployment

All deployments run under the client's own AWS account, via the `debos-boxing` named
CLI profile:

```bash
cp -r src ./package/
sam build --template infrastructure/template.yaml --profile debos-boxing
sam deploy --profile debos-boxing
```

## Status

🚧 Under active development — MVP build in progress.

- [x] Repo scaffolding + CI pipeline
- [ ] DynamoDB tables (bookings, leads)
- [ ] SES setup
- [ ] EventBridge reminder job
- [ ] Framer frontend integration

## License

Client engagement built and maintained by RaynerCodes Cloud Solutions for
Debo's Boxing and Fitness. Not licensed for reuse, redistribution, or
deployment by parties outside this engagement.
