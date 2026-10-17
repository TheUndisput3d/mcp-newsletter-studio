"""Local, explicit credential persistence. Secrets never enter YAML, UI responses or logs."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from dotenv import dotenv_values


def update_secrets(root: Path, changes: dict[str, str | None]) -> None:
    configured = os.getenv("NEWSLETTER_SECRETS_FILE")
    target = Path(configured) if configured else root / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)
    values = dict(dotenv_values(target)) if target.exists() else {}
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            if any(c in value for c in "\r\n"):
                raise ValueError("Credential values must not contain newlines")
            values[key] = value
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".secrets-", suffix=".tmp")
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# Local secrets. Do not commit or share this file.\n")
            for key, value in sorted(values.items()):
                # JSON quotes work for dotenv double-quoted string values.
                f.write(key + "=" + json.dumps(value or "", ensure_ascii=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
