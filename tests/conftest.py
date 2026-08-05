"""
Shared test fixtures.

CRITICAL: fintech hit a real, documented bug where a module-level DynamoDB
singleton wasn't reset between test runs — a warm client/table reference
from one test's moto mock context silently survived into the next test's
DIFFERENT mock context, causing confusing, hard-to-diagnose failures.

The fix is this autouse fixture: reset every singleton BEFORE and AFTER
every single test, not just one or the other. "Before" guards against a
previous test leaving stale state; "after" guards against this test's
state leaking into whatever runs next. Both directions matter — this is
exactly the class of bug worth preventing here rather than rediscovering.
"""

import json

import boto3
import pytest
from moto import mock_aws

import src.api.core.database as database_module
import src.api.core.security as security_module

TEST_ENV_VARS = {
    "ENVIRONMENT": "dev",
    "AWS_DEFAULT_REGION": "us-east-1",
    "BOOKINGS_TABLE_NAME": "debos-boxing-bookings-test",
    "LEADS_TABLE_NAME": "debos-boxing-leads-test",
    "SECURITY_TABLE_NAME": "debos-boxing-security-test",
    "JWT_SECRET_PATH": "/debos-boxing/test/jwt-secret",
    "ADMIN_CREDENTIALS_PATH": "/debos-boxing/test/admin-credentials",
    "PASSWORD_PEPPER_PATH": "/debos-boxing/test/password-pepper",
    "ADMIN_EMAIL": "test-admin@example-test.invalid",
}

# A known password used ONLY in tests, to exercise the real /auth/login
# route end-to-end (right password, wrong password, lockout escalation).
# Previously nothing did this — the admin_token fixture below minted a JWT
# directly, bypassing login entirely, which meant the login route itself
# had zero real test coverage.
TEST_ADMIN_PASSWORD = "TestAdminPassword123!"


@pytest.fixture(autouse=True)
def reset_singletons():
    database_module._db_service = None
    security_module._security_service = None
    yield
    database_module._db_service = None
    security_module._security_service = None


@pytest.fixture(autouse=True)
def aws_test_env(monkeypatch):
    for key, value in TEST_ENV_VARS.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def mock_aws_infra(aws_test_env):
    """Spins up mocked DynamoDB tables (matching infrastructure/template.yaml's
    real schema) and mocked Secrets Manager entries, scoped to a single test
    via moto's context manager. Anything using get_db_service()/
    get_security_service() during this fixture's lifetime talks to this
    mocked infra, not real AWS."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")

        ddb.create_table(
            TableName=TEST_ENV_VARS["BOOKINGS_TABLE_NAME"],
            KeySchema=[{"AttributeName": "booking_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "booking_id", "AttributeType": "S"},
                {"AttributeName": "session_date", "AttributeType": "S"},
                {"AttributeName": "session_time", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "session-date-index",
                "KeySchema": [
                    {"AttributeName": "session_date", "KeyType": "HASH"},
                    {"AttributeName": "session_time", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=TEST_ENV_VARS["LEADS_TABLE_NAME"],
            KeySchema=[{"AttributeName": "lead_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "lead_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        ddb.create_table(
            TableName=TEST_ENV_VARS["SECURITY_TABLE_NAME"],
            KeySchema=[{"AttributeName": "security_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "security_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(
            Name=TEST_ENV_VARS["JWT_SECRET_PATH"],
            SecretString='{"secret": "test-jwt-secret-not-a-real-value"}',
        )
        sm.create_secret(
            Name=TEST_ENV_VARS["PASSWORD_PEPPER_PATH"],
            SecretString='{"pepper": "test-pepper-not-a-real-value"}',
        )

        # Admin credentials secret needs a REAL computed hash, not a
        # placeholder — otherwise no test could ever exercise a genuinely
        # correct /auth/login. The hash must be computed AFTER the pepper
        # secret above exists (hash_password reads the pepper), and the
        # singleton must be reset first so the SecurityService instance
        # used here actually talks to the freshly-mocked Secrets Manager
        # rather than any stale prior instance.
        security_module._security_service = None
        security_service = security_module.get_security_service()
        admin_password_hash = security_service.hash_password(TEST_ADMIN_PASSWORD)
        sm.create_secret(
            Name=TEST_ENV_VARS["ADMIN_CREDENTIALS_PATH"],
            SecretString=json.dumps({"password_hash": admin_password_hash}),
        )
        # Reset again so actual test code gets a clean instance too, rather
        # than reusing internal state left over from computing the hash above.
        security_module._security_service = None

        yield ddb


@pytest.fixture
def admin_token(mock_aws_infra):
    """A real, validly-signed JWT — computed the same way the actual
    /auth/login route would, using the mocked JWT secret above. Lets tests
    exercise the real require_admin dependency instead of bypassing it."""
    return security_module.get_security_service().create_jwt()

