import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from sqlalchemy import Engine

from ledger_api.api.database import get_engine
from ledger_api.main import app


@pytest.fixture
def server_url(db: Engine) -> Iterator[str]:
    """A real uvicorn server, so requests run concurrently in its thread pool."""
    app.dependency_overrides[get_engine] = lambda: db
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    while not server.started:
        time.sleep(0.01)
    host, port = sock.getsockname()
    yield f"http://{host}:{port}"
    server.should_exit = True
    thread.join()
    app.dependency_overrides.clear()
