from __future__ import annotations

import os

import uvicorn

from app.checkout.observability import logging_config


def main() -> None:
    uvicorn.run(
        "app.checkout.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_config=logging_config(),
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
