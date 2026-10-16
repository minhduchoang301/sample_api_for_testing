from __future__ import annotations

from fastapi import APIRouter

from app.checkout import clock
from app.checkout.payments_state import clear_in_flight
from app.checkout.schemas import ClockOut, ClockSet
from app.checkout.security import AdminUser
from app.checkout.seed import init_db

router = APIRouter(prefix="/__test", tags=["test hooks"])


def _clock_out() -> ClockOut:
    return ClockOut(now=clock.isoformat(clock.now()), fixed=clock.is_fixed())


@router.post("/reset")
async def reset(_: AdminUser) -> dict[str, str]:
    clear_in_flight()
    init_db(wipe=True)
    return {"status": "reset"}


@router.get("/clock", response_model=ClockOut)
async def get_clock(_: AdminUser) -> ClockOut:
    return _clock_out()


@router.post("/clock", response_model=ClockOut)
async def set_clock(body: ClockSet, _: AdminUser) -> ClockOut:
    clock.set_fixed(clock.parse_instant(body.now.isoformat()))
    return _clock_out()


@router.delete("/clock", response_model=ClockOut)
async def clear_clock(_: AdminUser) -> ClockOut:
    clock.clear()
    return _clock_out()
