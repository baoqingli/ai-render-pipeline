# app/models/pipeline.py
from enum import StrEnum

from pydantic import BaseModel


class PipelineStage(StrEnum):
    created = "created"
    ingested = "ingested"
    converted = "converted"
    inspected = "inspected"
    parsed = "parsed"
    white_modelled = "white_modelled"
    styled = "styled"
    planned = "planned"
    rendering = "rendering"
    qa = "qa"
    finalized = "finalized"
    failed = "failed"


class NodeError(BaseModel):
    node: str
    code: str
    message: str


class FallbackEvent(BaseModel):
    stage: str
    detail: str


class QaAction(BaseModel):
    view_id: str
    variant_id: str
    action: str   # pass / flag / missing
    reason: str
