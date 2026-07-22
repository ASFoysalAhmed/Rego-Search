from __future__ import annotations

import os

from waitress import serve

from app import app


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = _env_int("API_PORT", 5000)
    threads = _env_int("WAITRESS_THREADS", 8)

    # Run with a production WSGI server instead of Flask's dev server.
    serve(app, host=host, port=port, threads=max(2, threads))
