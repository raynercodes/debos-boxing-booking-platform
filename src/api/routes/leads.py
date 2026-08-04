import uuid
from datetime import datetime, timezone
from fastapi import APIRouter
from src.api.models.lead import LeadRequest, LeadResponse

router = APIRouter()


@router.post(
    "/",
    response_model=LeadResponse,
    status_code=201,
    summary="Capture Lead",
    description="""
Capture a lead — someone interested but not ready to book a session yet.

📋 **Fill in name, email, phone, and an optional interest note**

⏳ **Note:** DynamoDB write is not yet implemented — this currently returns a mock
response so the API contract is locked in before infrastructure is built.
""",
)
async def create_lead(request: LeadRequest):
    # TODO: write to debos-boxing-leads table once infrastructure/template.yaml is deployed
    return LeadResponse(
        lead_id=str(uuid.uuid4()),
        name=request.name,
        email=request.email,
        phone=request.phone,
        interest_note=request.interest_note,
        created_at=datetime.now(timezone.utc).isoformat(),
        converted=False,
    )
