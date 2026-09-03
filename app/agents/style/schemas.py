# app/agents/style/schemas.py
from pydantic import BaseModel

from app.models.rendering import StyleParams


class StyleAgentOutput(BaseModel):
    params: StyleParams
    fallbacks: list[str] = []
