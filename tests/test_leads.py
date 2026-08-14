from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)


def test_create_lead_success(mock_aws_infra):
    payload = {
        "name": "Interested Person",
        "email": "interested@example.com",
        "phone": "4045559876",
        "interest_note": "Asked about evening kids classes",
    }
    response = client.post("/leads", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "interested@example.com"
    assert body["converted"] is False


def test_create_lead_without_optional_note(mock_aws_infra):
    payload = {
        "name": "No Note Person",
        "email": "nonote@example.com",
        "phone": "4045551111",
    }
    response = client.post("/leads", json=payload)
    assert response.status_code == 201
    assert response.json()["interest_note"] is None
