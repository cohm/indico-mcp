import os

import pytest


@pytest.fixture(autouse=True)
def clean_indico_env(monkeypatch):
    """Isolate tests from INDICO_* variables in the shell or a local .env file.

    server.py calls load_dotenv() at import time, so a developer's real
    configuration would otherwise leak into Config() during tests.
    """
    for key in list(os.environ):
        if key.startswith("INDICO_"):
            monkeypatch.delenv(key)
