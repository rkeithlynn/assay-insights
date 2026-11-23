"""Load balancer health check. No token, no database."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

HEALTH_PATH = "/health"


class Health(BaseModel):
    status: str


@router.get(HEALTH_PATH, response_model=Health, include_in_schema=False)
def health() -> Health:
    return Health(status="ok")
