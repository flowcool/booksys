"""Execution-local decoder ownership interface, independent of transport."""
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol


class DecoderContext(Protocol):
    path: Path
    def register(self, name: str) -> None: ...
    def prepare(self, command: list[str]) -> list[str]: ...


current_decoder: ContextVar[DecoderContext | None] = ContextVar('current_decoder', default=None)
