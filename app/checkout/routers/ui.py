from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.checkout.bugs import bug_enabled

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

_env = Environment(
    loader=FileSystemLoader(WEB_DIR / "templates"),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)

router = APIRouter(tags=["ui"])

_script: str | None = None


def _render_script() -> str:
    global _script
    if _script is None:
        _script = _env.get_template("app.js").render(bug=bug_enabled)
    return _script


@router.get("/", response_class=FileResponse, summary="Web UI")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "templates" / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/app.js", summary="Web UI script")
async def app_script() -> Response:
    return Response(
        _render_script(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )
