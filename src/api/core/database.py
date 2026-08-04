import os
import boto3

# Outside handler — cached in Lambda execution context (L1), persists across warm invocations
# NOTE: if tests ever mock DynamoDB (moto), reset ALL THREE globals together in a fixture —
# resetting only the table singletons while _dynamodb stays stale caused a real bug on the
# fintech project (module-level resource wasn't reset alongside the table objects).
_dynamodb = None
_bookings_table = None
_leads_table = None
_security_table = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def get_bookings_table():
    global _bookings_table
    if _bookings_table is None:
        table_name = os.environ["BOOKINGS_TABLE_NAME"]
        _bookings_table = _get_dynamodb().Table(table_name)
    return _bookings_table


def get_leads_table():
    global _leads_table
    if _leads_table is None:
        table_name = os.environ["LEADS_TABLE_NAME"]
        _leads_table = _get_dynamodb().Table(table_name)
    return _leads_table


def get_security_table():
    """Small dedicated table — brute force lockout tracking only.
    Not a general cache table like fintech's, because this project doesn't
    need L2 caching at gym scale. Keeping it single-purpose and named
    accordingly avoids scope creep into a cache layer nothing here needs yet."""
    global _security_table
    if _security_table is None:
        table_name = os.environ["SECURITY_TABLE_NAME"]
        _security_table = _get_dynamodb().Table(table_name)
    return _security_table
