"""Run ASGI apps on real loopback ports for integration and end-to-end tests."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(app, port: int) -> Iterator[None]:
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield
    finally:
        srv.should_exit = True
        thread.join(timeout=10)
