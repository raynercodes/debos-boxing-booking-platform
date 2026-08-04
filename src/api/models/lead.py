from pydantic import BaseModel, EmailStr, Field
from typing import Optional


class LeadRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=7, max_length=20)
    interest_note: Optional[str] = Field(None, max_length=500)


class LeadResponse(BaseModel):
    lead_id: str
    name: str
    email: EmailStr
    phone: str
    interest_note: Optional[str] = None
    created_at: str
    converted: bool
