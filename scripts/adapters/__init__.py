from __future__ import annotations

from adapters.base import AdapterSpec


_ADAPTERS = {
    spec.id: spec
    for spec in (
        AdapterSpec(id="native-text-pdf", source_script="extract_pdf.py"),
        AdapterSpec(id="mineru-import", import_script="import_mineru.py"),
    )
}


def registered_adapter_ids() -> frozenset[str]:
    return frozenset(_ADAPTERS)


def get_adapter(adapter_id: str) -> AdapterSpec:
    try:
        return _ADAPTERS[adapter_id]
    except KeyError as exc:
        raise ValueError(f"unsupported input adapter: {adapter_id}") from exc
