from __future__ import annotations

import os

import uvicorn

from app.common.jsonlog import logging_config


def main() -> None:
    uvicorn.run(
        "app.provider.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8001")),
        log_config=logging_config(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
