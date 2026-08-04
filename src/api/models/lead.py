from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional

from src.api.models.booking import PHONE_PATTERN


class LeadRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=20)
    interest_note: Optional[str] = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank or whitespace only")
        return stripped

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = value.strip()
        if not PHONE_PATTERN.match(cleaned):
            raise ValueError("phone must be 7-15 digits, optionally prefixed with +")
        return cleaned


class LeadResponse(BaseModel):
    lead_id: str
    name: str
    email: EmailStr
    phone: str
    interest_note: Optional[str] = None
    created_at: str
    converted: bool
