"""Initial data: users, catalog and vouchers (relative to the app clock)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.checkout import clock
from app.checkout.db import Base, Product, User, Voucher, engine
from app.checkout.security import hash_password

USERS = [
    ("alice", "alice123", "customer"),
    ("bob", "bob123", "customer"),
    ("admin", "admin123", "admin"),
]

PRODUCTS: list[tuple[str, str]] = [
    ("Wireless Mouse", "19.99"),
    ("USB-C Charging Cable 1m", "9.49"),
    ("Mechanical Keyboard", "89.00"),
    ("27-inch Monitor", "249.99"),
    ("Laptop Stand", "34.50"),
    ("Noise-Cancelling Headphones", "179.95"),
    ("Webcam 1080p", "49.99"),
    ("Desk Lamp", "27.25"),
    ("Ergonomic Chair Cushion", "39.99"),
    ("HDMI Cable 2m", "12.99"),
    ("Portable SSD 1TB", "119.00"),
    ("Microphone Arm", "44.75"),
    ("Mouse Pad XL", "15.99"),
    ("Bluetooth Speaker", "59.99"),
    ("Phone Holder", "8.99"),
    ("Cable Organizer Kit", "13.33"),
    ("Smart Plug", "22.49"),
    ("Wi-Fi Range Extender", "54.99"),
    ("Power Bank 20000mAh", "45.00"),
    ("Screen Cleaning Kit", "7.77"),
    ("Docking Station", "159.99"),
    ("Graphics Tablet", "99.95"),
    ("Laptop Sleeve 14-inch", "24.99"),
    ("USB Hub 7-port", "29.99"),
    ("Ring Light", "36.40"),
    ("Wireless Charger", "31.15"),
    ("Monitor Arm", "84.99"),
    ("Keyboard Wrist Rest", "18.25"),
    ("Smart Bulb", "14.99"),
    ("Surge Protector", "26.66"),
]


def seed(session: Session) -> None:
    for username, password, role in USERS:
        session.add(User(username=username, password_hash=hash_password(password), role=role))
    for index, (name, price) in enumerate(PRODUCTS, start=1):
        session.add(Product(sku=f"SKU-{index:03d}", name=name, unit_price=Decimal(price)))

    now = clock.now()
    window_start = now - timedelta(days=1)
    window_end = now + timedelta(days=365)
    vouchers = [
        ("SAVE15", "percent", "15", window_start, window_end, None),
        ("FLAT5", "fixed", "5.00", window_start, window_end, None),
        ("ONCE10", "percent", "10", window_start, window_end, 1),
        ("OLD20", "percent", "20", now - timedelta(days=30), now - timedelta(days=1), None),
    ]
    for code, kind, value, start, end, max_uses in vouchers:
        session.add(
            Voucher(
                code=code,
                kind=kind,
                value=Decimal(value),
                valid_from=start,
                valid_until=end,
                max_uses=max_uses,
                uses=0,
                created_at=now,
            )
        )
    session.commit()


def init_db(*, wipe: bool) -> None:
    if wipe:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.get(Product, "SKU-001") is None:
            seed(session)
