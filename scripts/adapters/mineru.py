from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import warnings
from collections import Counter
from html import unescape
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

import pymupdf as fitz
from PIL import Image, ImageDraw

from adapters.base import AdapterError, AdapterSpec
from common import (
    normalize_text,
    problem_ids,
    sha256_file,
    sha256_text,
    write_json,
    write_jsonl,
)
from html_table import validate_table_html
from visual_utils import make_contact_sheets


SPEC = AdapterSpec(
    id="mineru-import",
    import_script="import_mineru.py",
)

ADAPTER_EVIDENCE_FILENAME = "adapter-evidence.json"
MAX_JSON_BYTES = 100 * 1024 * 1024
MAX_CONTENT_ITEMS = 100_000
MAX_JSON_DEPTH = 128
MAX_MIDDLE_VALUES = 1_000_000
SUPPORTED_MAJOR = 3
VERIFIED_VERSION = "3.4.4"
SUPPORTED_BACKEND = "pipeline"
LARGE_RASTER_PAGE_AREA_RATIO = 0.5
RASTER_COVERAGE_METHOD = "pymupdf-rotated-image-bbox-union-v2"
CONTENT_TYPES = {
    "text",
    "equation",
    "image",
    "chart",
    "table",
    "code",
    "list",
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
}
AUXILIARY_TYPES = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
}
URL_RE = re.compile(r"https?://[^\s<>\]\[{}]+", re.I)
VERSION_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")


def _reject_constant(value: str) -> None:
    raise AdapterError(f"JSON contains a non-finite number: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"JSON contains duplicate object key: {key!r}")
        result[key] = value
    return result


def load_strict_json(path: Path) -> Any:
    if not path.is_file():
        raise AdapterError(f"missing required MinerU artifact: {path.name}")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise AdapterError(f"MinerU JSON exceeds {MAX_JSON_BYTES} bytes: {path.name}")
    payload = path.read_bytes()
    if payload.startswith(b"\xef\xbb\xbf"):
        raise AdapterError(f"MinerU JSON must be UTF-8 without BOM: {path.name}")
    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise AdapterError(f"MinerU JSON is not valid UTF-8: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise AdapterError(f"invalid MinerU JSON {path.name}: {exc}") from exc
    except RecursionError as exc:
        raise AdapterError(f"MinerU JSON nesting is too deep: {path.name}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"value cannot be canonicalized as JSON: {exc}") from exc


def canonical_json_sha256(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_content_bbox(value: Any, pointer: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise AdapterError(f"{pointer}/bbox must be four integers")
    x0, y0, x1, y1 = value
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        raise AdapterError(f"{pointer}/bbox is outside the MinerU 0-1000 coordinate space")
    return list(value)


def validate_raw_bbox(value: Any, page_size: list[float], pointer: str) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not _is_number(item) or not math.isfinite(float(item)) for item in value)
    ):
        raise AdapterError(f"{pointer} must be a finite four-number bbox")
    x0, y0, x1, y1 = (float(item) for item in value)
    width, height = page_size
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise AdapterError(f"{pointer} is outside its middle.json page_size")
    return [x0, y0, x1, y1]


def normalize_middle_bbox(bbox: list[float], page_size: list[float]) -> list[int]:
    width, height = page_size
    return [
        int(bbox[0] * 1000 / width),
        int(bbox[1] * 1000 / height),
        int(bbox[2] * 1000 / width),
        int(bbox[3] * 1000 / height),
    ]


def _json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_subpointer(base: str, value: str) -> str:
    """Append an unescaped slash-delimited field path to an RFC 6901 pointer."""
    return base + "".join(f"/{_json_pointer_token(part)}" for part in value.split("/"))


def _walk_middle(
    value: Any,
    *,
    pointer: str,
    page_size: list[float],
    blocks: list[dict[str, Any]],
    image_paths: set[str],
    depth: int = 0,
    state: list[int] | None = None,
) -> None:
    if depth > MAX_JSON_DEPTH:
        raise AdapterError(f"middle.json exceeds maximum nesting depth at {pointer}")
    if state is None:
        state = [0]
    state[0] += 1
    if state[0] > MAX_MIDDLE_VALUES:
        raise AdapterError(f"middle.json exceeds {MAX_MIDDLE_VALUES} values")
    if isinstance(value, dict):
        if "bbox" in value:
            raw_bbox = validate_raw_bbox(value["bbox"], page_size, f"{pointer}/bbox")
            blocks.append(
                {
                    "pointer": pointer,
                    "type": value.get("type"),
                    "raw_bbox": raw_bbox,
                    "normalized_bbox": normalize_middle_bbox(raw_bbox, page_size),
                }
            )
        image_path = value.get("image_path")
        if image_path is not None:
            if not isinstance(image_path, str) or not image_path.strip():
                raise AdapterError(f"{pointer}/image_path must be a nonempty string")
            image_paths.add(image_path)
        for key, item in value.items():
            _walk_middle(
                item,
                pointer=f"{pointer}/{_json_pointer_token(str(key))}",
                page_size=page_size,
                blocks=blocks,
                image_paths=image_paths,
                depth=depth + 1,
                state=state,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_middle(
                item,
                pointer=f"{pointer}/{index}",
                page_size=page_size,
                blocks=blocks,
                image_paths=image_paths,
                depth=depth + 1,
                state=state,
            )


def validate_middle(
    middle: Any, page_count: int
) -> tuple[str, str, list[dict[str, Any]], set[str]]:
    if not isinstance(middle, dict):
        raise AdapterError("middle.json must contain a JSON object")
    backend = middle.get("_backend")
    version = middle.get("_version_name")
    if backend != SUPPORTED_BACKEND:
        raise AdapterError(
            "V2.3 supports only MinerU stable 3.x pipeline output; "
            f"found backend {backend!r}"
        )
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise AdapterError("MinerU _version_name must be a stable semantic version")
    if int(VERSION_RE.fullmatch(version).group("major")) != SUPPORTED_MAJOR:
        raise AdapterError(
            f"unsupported MinerU major version {version!r}; V2.3 accepts stable 3.x only"
        )
    pdf_info = middle.get("pdf_info")
    if not isinstance(pdf_info, list) or len(pdf_info) != page_count:
        raise AdapterError("middle.json pdf_info length must equal the source PDF page count")

    pages: list[dict[str, Any]] = []
    image_paths: set[str] = set()
    for index, page in enumerate(pdf_info):
        pointer = f"/pdf_info/{index}"
        if not isinstance(page, dict):
            raise AdapterError(f"{pointer} must be an object")
        page_idx = page.get("page_idx")
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx != index:
            raise AdapterError(f"{pointer}/page_idx must equal its zero-based array index")
        raw_size = page.get("page_size")
        if (
            not isinstance(raw_size, list)
            or len(raw_size) != 2
            or any(
                not _is_number(item)
                or not math.isfinite(float(item))
                or float(item) <= 0
                for item in raw_size
            )
        ):
            raise AdapterError(f"{pointer}/page_size must contain two positive numbers")
        page_size = [float(raw_size[0]), float(raw_size[1])]
        for field in ("preproc_blocks", "para_blocks", "discarded_blocks"):
            if not isinstance(page.get(field), list):
                raise AdapterError(f"{pointer}/{field} must be an array")
        middle_blocks: list[dict[str, Any]] = []
        _walk_middle(
            page,
            pointer=pointer,
            page_size=page_size,
            blocks=middle_blocks,
            image_paths=image_paths,
        )
        pages.append(
            {
                "page_idx": index,
                "page_size": page_size,
                "blocks": middle_blocks,
            }
        )
    return backend, version, pages, image_paths


def _path_collision_key(value: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", value).casefold()


def resolve_asset(output_dir: Path, value: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AdapterError("MinerU asset path must be a nonempty string without NUL")
    if "\\" in value:
        raise AdapterError(f"MinerU asset path must use POSIX separators: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise AdapterError(f"absolute MinerU asset path is not allowed: {value!r}")
    if any(part in {"", ".", ".."} for part in posix.parts):
        raise AdapterError(f"unsafe MinerU asset path: {value!r}")
    root = output_dir.resolve()
    try:
        candidate = (root / Path(*posix.parts)).resolve(strict=True)
    except OSError as exc:
        raise AdapterError(f"MinerU asset does not exist: {value!r}") from exc
    if not candidate.is_relative_to(root):
        raise AdapterError(f"MinerU asset escapes its output directory: {value!r}")
    if not candidate.is_file():
        raise AdapterError(f"MinerU asset is not a regular file: {value!r}")
    return posix.as_posix(), candidate


def validate_assets(output_dir: Path, paths: Iterable[str]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    collision_keys: dict[str, str] = {}
    for value in sorted(set(paths), key=lambda item: item.encode("utf-8")):
        relative, path = resolve_asset(output_dir, value)
        key = _path_collision_key(relative)
        if key in collision_keys and collision_keys[key] != relative:
            raise AdapterError(
                f"MinerU asset paths collide across case/Unicode normalization: "
                f"{collision_keys[key]!r}, {relative!r}"
            )
        collision_keys[key] = relative
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    image.load()
                    image_format = image.format
                    width, height = image.size
                    mime = Image.MIME.get(image_format, "application/octet-stream")
        except Exception as exc:
            raise AdapterError(f"MinerU asset cannot be fully decoded: {relative}") from exc
        digest = sha256_file(path)
        assets.append(
            {
                "id": "asset-" + sha256((relative + "\0" + digest).encode("utf-8")).hexdigest()[:24],
                "relative_path": relative,
                "source_path": path,
                "sha256": digest,
                "size": path.stat().st_size,
                "mime": mime,
                "width": width,
                "height": height,
            }
        )
    return assets


def _require_text(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"{pointer} must be a nonempty string without NUL")
    return value


def _require_string_list(value: Any, pointer: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise AdapterError(f"{pointer} must be an array" + (" with entries" if nonempty else ""))
    result = []
    for index, item in enumerate(value):
        result.append(_require_text(item, f"{pointer}/{index}"))
    return result


def _node_id(
    *,
    source_sha256: str,
    backend: str,
    version: str,
    pointer: str,
    payload: str,
    page_idx: int,
    page_order: int,
) -> str:
    payload_sha256 = sha256_text(payload)
    seed = "\0".join(
        (
            "mineru-import/v1",
            source_sha256,
            backend,
            version,
            pointer,
            payload_sha256,
        )
    )
    digest = sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"mu-p{page_idx + 1:04d}-i{page_order:04d}-{digest}"


def _middle_type_compatible(content_type: str, middle_type: Any) -> bool:
    aliases = {
        "text": {"text", "title", "index", "list"},
        "equation": {"interline_equation"},
        "image": {"image", "image_body"},
        "chart": {"chart", "chart_body"},
        "table": {"table", "table_body"},
        "code": {"code", "algorithm"},
        "list": {"list", "index", "ref_text"},
        "header": {"header"},
        "footer": {"footer"},
        "page_number": {"page_number"},
        "aside_text": {"aside_text"},
        "page_footnote": {"page_footnote"},
    }
    return isinstance(middle_type, str) and middle_type in aliases.get(content_type, set())


def _middle_match(
    content_type: str, bbox: list[int], page: dict[str, Any]
) -> tuple[str | None, str]:
    exact = [
        item
        for item in page["blocks"]
        if item["normalized_bbox"] == bbox
        and _middle_type_compatible(content_type, item.get("type"))
    ]
    if len(exact) == 1:
        return exact[0]["pointer"], "exact"
    if len(exact) > 1:
        return None, "ambiguous"
    return None, "unmatched"


def _extract_urls(text: str) -> list[str]:
    return sorted({match.group(0).rstrip(".,;:)") for match in URL_RE.finditer(text)})


def normalize_content(
    content: Any,
    *,
    source_sha256: str,
    backend: str,
    version: str,
    page_count: int,
    middle_pages: list[dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    if not isinstance(content, list):
        raise AdapterError("content_list.json must contain a flat JSON array")
    if len(content) > MAX_CONTENT_ITEMS:
        raise AdapterError(f"content_list.json exceeds {MAX_CONTENT_ITEMS} items")

    blocks: list[dict[str, Any]] = []
    visual_requests: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    asset_paths: set[str] = set()
    seen_node_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    previous_page = -1
    page_orders: Counter[int] = Counter()
    heading_seen = False
    auxiliary = profile.get("semantics", {}).get("auxiliary_dispositions", {})

    def add_block(
        *,
        item: dict[str, Any],
        item_index: int,
        page_idx: int,
        page_order: int,
        bbox: list[int],
        suffix: str,
        pointer: str,
        source: str,
        kind: str,
        translatable: bool,
        middle_pointer: str | None,
        middle_match: str,
        caption_parent: str | None = None,
        adapter_role: str | None = None,
    ) -> str:
        block_id = _node_id(
            source_sha256=source_sha256,
            backend=backend,
            version=version,
            pointer=pointer,
            payload=source,
            page_idx=page_idx,
            page_order=page_order,
        )
        if suffix:
            block_id = f"{block_id}-{suffix}"
        if block_id in seen_node_ids:
            raise AdapterError(f"stable node id collision: {block_id}")
        seen_node_ids.add(block_id)
        block: dict[str, Any] = {
            "id": block_id,
            "page": page_idx + 1,
            "bbox": bbox,
            "source": source,
            "source_sha256": sha256_text(source),
            "kind": kind,
            "translatable": translatable,
            "stats": {},
            "protected_spans": [],
            "links": [],
            "evidence": {
                "adapter": SPEC.id,
                "content_pointer": pointer,
                "content_item_pointer": f"/{item_index}",
                "content_item_sha256": canonical_json_sha256(item),
                "middle_pointer": middle_pointer,
                "middle_match": middle_match,
                "page_idx": page_idx,
                "page_order": page_order,
                "raw_type": item["type"],
                "raw_sub_type": item.get("sub_type"),
                "text_level": item.get("text_level"),
                "bbox_coordinate_system": "mineru-normalized-1000",
            },
        }
        if caption_parent is not None:
            block["caption_parent"] = caption_parent
        if adapter_role is not None:
            block["adapter_role"] = adapter_role
        blocks.append(block)
        return block_id

    for item_index, item in enumerate(content):
        item_pointer = f"/{item_index}"
        if not isinstance(item, dict):
            raise AdapterError(f"{item_pointer} must be an object")
        raw_type = item.get("type")
        if raw_type not in CONTENT_TYPES:
            raise AdapterError(f"{item_pointer}/type is unsupported: {raw_type!r}")
        raw_sub_type = item.get("sub_type")
        if raw_sub_type is not None and (
            not isinstance(raw_sub_type, str) or not raw_sub_type.strip()
        ):
            raise AdapterError(f"{item_pointer}/sub_type must be a nonempty string")
        if raw_sub_type is not None and raw_type not in {
            "image",
            "chart",
            "code",
            "list",
        }:
            raise AdapterError(f"{item_pointer}/sub_type is not defined for {raw_type}")
        source_id = item.get("id")
        if source_id is not None:
            if not isinstance(source_id, str) or not source_id.strip():
                raise AdapterError(f"{item_pointer}/id must be a nonempty string")
            if source_id in seen_source_ids:
                raise AdapterError(f"duplicate MinerU content id: {source_id!r}")
            seen_source_ids.add(source_id)
        page_idx = item.get("page_idx")
        if (
            not isinstance(page_idx, int)
            or isinstance(page_idx, bool)
            or not 0 <= page_idx < page_count
        ):
            raise AdapterError(f"{item_pointer}/page_idx is out of range")
        if page_idx < previous_page:
            raise AdapterError("content_list page_idx values must be nondecreasing")
        previous_page = page_idx
        page_orders[page_idx] += 1
        page_order = page_orders[page_idx]
        bbox = validate_content_bbox(item.get("bbox"), item_pointer)
        middle_pointer, middle_match = _middle_match(
            raw_type, bbox, middle_pages[page_idx]
        )
        node_ids: list[str] = []
        visual_ids: list[str] = []
        disposition = "emitted"
        reason = None

        def item_node(
            source: str,
            kind: str,
            translatable: bool,
            *,
            field: str,
            suffix: str = "",
            caption_parent: str | None = None,
            adapter_role: str | None = None,
        ) -> str:
            pointer = _json_subpointer(item_pointer, field)
            block_id = add_block(
                item=item,
                item_index=item_index,
                page_idx=page_idx,
                page_order=page_order,
                bbox=bbox,
                suffix=suffix,
                pointer=pointer,
                source=source,
                kind=kind,
                translatable=translatable,
                middle_pointer=middle_pointer,
                middle_match=middle_match,
                caption_parent=caption_parent,
                adapter_role=adapter_role,
            )
            node_ids.append(block_id)
            return block_id

        if raw_type == "text":
            text = _require_text(item.get("text"), f"{item_pointer}/text")
            text_level = item.get("text_level", 0)
            if (
                not isinstance(text_level, int)
                or isinstance(text_level, bool)
                or text_level < 0
            ):
                raise AdapterError(f"{item_pointer}/text_level must be a nonnegative integer")
            adapter_role = (
                "title" if text_level > 0 and not heading_seen else
                "heading" if text_level > 0 else
                "paragraph"
            )
            item_node(
                text,
                "heading" if text_level > 0 else "prose",
                True,
                field="text",
                adapter_role=adapter_role,
            )
            if text_level > 0:
                heading_seen = True
        elif raw_type == "equation":
            text = _require_text(item.get("text"), f"{item_pointer}/text")
            if item.get("text_format") not in (None, "latex"):
                raise AdapterError(f"{item_pointer}/text_format must be 'latex' when present")
            image_path = item.get("img_path")
            if image_path is not None:
                image_path = _require_text(image_path, f"{item_pointer}/img_path")
            parent = item_node(
                text,
                "math",
                False,
                field="text",
                adapter_role="equation_visual" if image_path is not None else "equation",
            )
            if image_path is not None:
                asset_paths.add(image_path)
                visual_id = f"visual-{parent}"
                visual_ids.append(visual_id)
                visual_requests.append(
                    {
                        "id": visual_id,
                        "page": page_idx + 1,
                        "kind": "math",
                        "anchor_id": parent,
                        "caption_id": None,
                        "bbox": bbox,
                        "source_asset": image_path,
                        "contained_block_ids": [parent],
                        "item_pointer": item_pointer,
                    }
                )
        elif raw_type in {"image", "chart", "table"}:
            if raw_type == "table":
                captions = _require_string_list(
                    item.get("table_caption"), f"{item_pointer}/table_caption"
                )
                footnotes = _require_string_list(
                    item.get("table_footnote"), f"{item_pointer}/table_footnote"
                )
                body = item.get("table_body")
                image_path = item.get("img_path")
                if not (isinstance(body, str) and body.strip()) and not (
                    isinstance(image_path, str) and image_path.strip()
                ):
                    raise AdapterError(f"{item_pointer} table needs table_body or img_path")
                if body is not None:
                    body = _require_text(body, f"{item_pointer}/table_body")
                    try:
                        body = validate_table_html(body)
                    except ValueError as exc:
                        raise AdapterError(
                            f"{item_pointer}/table_body is unsafe or invalid: {exc}"
                        ) from exc
            else:
                prefix = "image" if raw_type == "image" else "chart"
                captions = _require_string_list(
                    item.get(f"{prefix}_caption"), f"{item_pointer}/{prefix}_caption"
                )
                footnotes = _require_string_list(
                    item.get(f"{prefix}_footnote"), f"{item_pointer}/{prefix}_footnote"
                )
                image_path = _require_text(
                    item.get("img_path"), f"{item_pointer}/img_path"
                )
            visual_parent: str | None = None
            if raw_type == "table" and body is not None:
                # Pipeline tables commonly carry both editable HTML and a source
                # screenshot.  They are independent evidence: keep the table as
                # a native source-only node and model the screenshot separately
                # as visual-once so neither representation can erase the other.
                parent = item_node(
                    body,
                    "table",
                    False,
                    field="table_body",
                    adapter_role="table",
                )
                if image_path is not None:
                    visual_parent = item_node(
                        "",
                        "image",
                        False,
                        field="img_path",
                        suffix="visual",
                        adapter_role="table_visual",
                    )
            elif image_path is not None:
                parent = item_node(
                    "",
                    "image",
                    False,
                    field="img_path",
                    adapter_role=("table_visual" if raw_type == "table" else raw_type),
                )
                visual_parent = parent
            else:
                raise AdapterError(f"{item_pointer} visual content lacks an image or table body")

            if visual_parent is not None:
                image_path = _require_text(image_path, f"{item_pointer}/img_path")
                asset_paths.add(image_path)
                visual_id = f"visual-{visual_parent}"
                visual_ids.append(visual_id)
                visual_requests.append(
                    {
                        "id": visual_id,
                        "page": page_idx + 1,
                        "kind": raw_type,
                        "anchor_id": visual_parent,
                        "caption_id": None,
                        "bbox": bbox,
                        "source_asset": image_path,
                        "contained_block_ids": [visual_parent],
                        "item_pointer": item_pointer,
                    }
                )
            for index, caption in enumerate(captions):
                caption_id = item_node(
                    caption,
                    "caption",
                    True,
                    field=("table_caption" if raw_type == "table" else f"{raw_type}_caption")
                    + f"/{index}",
                    suffix=f"caption-{index + 1:02d}",
                    caption_parent=parent,
                    adapter_role=f"{raw_type}_caption",
                )
                if (
                    index == 0
                    and visual_requests
                    and visual_requests[-1].get("item_pointer") == item_pointer
                ):
                    visual_requests[-1]["caption_id"] = caption_id
            for index, footnote in enumerate(footnotes):
                item_node(
                    footnote,
                    "caption",
                    True,
                    field=("table_footnote" if raw_type == "table" else f"{raw_type}_footnote")
                    + f"/{index}",
                    suffix=f"footnote-{index + 1:02d}",
                    caption_parent=parent,
                    adapter_role=f"{raw_type}_footnote",
                )
        elif raw_type == "code":
            sub_type = item.get("sub_type")
            if sub_type not in {"code", "algorithm"}:
                raise AdapterError(f"{item_pointer}/sub_type is unsupported for pipeline code")
            body = _require_text(item.get("code_body"), f"{item_pointer}/code_body")
            parent = item_node(body, "code", False, field="code_body", adapter_role=sub_type)
            captions = _require_string_list(
                item.get("code_caption", []), f"{item_pointer}/code_caption"
            )
            footnotes = _require_string_list(
                item.get("code_footnote", []), f"{item_pointer}/code_footnote"
            )
            for index, caption in enumerate(captions):
                item_node(
                    caption,
                    "caption",
                    True,
                    field=f"code_caption/{index}",
                    suffix=f"caption-{index + 1:02d}",
                    caption_parent=parent,
                    adapter_role=f"{sub_type}_caption",
                )
            for index, footnote in enumerate(footnotes):
                item_node(
                    footnote,
                    "caption",
                    True,
                    field=f"code_footnote/{index}",
                    suffix=f"footnote-{index + 1:02d}",
                    caption_parent=parent,
                    adapter_role=f"{sub_type}_footnote",
                )
        elif raw_type == "list":
            if item.get("sub_type") != "ref_text":
                raise AdapterError(
                    f"{item_pointer}/sub_type must be 'ref_text' for pipeline legacy list"
                )
            values = _require_string_list(
                item.get("list_items"), f"{item_pointer}/list_items", nonempty=True
            )
            item_node(
                "\n".join(f"- {value}" for value in values),
                "list",
                True,
                field="list_items",
                adapter_role="reference",
            )
        else:
            text = _require_text(item.get("text"), f"{item_pointer}/text")
            policy = auxiliary.get(raw_type, "bilingual" if raw_type == "page_footnote" else "artifact-omitted")
            if policy not in {"bilingual", "source-only", "artifact-omitted"}:
                raise AdapterError(f"Profile has invalid auxiliary disposition for {raw_type}")
            if policy == "artifact-omitted":
                disposition = "artifact_omitted"
                reason = f"profile auxiliary policy for {raw_type}"
                item_node(text, "artifact", False, field="text", adapter_role=raw_type)
            else:
                item_node(
                    text,
                    "prose",
                    policy == "bilingual",
                    field="text",
                    adapter_role=raw_type,
                )

        if not node_ids and not visual_ids:
            raise AdapterError(f"{item_pointer} has no explicit disposition target")
        dispositions.append(
            {
                "pointer": item_pointer,
                "item_sha256": canonical_json_sha256(item),
                "page_idx": page_idx,
                "page_order": page_order,
                "raw_type": raw_type,
                "raw_sub_type": item.get("sub_type"),
                "disposition": disposition,
                "reason": reason,
                "node_ids": node_ids,
                "visual_ids": visual_ids,
                "middle_pointers": [middle_pointer] if middle_pointer else [],
                "middle_match": middle_match,
            }
        )
    return blocks, visual_requests, dispositions, asset_paths


def adapter_substantive_text_lengths(
    blocks: list[dict[str, Any]], page_count: int
) -> list[int]:
    """Count parser text independently for every page.

    Visual placeholders and profile-omitted page furniture are not substantive.
    Validated HTML tables contribute their cell text rather than markup bytes.
    """

    page_parts: list[list[str]] = [[] for _ in range(page_count)]
    for block in blocks:
        page = block.get("page")
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or not 1 <= page <= page_count
            or block.get("kind") in {"artifact", "image"}
        ):
            continue
        source = block.get("source")
        if not isinstance(source, str) or not source:
            continue
        if block.get("kind") == "table":
            source = unescape(re.sub(r"<[^>]+>", " ", source))
        page_parts[page - 1].append(source)
    return [len(normalize_text("\n".join(parts))) for parts in page_parts]


def adapter_text_exceeds_native_oracle(
    native_length: int,
    adapter_length: int,
    minimum_native_characters: int,
) -> bool:
    """Detect page-local parser text that the independent oracle cannot support.

    ``minimum_native_characters`` describes when Poppler alone is a sufficient
    native-text oracle; it must not also impose a minimum size on OCR/parser
    text.  On a short page, twice as much adapter text as native text is enough
    evidence of a substantive page-local gap and therefore requires review.
    Empty native pages are already caught by ``native_oracle_empty``.
    """

    return (
        0 < native_length < minimum_native_characters
        and adapter_length >= 2 * native_length
    )


def _rectangle_union_area(
    rectangles: list[tuple[float, float, float, float]],
) -> float:
    """Return the exact union area of axis-aligned rectangles."""

    x_edges = sorted({edge for rect in rectangles for edge in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(x_edges, x_edges[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (top, bottom)
            for x0, top, x1, bottom in rectangles
            if x0 < right and x1 > left
        )
        covered_height = 0.0
        current_top: float | None = None
        current_bottom: float | None = None
        for top, bottom in intervals:
            if current_top is None or current_bottom is None:
                current_top, current_bottom = top, bottom
            elif top > current_bottom:
                covered_height += current_bottom - current_top
                current_top, current_bottom = top, bottom
            else:
                current_bottom = max(current_bottom, bottom)
        if current_top is not None and current_bottom is not None:
            covered_height += current_bottom - current_top
        area += (right - left) * covered_height
    return area


def page_raster_coverage_ratios(document: fitz.Document) -> list[float]:
    """Measure displayed raster-image coverage from the source PDF itself.

    The measurement is independent of MinerU output and uses the union of image
    display rectangles, clipped to each page.  Overlapping images are counted
    once.  Values are rounded so importer and source-audit evidence is portable.
    """

    ratios: list[float] = []
    for page in document:
        page_rect = page.rect
        page_area = float(page_rect.width * page_rect.height)
        if not math.isfinite(page_area) or page_area <= 0:
            raise AdapterError(f"source PDF page {page.number + 1} has invalid geometry")
        rectangles: list[tuple[float, float, float, float]] = []
        for image in page.get_image_info(xrefs=True):
            bbox = image.get("bbox")
            if (
                not isinstance(bbox, (list, tuple))
                or len(bbox) != 4
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                    for value in bbox
                )
            ):
                raise AdapterError(
                    f"source PDF page {page.number + 1} has invalid image geometry"
                )
            # get_image_info() reports unrotated page coordinates, while
            # Page.rect reflects page rotation.  Map every image rectangle
            # through the page rotation matrix before clipping so both operands
            # share the same coordinate system.
            rotated_bbox = (
                fitz.Rect(*(float(value) for value in bbox)) * page.rotation_matrix
            )
            x0 = max(float(rotated_bbox.x0), float(page_rect.x0))
            y0 = max(float(rotated_bbox.y0), float(page_rect.y0))
            x1 = min(float(rotated_bbox.x1), float(page_rect.x1))
            y1 = min(float(rotated_bbox.y1), float(page_rect.y1))
            if x0 < x1 and y0 < y1:
                rectangles.append((x0, y0, x1, y1))
        union_area = _rectangle_union_area(rectangles)
        ratios.append(round(min(1.0, max(0.0, union_area / page_area)), 6))
    return ratios


def discover_inputs(source_pdf: Path, output_dir: Path) -> dict[str, Path | list[Path]]:
    if not output_dir.is_dir():
        raise AdapterError(f"MinerU output directory does not exist: {output_dir}")
    content_candidates = sorted(
        path
        for path in output_dir.glob("*_content_list.json")
        if not path.name.endswith("_content_list_v2.json")
    )
    if len(content_candidates) != 1:
        raise AdapterError(
            "MinerU output directory must contain exactly one legacy *_content_list.json"
        )
    content_path = content_candidates[0]
    prefix = content_path.name[: -len("_content_list.json")]
    middle_path = output_dir / f"{prefix}_middle.json"
    origin_path = output_dir / f"{prefix}_origin.pdf"
    if not middle_path.is_file():
        raise AdapterError(f"missing required MinerU artifact: {middle_path.name}")
    if not origin_path.is_file():
        raise AdapterError(
            f"missing {origin_path.name}; source binding cannot be proven without MinerU origin.pdf"
        )
    if sha256_file(origin_path) != sha256_file(source_pdf):
        raise AdapterError("MinerU origin.pdf hash does not match the supplied source PDF")
    optional = [
        output_dir / f"{prefix}_layout.pdf",
        output_dir / f"{prefix}_span.pdf",
        output_dir / f"{prefix}.md",
        output_dir / f"{prefix}_content_list_v2.json",
    ]
    return {
        "content": content_path,
        "middle": middle_path,
        "origin": origin_path,
        "optional": [path for path in optional if path.is_file()],
    }


def _prepare_work_dir(work_dir: Path, force: bool) -> None:
    collisions = [
        work_dir / "profile.json",
        work_dir / "manifest.json",
        work_dir / "blocks.jsonl",
        work_dir / "document-ir.json",
        work_dir / ADAPTER_EVIDENCE_FILENAME,
        work_dir / "source-audit.json",
    ]
    existing = [path for path in collisions if path.exists()]
    if existing and not force:
        raise AdapterError(
            "refusing to overwrite existing artifacts: "
            + ", ".join(path.name for path in existing)
            + "; use --force"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    if force:
        for path in collisions:
            if path.is_file():
                path.unlink()
        for directory in (
            "adapter-inputs",
            "adapter-assets",
            "renders",
            "visuals",
            "source-contact",
            "source-review",
            "source-review-contact",
            "source-review-layout",
            "source-review-span",
            "translation",
            "output",
        ):
            target = work_dir / directory
            if target.is_dir():
                if target.resolve().parent != work_dir:
                    raise AdapterError(f"refusing to remove unsafe work path: {target}")
                shutil.rmtree(target)


def _render_debug_pdf(
    pdftoppm: str,
    pdf_path: Path,
    render_dir: Path,
    dpi: int,
    page_count: int,
) -> list[Path]:
    render_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(render_dir / "page")],
        check=True,
    )
    renders = sorted(render_dir.glob("page-*.png"))
    if len(renders) != page_count:
        raise AdapterError(
            f"debug review PDF {pdf_path.name} has {len(renders)} rendered pages; "
            f"expected {page_count}"
        )
    return renders


def _make_manual_review_pages(
    *,
    source_renders: list[Path],
    debug_renders: list[tuple[str, list[Path]]],
    output_dir: Path,
    unavailable_labels: list[str],
) -> list[Path]:
    output_dir.mkdir(exist_ok=True)
    results: list[Path] = []
    for page_index, source_path in enumerate(source_renders):
        columns: list[tuple[str, Image.Image]] = []
        with Image.open(source_path) as source_image:
            columns.append(("Source PDF", source_image.convert("RGB")))
        for label, paths in debug_renders:
            with Image.open(paths[page_index]) as debug_image:
                columns.append((label, debug_image.convert("RGB")))
        target_height = min(1200, max(image.height for _, image in columns))
        prepared: list[tuple[str, Image.Image]] = []
        for label, image in columns:
            width = max(1, round(image.width * target_height / image.height))
            prepared.append(
                (label, image.resize((width, target_height), Image.Resampling.LANCZOS))
            )
        label_height = 34
        note_height = 42 if unavailable_labels else 0
        canvas = Image.new(
            "RGB",
            (
                sum(image.width for _, image in prepared) + 16 * (len(prepared) + 1),
                target_height + label_height + note_height + 16,
            ),
            "#d9dde5",
        )
        draw = ImageDraw.Draw(canvas)
        x = 16
        for label, image in prepared:
            draw.text((x, 8), f"Page {page_index + 1} - {label}", fill="black")
            canvas.paste(image, (x, label_height))
            x += image.width + 16
        if unavailable_labels:
            draw.text(
                (16, label_height + target_height + 8),
                "Not supplied: " + ", ".join(unavailable_labels),
                fill="#7a1f1f",
            )
        path = output_dir / f"page-{page_index + 1:04d}.png"
        canvas.save(path)
        results.append(path)
    return results


def import_mineru(
    source_pdf: Path,
    output_dir: Path,
    work_dir: Path,
    profile_reference: str | Path,
    *,
    render_dpi: int = 120,
    force: bool = False,
) -> dict[str, Any]:
    from document_ir import write_document_ir
    from extract_pdf import (
        command_version,
        repair_truncated_renders,
        require_command,
        run_text,
        split_poppler_pages,
    )
    from profile import bind_profile, canonical_profile_sha256, load_profile

    source_pdf = source_pdf.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    work_dir = work_dir.expanduser().resolve()
    if not source_pdf.is_file() or source_pdf.suffix.lower() != ".pdf":
        raise AdapterError(f"input is not an existing PDF: {source_pdf}")
    if not 72 <= render_dpi <= 300:
        raise AdapterError("render_dpi must be between 72 and 300")
    profile = load_profile(profile_reference)
    if profile["input"]["adapter"] != SPEC.id:
        raise AdapterError(f"Profile {profile['id']} does not select adapter {SPEC.id}")

    inputs = discover_inputs(source_pdf, output_dir)
    content = load_strict_json(inputs["content"])
    middle = load_strict_json(inputs["middle"])
    try:
        document = fitz.open(source_pdf)
    except Exception as exc:
        raise AdapterError(f"cannot open source PDF: {exc}") from exc
    if document.needs_pass:
        document.close()
        raise AdapterError("encrypted PDFs are not supported")
    if document.page_count == 0:
        document.close()
        raise AdapterError("source PDF has no pages")
    page_count = document.page_count
    source_page_sizes = [
        [float(document[index].rect.width), float(document[index].rect.height)]
        for index in range(page_count)
    ]
    try:
        raster_coverage_ratios = page_raster_coverage_ratios(document)
    except Exception as exc:
        document.close()
        if isinstance(exc, AdapterError):
            raise
        raise AdapterError(f"cannot inspect source PDF raster geometry: {exc}") from exc

    backend, version, middle_pages, middle_asset_paths = validate_middle(
        middle, page_count
    )
    blocks, visual_requests, item_dispositions, content_asset_paths = normalize_content(
        content,
        source_sha256=sha256_file(source_pdf),
        backend=backend,
        version=version,
        page_count=page_count,
        middle_pages=middle_pages,
        profile=profile,
    )
    assets = validate_assets(output_dir, content_asset_paths | middle_asset_paths)
    assets_by_path = {item["relative_path"]: item for item in assets}

    pdftotext = require_command("pdftotext")
    pdftoppm = require_command("pdftoppm")
    oracle_text = run_text([pdftotext, "-raw", str(source_pdf), "-"])
    oracle_layout_text = run_text([pdftotext, "-layout", str(source_pdf), "-"])
    oracle_pages = split_poppler_pages(oracle_text)
    oracle_layout_pages = split_poppler_pages(oracle_layout_text)
    if len(oracle_pages) != page_count or len(oracle_layout_pages) != page_count:
        document.close()
        raise AdapterError("Poppler page count does not match the source PDF")

    minimum_chars = profile["input"]["minimum_text_characters_per_page"]
    minimum_ratio = float(profile["input"]["minimum_native_text_page_ratio"])
    native_lengths = [len(normalize_text(page)) for page in oracle_pages]
    native_ascii_letter_ratios = [
        len(re.findall(r"[A-Za-z]", normalize_text(page))) / max(1, length)
        for page, length in zip(oracle_pages, native_lengths)
    ]
    native_pages = sum(length >= minimum_chars for length in native_lengths)
    native_ratio = native_pages / page_count
    blocks_per_page = Counter(int(block["page"]) for block in blocks)
    adapter_lengths = adapter_substantive_text_lengths(blocks, page_count)
    manual_reasons_by_page: list[list[str]] = []
    for index, (native_length, adapter_length) in enumerate(
        zip(native_lengths, adapter_lengths)
    ):
        reasons: list[str] = []
        if native_length == 0:
            reasons.append("native_oracle_empty")
        if (
            native_length >= minimum_chars
            and native_ascii_letter_ratios[index] < 0.2
        ):
            reasons.append("native_oracle_not_substantive_english")
        if native_ratio < minimum_ratio and native_length < minimum_chars:
            reasons.append("document_native_ratio_below_threshold")
        # MinerU cannot prove the completeness of its own OCR.  A large raster
        # region in the source PDF is independent evidence that a low-text page
        # may be scanned, even if MinerU emits one character or no text at all.
        if (
            native_length < minimum_chars
            and raster_coverage_ratios[index] >= LARGE_RASTER_PAGE_AREA_RATIO
        ):
            reasons.append("large_raster_without_native_oracle")
        # A document-wide ratio cannot absolve one page whose parser text is
        # substantial relative to its independent Poppler layer.  The adapter
        # text does not need to reach the Profile's native-page character
        # threshold: short scanned lecture/slide pages need the same gate.
        if adapter_text_exceeds_native_oracle(
            native_length, adapter_length, minimum_chars
        ):
            reasons.append("adapter_text_without_native_oracle")
        manual_reasons_by_page.append(reasons)
    manual_pages = [
        index + 1
        for index, reasons in enumerate(manual_reasons_by_page)
        if reasons
    ]
    page_evidence = []
    for index, page in enumerate(middle_pages):
        source_size = source_page_sizes[index]
        mineru_size = page["page_size"]
        source_ratio = source_size[0] / source_size[1]
        mineru_ratio = mineru_size[0] / mineru_size[1]
        ratio_error = min(
            abs(source_ratio - mineru_ratio) / max(source_ratio, mineru_ratio),
            abs((1 / source_ratio) - mineru_ratio)
            / max(1 / source_ratio, mineru_ratio),
        )
        if ratio_error > 0.02:
            document.close()
            raise AdapterError(
                f"middle.json page_size aspect ratio disagrees with source page {index + 1}"
            )
        page_evidence.append(
            {
                "page_idx": index,
                "page_size": mineru_size,
                "source_page_size": source_size,
                "native_text_characters": native_lengths[index],
                "adapter_text_characters": adapter_lengths[index],
                "raster_image_area_ratio": raster_coverage_ratios[index],
                "manual_review_reasons": manual_reasons_by_page[index],
                "status": (
                    "manual_source_review_required"
                    if index + 1 in manual_pages
                    else "native_oracle_available"
                ),
            }
        )

    # All validation above is side-effect free. Only now publish a coherent work directory.
    _prepare_work_dir(work_dir, force)
    try:
        bound_profile = bind_profile(work_dir, profile_reference, force=force)
        frozen_inputs_dir = work_dir / "adapter-inputs"
        frozen_inputs_dir.mkdir(exist_ok=True)
        input_records = []
        input_pairs = [
            ("origin", inputs["origin"]),
            ("content", inputs["content"]),
            ("middle", inputs["middle"]),
            *(("debug", path) for path in inputs["optional"]),
        ]
        for role, path in input_pairs:
            destination = frozen_inputs_dir / path.name
            shutil.copyfile(path, destination)
            digest = sha256_file(path)
            if sha256_file(destination) != digest:
                raise AdapterError(f"input changed while importing: {path.name}")
            input_records.append(
                {
                    "role": role,
                    "relative_path": path.relative_to(output_dir).as_posix(),
                    "work_path": f"adapter-inputs/{destination.name}",
                    "sha256": digest,
                    "size": destination.stat().st_size,
                }
            )
        input_records.sort(key=lambda item: (item["role"], item["relative_path"]))

        frozen_assets_dir = work_dir / "adapter-assets"
        for asset in assets:
            destination = frozen_assets_dir.joinpath(
                *PurePosixPath(asset["relative_path"]).parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset["source_path"], destination)
            if sha256_file(destination) != asset["sha256"]:
                raise AdapterError(
                    f"asset changed while importing: {asset['relative_path']}"
                )
            asset["frozen_path"] = destination
            asset["work_path"] = destination.relative_to(work_dir).as_posix()

        visuals_dir = work_dir / "visuals"
        visuals_dir.mkdir(exist_ok=True)
        visuals: list[dict[str, Any]] = []
        for request in visual_requests:
            asset = assets_by_path[request["source_asset"]]
            suffix = asset["source_path"].suffix.lower() or ".bin"
            destination = visuals_dir / f"{request['id']}{suffix}"
            shutil.copyfile(asset["frozen_path"], destination)
            if sha256_file(destination) != asset["sha256"]:
                raise AdapterError(f"asset changed while importing: {asset['relative_path']}")
            visual = dict(request)
            visual.pop("source_asset")
            visual.pop("item_pointer")
            visual["path"] = f"visuals/{destination.name}"
            visual["sha256"] = asset["sha256"]
            visual["asset_id"] = asset["id"]
            visuals.append(visual)

        renders_dir = work_dir / "renders"
        renders_dir.mkdir(exist_ok=True)
        prefix = renders_dir / "page"
        subprocess.run(
            [
                pdftoppm,
                "-png",
                "-r",
                str(render_dpi),
                str(source_pdf),
                str(prefix),
            ],
            check=True,
        )
        renders = sorted(renders_dir.glob("page-*.png"))
        if len(renders) != page_count:
            raise AdapterError(
                f"rendering incomplete: expected {page_count}, found {len(renders)}"
            )
        repair_truncated_renders(pdftoppm, source_pdf, renders, render_dpi)
        source_contacts = make_contact_sheets(renders, work_dir / "source-contact")
        source_review_contacts: list[dict[str, Any]] = []
        source_review_pages: list[str] = []
        if manual_pages:
            debug_renders: list[tuple[str, list[Path]]] = []
            unavailable_labels: list[str] = []
            for suffix, label in (("_layout.pdf", "MinerU layout"), ("_span.pdf", "MinerU span")):
                record = next(
                    (
                        item
                        for item in input_records
                        if item["role"] == "debug"
                        and item["relative_path"].endswith(suffix)
                    ),
                    None,
                )
                if record is None:
                    unavailable_labels.append(label)
                    continue
                debug_path = work_dir / record["work_path"]
                debug_dir = work_dir / f"source-review-{suffix[1:-4]}"
                pages = _render_debug_pdf(
                    pdftoppm, debug_path, debug_dir, render_dpi, page_count
                )
                repair_truncated_renders(
                    pdftoppm, debug_path, pages, render_dpi
                )
                debug_renders.append((label, pages))
            review_pages = _make_manual_review_pages(
                source_renders=renders,
                debug_renders=debug_renders,
                output_dir=work_dir / "source-review",
                unavailable_labels=unavailable_labels,
            )
            source_review_pages = [
                path.relative_to(work_dir).as_posix() for path in review_pages
            ]
            source_review_contacts = make_contact_sheets(
                review_pages, work_dir / "source-review-contact"
            )

        (work_dir / "oracle.txt").write_text(oracle_text, encoding="utf-8")
        (work_dir / "oracle-layout.txt").write_text(
            oracle_layout_text, encoding="utf-8"
        )
        links: list[dict[str, Any]] = []
        uri_to_id: dict[str, str] = {}
        for block in blocks:
            for uri in _extract_urls(block["source"]):
                link_id = uri_to_id.get(uri)
                if link_id is None:
                    link_id = f"link-{len(uri_to_id) + 1:04d}"
                    uri_to_id[uri] = link_id
                    links.append(
                        {
                            "id": link_id,
                            "page": block["page"],
                            "bbox": block["bbox"],
                            "uri": uri,
                            "target_page": None,
                        }
                    )
                block["links"].append(link_id)
        write_jsonl(work_dir / "blocks.jsonl", blocks)

        evidence_assets = [
            {
                key: value
                for key, value in asset.items()
                if key not in {"source_path", "frozen_path"}
            }
            for asset in assets
        ]
        for asset in assets:
            if sha256_file(asset["source_path"]) != asset["sha256"]:
                raise AdapterError(
                    f"asset changed between validation and commit: {asset['relative_path']}"
                )
        adapter_evidence = {
            "schema_version": 1,
            "adapter": SPEC.id,
            "source": {
                "logical_name": source_pdf.name,
                "sha256": sha256_file(source_pdf),
                "page_count": page_count,
            },
            "mineru": {
                "version": version,
                "backend": backend,
                "support_level": (
                    "verified" if version == VERIFIED_VERSION else "compatible-unverified"
                ),
            },
            "raster_detection": {
                "method": RASTER_COVERAGE_METHOD,
                "large_page_area_ratio": LARGE_RASTER_PAGE_AREA_RATIO,
            },
            "inputs": input_records,
            "assets": evidence_assets,
            "pages": page_evidence,
            "items": item_dispositions,
            "manual_source_review_required": bool(manual_pages),
            "manual_review_pages": manual_pages,
            "manual_review_page_comparisons": source_review_pages,
            "manual_review_contact_sheets": source_review_contacts,
        }
        evidence_path = work_dir / ADAPTER_EVIDENCE_FILENAME
        write_json(evidence_path, adapter_evidence)

        external_uris = sorted(uri_to_id)
        manifest = {
            "schema_version": 4,
            "profile": {
                "id": bound_profile["id"],
                "sha256": canonical_profile_sha256(bound_profile),
            },
            "source_pdf": str(source_pdf),
            "source_sha256": sha256_file(source_pdf),
            "page_count": page_count,
            "native_text_page_ratio": round(native_ratio, 4),
            "render_dpi": render_dpi,
            "adapter": {
                "id": SPEC.id,
                "evidence": ADAPTER_EVIDENCE_FILENAME,
                "evidence_sha256": sha256_file(evidence_path),
                "backend": backend,
                "version": version,
            },
            "input_artifacts": input_records,
            "artifacts": {
                "profile": "profile.json",
                "document_ir": "document-ir.json",
                "adapter_evidence": ADAPTER_EVIDENCE_FILENAME,
                "blocks": "blocks.jsonl",
                "oracle": "oracle.txt",
                "oracle_layout": "oracle-layout.txt",
                "renders": "renders/page-*.png",
                "visuals": "visuals/*",
                "source_contact": "source-contact/contact-*.png",
                "source_review": "source-review/page-*.png",
                "source_review_contact": "source-review-contact/contact-*.png",
            },
            "tools": {
                "pymupdf": fitz.VersionBind,
                "pdftotext": command_version(pdftotext, "-v"),
                "pdftoppm": command_version(pdftoppm, "-v"),
                "mineru": version,
                "mineru_backend": backend,
            },
            "pages": [
                {
                    "page": index + 1,
                    "block_count": blocks_per_page[index + 1],
                    "native_text_characters": native_lengths[index],
                    "native_ascii_letter_ratio": round(
                        native_ascii_letter_ratios[index], 4
                    ),
                    "adapter_text_characters": adapter_lengths[index],
                    "raster_image_area_ratio": raster_coverage_ratios[index],
                    "manual_review_reasons": manual_reasons_by_page[index],
                    "manual_source_review_required": index + 1 in manual_pages,
                }
                for index in range(page_count)
            ],
            "block_count": len(blocks),
            "block_kind_counts": dict(Counter(block["kind"] for block in blocks)),
            "problem_ids": sorted(set(problem_ids(oracle_text))),
            "external_uris": external_uris,
            "external_uri_count": len(external_uris),
            "internal_link_count": 0,
            "links": links,
            "visuals": visuals,
            "unresolved_visual_anchors": [],
            "source_contact_sheets": source_contacts,
            "source_review_pages": source_review_pages,
            "source_review_contact_sheets": source_review_contacts,
            "drawing_pages": [],
        }
        write_json(work_dir / "manifest.json", manifest)
        ir_path = write_document_ir(work_dir, bound_profile)
        document_ir = json.loads(ir_path.read_text(encoding="utf-8"))
    except Exception:
        document.close()
        raise
    document.close()
    return {
        "status": (
            "manual_source_review_required" if manual_pages else "passed"
        ),
        "work_dir": str(work_dir),
        "profile": profile["id"],
        "adapter": SPEC.id,
        "mineru_backend": backend,
        "mineru_version": version,
        "pages": page_count,
        "nodes": document_ir["inventories"]["node_count"],
        "items": len(item_dispositions),
        "manual_review_pages": manual_pages,
    }
