from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.checkout import clock
from app.checkout.bugs import bug_enabled
from app.checkout.db import DeviceEvent, DeviceState, get_db
from app.checkout.errors import not_found
from app.checkout.schemas import DeviceBatch, DeviceBatchResult, DeviceStateOut
from app.checkout.security import CurrentUser

router = APIRouter(tags=["devices"])

Db = Annotated[Session, Depends(get_db)]
DeviceId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]


@router.post("/devices/{device_id}/events", response_model=DeviceBatchResult)
async def ingest_events(
    device_id: DeviceId, body: DeviceBatch, _: CurrentUser, db: Db
) -> DeviceBatchResult:
    seen = set(
        db.scalars(
            select(DeviceEvent.seq).where(
                DeviceEvent.device_id == device_id,
                DeviceEvent.seq.in_({e.seq for e in body.events}),
            )
        ).all()
    )
    state = db.get(DeviceState, device_id)
    accepted = duplicates = 0
    received_at = clock.now()
    for event in body.events:
        if event.seq in seen:
            duplicates += 1
            continue
        seen.add(event.seq)
        accepted += 1
        db.add(
            DeviceEvent(
                device_id=device_id,
                seq=event.seq,
                ts=event.ts,
                type=event.type,
                plate=event.plate,
                camera=event.camera,
                received_at=received_at,
            )
        )
        if state is None:
            state = DeviceState(
                device_id=device_id,
                last_seq=event.seq,
                last_ts=event.ts,
                last_plate=event.plate,
                event_count=0,
            )
            db.add(state)
        elif bug_enabled("B06") or event.seq > state.last_seq:
            state.last_seq = event.seq
            state.last_ts = event.ts
            state.last_plate = event.plate
        state.event_count += 1
    db.commit()
    return DeviceBatchResult(accepted=accepted, duplicates=duplicates)


@router.get("/devices/{device_id}/state", response_model=DeviceStateOut)
async def device_state(device_id: DeviceId, _: CurrentUser, db: Db) -> DeviceStateOut:
    state = db.get(DeviceState, device_id)
    if state is None:
        raise not_found("Device")
    return DeviceStateOut(
        device_id=state.device_id,
        last_seq=state.last_seq,
        last_ts=state.last_ts,
        last_plate=state.last_plate,
        event_count=state.event_count,
    )
