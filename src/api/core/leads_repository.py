from typing import Optional

from botocore.exceptions import ClientError, BotoCoreError

from src.api.core.database import DynamoDBService
from src.api.core.exceptions import ExternalServiceError
from src.api.core.logging_config import get_logger

logger = get_logger(__name__)


class LeadRepository:
    def __init__(self, db_service: DynamoDBService) -> None:
        self._db = db_service

    def create(self, item: dict) -> dict:
        try:
            self._db.leads_table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(lead_id)",
            )
            return item
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to write lead: %s", exc, exc_info=True)
            raise ExternalServiceError("Unable to save lead") from exc

    def get_by_id(self, lead_id: str) -> Optional[dict]:
        try:
            result = self._db.leads_table.get_item(Key={"lead_id": lead_id})
            return result.get("Item")
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to read lead %s: %s", lead_id, exc, exc_info=True)
            raise ExternalServiceError("Unable to retrieve lead") from exc
