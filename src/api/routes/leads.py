import uuid
from datetime import datetime, timezone
from fastapi import APIRouter
from src.api.models.lead import LeadRequest, LeadResponse
from src.api.core.database import get_db_service
from src.api.core.leads_repository import LeadRepository
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.post(
    "",
    response_model=LeadResponse,
    status_code=201,
    summary="Capture Lead",
    description="Capture someone interested but not ready to book yet.",
)
async def create_lead(request: LeadRequest):
    item = {
        "lead_id": str(uuid.uuid4()),
        "name": request.name,
        "email": request.email,
        "phone": request.phone,
        "interest_note": request.interest_note,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "converted": False,
    }

    repo = LeadRepository(get_db_service())
    repo.create(item)

    logger.info("Lead captured: %s", request.email)
    return LeadResponse(**item)
