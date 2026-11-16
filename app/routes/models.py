"""GET /v1/models — built from MODEL_MAP_JSON."""

from fastapi import APIRouter, Request

from app.state import get_state

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request):
    s = get_state(request).settings
    return {
        "object": "list",
        "data": [{"id": name, "object": "model", "created": 0, "owned_by": s.models_owned_by} for name in s.model_map],
    }
