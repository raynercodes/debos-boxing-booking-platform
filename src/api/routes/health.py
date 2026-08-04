from fastapi import APIRouter
from datetime import datetime, timezone

router = APIRouter()


@router.get("/health", summary="Health Check")
async def health_check():
    """Basic liveness check — confirms the Lambda is running and responsive."""
    return {
        "status": "healthy",
        "service": "debos-boxing-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
