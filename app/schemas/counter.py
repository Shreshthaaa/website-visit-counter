from __future__ import annotations

from pydantic import BaseModel


class VisitCount(BaseModel):
    count: int
    served_via: str
