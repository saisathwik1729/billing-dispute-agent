"""Sample dispute scenarios bundled with the app, so reviewers can test without data."""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..logging_setup import app_log
from ..models import Case
from .cases import DomainError, add_evidence, create_case

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def list_samples() -> list[dict]:
    out = []
    for meta_path in sorted(SAMPLES_DIR.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        meta["followups"] = [{"index": i, "kind": k, "filename": f, "label": label}
                             for i, (k, f, label) in enumerate(meta.get("followups", []))]
        meta["files"] = [{"kind": k, "filename": f} for k, f in meta["files"]]
        out.append(meta)
    return out


def get_sample(key: str) -> dict:
    for s in list_samples():
        if s["key"] == key:
            return s
    raise DomainError(f"Unknown sample '{key}'", 404, "not_found")


def sample_file(key: str, filename: str, followup: bool = False) -> str:
    sample = get_sample(key)
    names = [f["filename"] for f in (sample["followups"] if followup else sample["files"])]
    if filename not in names:
        raise DomainError("File not found in sample", 404, "not_found")
    base = SAMPLES_DIR / key / ("followups" if followup else "")
    return (base / filename).read_text()


def create_from_sample(db: Session, key: str, actor: str) -> Case:
    sample = get_sample(key)
    dispute = sample_file(key, next(f["filename"] for f in sample["files"] if f["kind"] == "dispute"))
    case = create_case(db, sample["title"], sample["customer_name"], dispute.strip(), actor, sample_key=key)
    for f in sample["files"]:
        if f["kind"] == "dispute":
            continue
        add_evidence(db, case.id, f["kind"], f["filename"], sample_file(key, f["filename"]), actor)
    return case


def add_followup(db: Session, case: Case, index: int, actor: str):
    if not case.sample_key:
        raise DomainError("This case was not created from a sample", 422, "validation_error")
    sample = get_sample(case.sample_key)
    if index < 0 or index >= len(sample["followups"]):
        raise DomainError("Unknown follow-up evidence", 404, "not_found")
    f = sample["followups"][index]
    return add_evidence(db, case.id, f["kind"], f["filename"], sample_file(case.sample_key, f["filename"], True), actor)


def seed_if_empty(db: Session) -> int:
    if (db.scalar(select(func.count()).select_from(Case)) or 0) > 0:
        return 0
    n = 0
    for s in list_samples():
        create_from_sample(db, s["key"], "system")
        n += 1
    db.commit()
    app_log.info("seeded_sample_cases", extra={"count": n})
    return n
