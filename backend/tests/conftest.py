import os
import sys
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ["SEED_SAMPLE_CASES"] = "false"
os.environ["REVIEWER_ACCOUNTS"] = "reviewer:pw-reviewer,lead:pw-lead"
os.environ["AUTH_SECRET"] = "test-secret"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["DATABASE_URL"] = "sqlite:///./data/test-bootstrap.db"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app import db as db_module  # noqa: E402
from app.evidence.parser import parse_evidence  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "app" / "samples"


@pytest.fixture()
def fresh_db(tmp_path):
    """SQLite by default; set TEST_DATABASE_URL to run the same suite against Postgres."""
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    db_module.configure(url)
    if os.environ.get("TEST_DATABASE_URL"):
        db_module.Base.metadata.drop_all(bind=db_module.engine)
    db_module.init_db()
    yield db_module.SessionLocal
    db_module.engine.dispose()


@pytest.fixture()
def client(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "reviewer", "password": "pw-reviewer"})
        c.headers["Authorization"] = f"Bearer {r.json()['token']}"
        yield c


def load_sample(key: str) -> dict:
    import json
    meta = json.loads((SAMPLES / key / "meta.json").read_text())
    out = {}
    for kind, fn in meta["files"]:
        out[kind] = parse_evidence(kind, (SAMPLES / key / fn).read_text()).data
    return out


def calc_sample(key: str) -> dict:
    from app.engine.calculator import calculate
    s = load_sample(key)
    return calculate(s["invoice"], s["contract"], s["usage"]["events"],
                     s["payments"]["entries"] if "payments" in s else [])
