"""Append-only experiment records for reproducible research."""

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union


@dataclass(frozen=True)
class ExperimentRecord:
    run_id: str
    strategy_key: str
    data_sha256: str
    code_revision: str
    started_at: datetime
    config: Mapping[str, Any]
    metrics: Mapping[str, Any]
    status: str = "completed"
    run_purpose: str = "engineering_diagnostic"
    knowledge_cutoff: Optional[datetime] = None
    data_start: Optional[datetime] = None
    data_end: Optional[datetime] = None

    def __post_init__(self) -> None:
        for name in ("run_id", "strategy_key", "code_revision"):
            if not getattr(self, name).strip():
                raise ValueError("{} must not be empty".format(name))
        digest = self.data_sha256.lower().strip()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("data_sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "data_sha256", digest)
        allowed_statuses = {"planned", "running", "completed", "failed", "rejected"}
        if self.status not in allowed_statuses:
            raise ValueError("status is not supported")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at must include a timezone")
        object.__setattr__(self, "started_at", self.started_at.astimezone(timezone.utc))
        allowed_purposes = {
            "engineering_diagnostic",
            "retrospective_diagnostic",
            "forward_evidence",
        }
        if self.run_purpose not in allowed_purposes:
            raise ValueError("run_purpose is not supported")
        for name in ("knowledge_cutoff", "data_start", "data_end"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("{} must be a timezone-aware datetime".format(name))
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.data_start is not None and self.data_end is not None:
            if self.data_end < self.data_start:
                raise ValueError("data_end cannot be earlier than data_start")
        if self.run_purpose == "forward_evidence":
            if self.knowledge_cutoff is None or self.data_start is None or self.data_end is None:
                raise ValueError("forward_evidence requires cutoff and data interval timestamps")
            if self.data_start <= self.knowledge_cutoff:
                raise ValueError("forward evidence data must begin after the knowledge cutoff")
        json.dumps(dict(self.config), sort_keys=True, allow_nan=False)
        json.dumps(dict(self.metrics), sort_keys=True, allow_nan=False)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "run_id": self.run_id,
            "strategy_key": self.strategy_key,
            "data_sha256": self.data_sha256,
            "code_revision": self.code_revision,
            "started_at": self.started_at.astimezone(timezone.utc).isoformat(),
            "config": dict(self.config),
            "metrics": dict(self.metrics),
            "status": self.status,
            "run_purpose": self.run_purpose,
            "knowledge_cutoff": (
                self.knowledge_cutoff.isoformat() if self.knowledge_cutoff is not None else None
            ),
            "data_start": self.data_start.isoformat() if self.data_start is not None else None,
            "data_end": self.data_end.isoformat() if self.data_end is not None else None,
        }
        json.dumps(payload, sort_keys=True, allow_nan=False)
        return payload


class ExperimentLedger:
    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def append(self, record: ExperimentRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(self.path) + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                existing_ids = {item["run_id"] for item in self._read_file()}
                if record.run_id in existing_ids:
                    raise ValueError("run_id already exists: {}".format(record.run_id))
                encoded = json.dumps(record.as_dict(), sort_keys=True, allow_nan=False)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        lock_path = Path(str(self.path) + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_file()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _read_file(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line, parse_constant=_reject_json_constant)
                    if not isinstance(item, dict) or not isinstance(item.get("run_id"), str):
                        raise ValueError("ledger entries must be objects with a run_id")
                    records.append(item)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ValueError("invalid ledger JSON on line {}".format(line_number)) from exc
        return records


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant: {}".format(value))
