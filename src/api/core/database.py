"""
DynamoDB access layer.

DynamoDBService is a singleton — get_db_service() always returns the same
instance within a single Lambda execution context, so table connections and
resource clients persist across warm invocations exactly like the old
module-level globals did. The difference is the state now lives inside an
object with a clear interface, instead of scattered module-level variables.
"""

import os
from typing import Optional

import boto3


class DynamoDBService:
    """Lazily-initialized DynamoDB table access, cached for the life of this
    Lambda execution context (L1 cache — dies with the container, persists
    across warm invocations)."""

    def __init__(self) -> None:
        self._resource = None
        self._bookings_table = None
        self._leads_table = None
        self._security_table = None

    @property
    def resource(self):
        if self._resource is None:
            self._resource = boto3.resource("dynamodb")
        return self._resource

    @property
    def bookings_table(self):
        if self._bookings_table is None:
            table_name = os.environ["BOOKINGS_TABLE_NAME"]
            self._bookings_table = self.resource.Table(table_name)
        return self._bookings_table

    @property
    def leads_table(self):
        if self._leads_table is None:
            table_name = os.environ["LEADS_TABLE_NAME"]
            self._leads_table = self.resource.Table(table_name)
        return self._leads_table

    @property
    def security_table(self):
        """Small dedicated table — brute force lockout tracking only. Not a
        general cache table like fintech's, because this project doesn't need
        L2 caching at gym scale. Single-purpose and named accordingly avoids
        scope creep into a cache layer nothing here needs yet."""
        if self._security_table is None:
            table_name = os.environ["SECURITY_TABLE_NAME"]
            self._security_table = self.resource.Table(table_name)
        return self._security_table


_db_service: Optional[DynamoDBService] = None


def get_db_service() -> DynamoDBService:
    """Module-level singleton accessor. Only one DynamoDBService instance
    exists per Lambda execution context — this function just hands back
    the same one every time it's called within that context."""
    global _db_service
    if _db_service is None:
        _db_service = DynamoDBService()
    return _db_service
