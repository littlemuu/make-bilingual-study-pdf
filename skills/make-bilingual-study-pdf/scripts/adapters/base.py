from __future__ import annotations

from dataclasses import dataclass


class AdapterError(ValueError):
    """Raised when an adapter input cannot satisfy its frozen contract."""


@dataclass(frozen=True)
class AdapterSpec:
    id: str
    source_script: str | None = None
    import_script: str | None = None

    def script_for(self, operation: str) -> str:
        script = {
            "source": self.source_script,
            "import": self.import_script,
        }.get(operation)
        if script is None:
            raise AdapterError(
                f"adapter {self.id!r} does not support the {operation!r} operation"
            )
        return script
