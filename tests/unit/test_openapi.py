import json
import os
from pathlib import Path

from ledger_api.main import app

CHECKED_IN = Path(__file__).parents[2] / "docs" / "openapi.json"


def test_checked_in_openapi_matches_the_code() -> None:
    current = json.dumps(app.openapi(), indent=2) + "\n"
    if os.environ.get("UPDATE_OPENAPI"):
        CHECKED_IN.write_text(current)

    assert CHECKED_IN.read_text() == current, (
        "docs/openapi.json is stale; regenerate with: "
        "UPDATE_OPENAPI=1 uv run pytest tests/unit/test_openapi.py"
    )
