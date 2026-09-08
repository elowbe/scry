"""Single-owner bearer/cookie authentication for the browser host."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path


class AuthManager:
    cookie_name = "game_stream_session"

    def __init__(self, token_file: Path):
        self.token_file = token_file
        self.token = self._load_or_create()

    def _load_or_create(self) -> str:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
        if len(token) < 32:
            raise ValueError(f"Access token in {self.token_file} is too short")
        return token

    def valid(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, self.token)

