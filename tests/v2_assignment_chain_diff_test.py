#!/usr/bin/env python3
"""Freeze a real native assignment V1 chain and its V1-to-V2 projection."""

from __future__ import annotations

import json
import io
import copy
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image, ImageChops

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from common import read_json, read_jsonl, sha256_file, sha256_text, write_json, write_jsonl  # noqa: E402
from job_state import evaluate_job  # noqa: E402
from profile import canonical_profile_sha256, load_profile  # noqa: E402
from self_test import run  # noqa: E402

FIXTURE_DIR = REPOSITORY / "tests" / "fixtures" / "profiles"
SNAPSHOT_FIXTURE = FIXTURE_DIR / "assignment-en-zh-v1-expanded-chain-snapshot.json"
PROJECTION_FIXTURE = FIXTURE_DIR / "assignment-en-zh-v1-v2-expanded-projection.json"
SOURCE_PDF_FIXTURE = FIXTURE_DIR / "assignment-native-source-expanded.pdf"
SOURCE_PDF_SHA256 = "0cfe0a798f92722245ead59d9220b3834af1b38cc8e6e531bb417eca295398ef"
NATIVE_KINDS = frozenset({
    "image", "artifact", "code", "math", "math_with_text", "caption",
    "callout", "heading", "list", "prose", "caption_continuation", "visual_content",
})
KIND_ROLE = {
    "caption_continuation": "caption-continuation", "visual_content": "visual-content",
    "heading": "heading", "list": "list-item", "prose": "paragraph",
    "caption": "caption", "math_with_text": "math-with-text", "code": "code",
    "math": "math", "image": "image", "artifact": "artifact",
}


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)
    return env


def _last_pipeline_json(command: str, output: str) -> dict:
    decoder = json.JSONDecoder()
    offset = 0
    reports: list[dict] = []
    while offset < len(output):
        while offset < len(output) and output[offset].isspace():
            offset += 1
        if offset == len(output):
            break
        try:
            report, offset = decoder.raw_decode(output, offset)
        except json.JSONDecodeError:
            next_line = output.find("\n", offset)
            if next_line < 0:
                break
            offset = next_line + 1
            continue
        if isinstance(report, dict):
            reports.append(report)
    if not reports:
        raise AssertionError(f"pipeline {command} emitted no JSON report: {output}")
    return reports[-1]


def _pipeline(
    arguments: list[str], *, expect_success: bool = True
) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "pipeline.py"), *arguments],
        check=False, capture_output=True, text=True, env=_subprocess_env(),
    )
    if expect_success and completed.returncode != 0:
        raise AssertionError(
            f"pipeline {' '.join(arguments)} failed: {completed.stdout}{completed.stderr}"
        )
    if not expect_success and completed.returncode == 0:
        raise AssertionError(f"pipeline {' '.join(arguments)} unexpectedly succeeded")
    report = (
        _last_pipeline_json(arguments[0], completed.stdout)
        if completed.returncode == 0 else None
    )
    return completed, report


def _run_pipeline_stage(command: str, work_dir: Path, *arguments: str) -> dict:
    return _pipeline([command, str(work_dir), *arguments])[1] or {}


def _source_pipeline(source_pdf: Path, work_dir: Path, profile_path: Path) -> dict:
    return _pipeline([
        "source", str(source_pdf), "--work-dir", str(work_dir),
        "--profile", str(profile_path), "--render-dpi", "96",
    ])[1] or {}


def _translation_for(request: dict) -> str:
    source = request["source_for_translation"]
    if source.startswith("This page deliberately"):
        return "本页有意重复完全相同的数学说明及其原文截图。"
    if source.startswith("where "):
        return source.replace("where ", "其中 ", 1).replace("and ", "且 ").replace("\n", "，")
    if source.startswith("Problem ("):
        return source.replace("Problem (", "问题（", 1).replace("):", "）：", 1) + "（中文说明）"
    if source.startswith("Example ("):
        return source.replace("Example (", "示例（", 1).replace("):", "）：", 1) + "（中文说明）"
    if source.startswith("Low-Resource Tip:"):
        return source.replace("Low-Resource Tip:", "低资源提示：", 1) + "（中文说明）"
    return "中文译文：" + source


def validate_synthetic_placeholder_placement(request: dict, translation: str) -> None:
    placeholders = [item["placeholder"] for item in request["protected_tokens"]]
    for placeholder in placeholders:
        if translation.count(placeholder) != 1:
            raise AssertionError(
                f"translation must place {placeholder!r} exactly once for {request['id']}"
            )
    if placeholders and translation.rstrip().endswith(" ".join(placeholders)):
        raise AssertionError("protected placeholders must not be appended as evidence")


def write_chain_responses(work_dir: Path) -> None:
    requests = [
        row
        for path in sorted((work_dir / "translation" / "requests").glob("part-*.jsonl"))
        for row in read_jsonl(path)
    ]
    responses = []
    for request in requests:
        translation = _translation_for(request)
        validate_synthetic_placeholder_placement(request, translation)
        responses.append({
            "id": request["id"], "source_sha256": request["source_sha256"],
            "translation": translation,
        })
    write_jsonl(
        work_dir / "translation" / "responses" / "part-0001.jsonl", responses
    )


def _assert_real_source_chain(work_dir: Path, source_pdf: Path = SOURCE_PDF_FIXTURE) -> list[dict]:
    manifest = read_json(work_dir / "manifest.json")
    audit = read_json(work_dir / "source-audit.json")
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    kinds = {block["kind"] for block in blocks}
    if kinds != NATIVE_KINDS:
        raise AssertionError(
            f"native kind coverage drifted: missing={sorted(NATIVE_KINDS-kinds)}, "
            f"extra={sorted(kinds-NATIVE_KINDS)}"
        )
    if manifest["source_sha256"] != sha256_file(source_pdf):
        raise AssertionError("manifest is not bound to the frozen source PDF")
    oracle_fivegrams = sum(
        page.get("oracle_fivegrams", 0) for page in audit.get("page_results", [])
    )
    if audit.get("status") != "passed" or oracle_fivegrams <= 0:
        raise AssertionError(f"source audit is not real or did not pass: {audit}")
    if audit.get("global_fivegram_coverage", 0) < 0.95:
        raise AssertionError(f"source coverage regressed: {audit}")
    oracle = (work_dir / "oracle.txt").read_text(encoding="utf-8")
    for block in blocks:
        first_line = block["source"].splitlines()[0] if block["source"] else ""
        if first_line and block["kind"] != "artifact" and first_line not in oracle:
            raise AssertionError(f"block is absent from Poppler oracle: {block['id']}")
    return blocks


def build_work_dir(root: Path, profile: dict | None = None, source_pdf: Path = SOURCE_PDF_FIXTURE) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    profile = profile or load_profile("assignment-en-zh")
    profile_path = root / "input-profile.json"
    write_json(profile_path, profile)
    work_dir = root / "work"
    _source_pipeline(source_pdf, work_dir, profile_path)
    _assert_real_source_chain(work_dir, source_pdf)
    _run_pipeline_stage("prepare", work_dir, "--max-source-chars", "1000")
    return work_dir


def _run_script(script: str, work_dir: Path) -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / script), str(work_dir)],
        check=False, capture_output=True, text=True, env=_subprocess_env(),
    )
    if completed.returncode != 0:
        raise AssertionError(f"{script} failed: {completed.stdout}{completed.stderr}")
    return json.loads(completed.stdout)


def _canonical_policy(kind: str) -> str:
    if kind == "artifact":
        return "artifact-omitted"
    if kind in {"image", "math", "visual_content"}:
        return "visual-once"
    if kind == "code":
        return "source-only"
    return "bilingual"


def _normalized_disposition(value: str) -> str:
    if value in {"artifact_omitted", "artifact-omitted"}:
        return "artifact-omitted"
    if value in {"image_visual", "math_visual", "visual-once", "preserved_inside_visual"}:
        return "visual-once"
    if value in {"source_code_once", "source-only"}:
        return "source-only"
    if value.startswith("bilingual") or value == "grouped_with_caption":
        return "bilingual"
    return value


def _visible_markdown(markdown: str) -> str:
    value = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    value = re.sub(r"<a\s+id=[^>]+></a>", "", value)
    return "\n".join(line.rstrip() for line in value.splitlines() if line.strip()) + "\n"


def _projection(work_dir: Path) -> dict:
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    ir = read_json(work_dir / "document-ir.json")
    manifest = read_json(work_dir / "output" / "build-manifest.json")
    markdown = (work_dir / "output" / manifest["markdown"]).read_text(encoding="utf-8")
    nodes = {node["id"]: node for node in ir["nodes"]}
    normalized_nodes = []
    for block in blocks:
        anchor = nodes[block["id"]].get("semantic_anchor") or {}
        role = anchor.get("role") or KIND_ROLE.get(block["kind"])
        disposition = _normalized_disposition(manifest["dispositions"][block["id"]])
        expected = _canonical_policy(block["kind"])
        if disposition != expected:
            raise AssertionError(
                f"{block['id']} disposition {disposition!r} != {expected!r}"
            )
        normalized_nodes.append({
            "id": block["id"], "kind": block["kind"], "source": block["source"],
            "source_sha256": block["source_sha256"], "role": role,
            "output": disposition,
        })
    visible = _visible_markdown(markdown)
    return {
        "source_pdf_sha256": read_json(work_dir / "manifest.json")["source_sha256"],
        "block_order": [block["id"] for block in blocks],
        "kind_counts": dict(sorted(Counter(block["kind"] for block in blocks).items())),
        "nodes": normalized_nodes,
        "semantic_groups": [
            {key: group[key] for key in (
                "role", "identifier", "anchor_node_id", "member_node_ids",
                "membership", "source_pages",
            )}
            for group in ir["semantic_groups"]
            if group["role"] in {"problem", "example", "tip"}
        ],
        "normalized_disposition_counts": dict(sorted(
            Counter(node["output"] for node in normalized_nodes).items()
        )),
        "visible_markdown_sha256": sha256_text(visible),
        "visible_markdown": visible,
    }


def run_assignment_chain(root: Path, profile: dict | None = None) -> dict:
    work_dir = build_work_dir(root, profile)
    write_chain_responses(work_dir)
    run("audit_translation.py", work_dir, True)
    build = _run_script("build_outputs.py", work_dir)
    output_audit = _run_script("audit_outputs.py", work_dir)
    ir = read_json(work_dir / "document-ir.json")
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    source_audit = read_json(work_dir / "source-audit.json")
    status = evaluate_job(work_dir).status_report
    return {
        "source_pdf_sha256": read_json(work_dir / "manifest.json")["source_sha256"],
        "source_audit": {
            "status": source_audit["status"],
            "oracle_fivegrams": sum(
                page["oracle_fivegrams"] for page in source_audit["page_results"]
            ),
            "global_fivegram_coverage": source_audit["global_fivegram_coverage"],
        },
        "block_order": [block["id"] for block in blocks],
        "kind_counts": dict(sorted(Counter(block["kind"] for block in blocks).items())),
        "ir_schema_version": ir["schema_version"], "ir_profile": ir["profile"],
        "ir_inventories": ir["inventories"],
        "ir_semantic_groups": ir["semantic_groups"], "ir_nodes": ir["nodes"],
        "build_status": build.get("status"),
        "output_audit_status": output_audit.get("status"),
        "gate_statuses": status["gate_statuses"], "next_action": status["next_action"],
        "cross_schema_projection": _projection(work_dir),
        "expected_schema_changes": {
            "profile_schema_version": (profile or load_profile("assignment-en-zh"))["schema_version"],
            "profile_canonical_sha256": canonical_profile_sha256(profile or load_profile("assignment-en-zh")),
            "ir_schema_version": ir["schema_version"],
            "ir_profile_sha256": ir["profile"]["sha256"],
        },
    }


def _docx_projection(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:body//w:p", ns):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns))
        style = paragraph.find("./w:pPr/w:pStyle", ns)
        borders = paragraph.find("./w:pPr/w:pBdr", ns)
        paragraphs.append({
            "text": text,
            "style": style.get(f"{{{ns['w']}}}val") if style is not None else None,
            "border_sides": sorted(child.tag.rsplit("}", 1)[-1] for child in borders)
            if borders is not None else [],
        })
    return {
        "paragraphs": paragraphs,
        "nonempty_text_sha256": sha256_text("\n".join(
            item["text"] for item in paragraphs if item["text"]
        )),
    }


def _render_projection(render_dir: Path) -> dict:
    pages = []
    for path in sorted(render_dir.glob("page-*.png")):
        with Image.open(path).convert("RGB") as image:
            bbox = ImageChops.difference(
                image, Image.new("RGB", image.size, "white")
            ).getbbox()
            pages.append({
                "size": list(image.size),
                "content_bbox": list(bbox) if bbox else None,
                "sha256": sha256_file(path),
            })
    return {"page_count": len(pages), "pages": pages}


def run_assignment_document_chain(root: Path, profile: dict | None = None, source_pdf: Path = SOURCE_PDF_FIXTURE) -> dict:
    work_dir = build_work_dir(root, profile, source_pdf)
    return finish_assignment_document_chain(work_dir, profile)


def finish_assignment_document_chain(work_dir: Path, profile: dict | None = None) -> dict:
    write_chain_responses(work_dir)
    run("audit_translation.py", work_dir, True)
    _run_script("build_outputs.py", work_dir)
    _run_script("audit_outputs.py", work_dir)
    build = read_json(work_dir / "output" / "build-manifest.json")
    basename = Path(build["markdown"]).stem
    _run_pipeline_stage(
        "docx", work_dir, "--markdown", str(work_dir / "output" / build["markdown"]),
        "--basename", basename, "--minimum-images", "0",
    )
    _run_pipeline_stage("compile-docx", work_dir, "--basename", basename, "--dpi", "96")
    output = work_dir / "output"
    compile_audit = read_json(output / "compile-audit.json")
    docx_audit = read_json(output / "docx-audit.json")
    gate_paths = [
        work_dir / "source-audit.json",
        work_dir / "translation" / "translation-audit.json",
        output / "output-audit.json", output / "docx-audit.json",
        output / "compile-audit.json",
    ]
    before = {str(path.relative_to(work_dir)): sha256_file(path) for path in gate_paths}
    failed, _ = _pipeline(["finalize", str(work_dir)], expect_success=False)
    after = {str(path.relative_to(work_dir)): sha256_file(path) for path in gate_paths}
    if before != after:
        raise AssertionError("failed finalize mutated an existing passed gate")
    qa_path = output / "qa-report.json"
    if qa_path.exists() and read_json(qa_path).get("status") == "passed":
        raise AssertionError("finalize manufactured a passed QA report")
    status = evaluate_job(work_dir).status_report
    if compile_audit.get("automated_status") != "passed":
        raise AssertionError(f"automated compile did not pass: {compile_audit}")
    if status["gate_statuses"].get("visual_review") == "passed":
        raise AssertionError("CI manufactured human visual approval")
    docx_path = output / compile_audit["docx"]
    return {
        "work_dir": str(work_dir),
        "profile_schema_version": (profile or load_profile("assignment-en-zh"))["schema_version"],
        "projection": _projection(work_dir),
        "docx_projection": _docx_projection(docx_path),
        "render_projection": _render_projection(output / "pdf-renders"),
        "docx_sha256": sha256_file(docx_path),
        "pdf_sha256": sha256_file(output / compile_audit["pdf"]),
        "docx_audit": docx_audit, "compile_audit": compile_audit,
        "finalize": {
            "returncode": failed.returncode,
            "gate_hashes_unchanged": before == after,
            "qa_passed": False,
        },
        "gate_statuses": status["gate_statuses"], "next_action": status["next_action"],
    }


class AssignmentChainDiffTests(unittest.TestCase):
    def test_native_source_pdf_bytes_are_frozen(self) -> None:
        self.assertEqual(sha256_file(SOURCE_PDF_FIXTURE), SOURCE_PDF_SHA256)

    def test_original_review_oracles_remain_byte_frozen(self) -> None:
        import hashlib
        historical = {'assignment-native-source.pdf': '21109d8a3109df0840305fe5cf305cf3924262cd6b1232d6b7a6e3225e6e0edd', 'assignment-en-zh-v1-chain-snapshot.json': '3dbd9d8d18cca935bd07e8da2bb2debeaf3cc35841411daaf82e9ffd8dc5490e', 'assignment-en-zh-v1-v2-projection.json': '5212e3509da860275254038fa849d1ceede1cec4eed986a7c125d76336e5fdbf'}
        for name, expected in historical.items():
            payload = (FIXTURE_DIR / name).read_bytes()
            if name.endswith(".json"):
                payload = payload.replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(payload).hexdigest(), expected, name)

    def test_expanded_v1_snapshot_is_frozen(self) -> None:
        frozen = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="assignment-chain-diff-") as temp:
            snapshot = run_assignment_chain(Path(temp))
        self.assertEqual({
            key: value for key, value in snapshot.items()
            if key not in {"cross_schema_projection", "expected_schema_changes"}
        }, frozen)

    def test_v1_and_candidate_v2_match_the_normalized_projection(self) -> None:
        from v2_migration_contract_test import build_candidate_v2
        v1 = load_profile("assignment-en-zh")
        v2 = build_candidate_v2(v1)
        with tempfile.TemporaryDirectory(prefix="assignment-v1-v2-projection-") as temp:
            root = Path(temp)
            v1_result = run_assignment_chain(root / "v1", v1)
            v2_result = run_assignment_chain(root / "v2", v2)
        self.assertEqual(v1_result["cross_schema_projection"], v2_result["cross_schema_projection"])
        self.assertEqual(
            v1_result["cross_schema_projection"],
            json.loads(PROJECTION_FIXTURE.read_text(encoding="utf-8")),
        )
        self.assertEqual((
            v1_result["expected_schema_changes"]["profile_schema_version"],
            v2_result["expected_schema_changes"]["profile_schema_version"],
        ), (1, 2))
        self.assertNotEqual(
            v1_result["expected_schema_changes"]["profile_canonical_sha256"],
            v2_result["expected_schema_changes"]["profile_canonical_sha256"],
        )


def write_repeated_visual_source(source: Path) -> Path:
    import pymupdf as fitz
    with fitz.open(SOURCE_PDF_FIXTURE) as document:
        page = document.new_page(width=612, height=792)
        page.insert_textbox(fitz.Rect(54, 100, 558, 150),
                            "This page deliberately repeats the identical mathematical explanation and its source crop.",
                            fontsize=11, lineheight=1.2)
        page.insert_textbox(fitz.Rect(54, 422, 558, 466), "where x = 2\nand y = 3",
                            fontsize=12, lineheight=1.2)
        document.save(source)
    return source


class CompositeDocxEvidenceTests(unittest.TestCase):
    def test_chinese_translation_and_exact_bound_image_are_required(self) -> None:
        self._check_composite_evidence(repeated=False)

    def test_distinct_assets_with_identical_bytes_preserve_multiplicity(self) -> None:
        self._check_composite_evidence(repeated=True)

    def _check_composite_evidence(self, *, repeated: bool) -> None:
        from lxml import etree
        from v2_migration_contract_test import build_candidate_v2
        with tempfile.TemporaryDirectory(prefix="assignment-composite-docx-") as temp:
            root = Path(temp)
            profile = build_candidate_v2(load_profile("assignment-en-zh"))
            if repeated:
                source = write_repeated_visual_source(root / "repeated.pdf")
                work = build_work_dir(root, profile, source)
                write_chain_responses(work)
                run("audit_translation.py", work, True)
                _run_script("build_outputs.py", work)
                _run_script("audit_outputs.py", work)
            else:
                run_assignment_chain(root, profile)
                work = root / "work"
            build = read_json(work / "output" / "build-manifest.json")
            if repeated:
                assets = build["assets"]
                repeats = [count for count in Counter(item["sha256"] for item in assets).values() if count > 1]
                self.assertEqual(repeats, [2])
                self.assertEqual(len({item["id"] for item in assets}), len(assets))
                self.assertEqual(len({item["path"] for item in assets}), len(assets))
            markdown = work / "output" / build["markdown"]
            _run_pipeline_stage("docx", work, "--markdown", str(markdown))
            docx = markdown.with_suffix(".docx")
            original = docx.read_bytes()
            audit = read_json(work / "output" / "docx-audit.json")
            self.assertEqual(audit["role_occurrence_evidence"]["math-with-text"],
                             {"textual": 0, "visual": 2 if repeated else 1, "omitted": 0})
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                  "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            for mutation in ("remove-target", "remove-image", "duplicate-image", "replace-image"):
                with self.subTest(mutation=mutation):
                    with zipfile.ZipFile(io.BytesIO(original)) as archive:
                        entries = {name: archive.read(name) for name in archive.namelist()}
                    xml = etree.fromstring(entries["word/document.xml"])
                    target = next(t for t in xml.xpath("//w:t", namespaces=ns) if "其中" in (t.text or ""))
                    # The math-with-text crop immediately precedes its translated paragraph.
                    paragraph = target
                    while paragraph.tag != "{" + ns["w"] + "}p":
                        paragraph = paragraph.getparent()
                    previous = paragraph.getprevious()
                    while previous is not None and not previous.xpath(".//w:drawing", namespaces=ns):
                        previous = previous.getprevious()
                    self.assertIsNotNone(previous)
                    drawing = previous.xpath(".//w:drawing", namespaces=ns)[0]
                    if mutation == "remove-target":
                        target.text = ""
                    elif mutation == "remove-image":
                        drawing.getparent().remove(drawing)
                    elif mutation == "duplicate-image":
                        drawing.getparent().append(copy.deepcopy(drawing))
                    else:
                        for name in entries:
                            if name.startswith("word/media/"):
                                entries[name] = b"unrelated visual bytes"
                    entries["word/document.xml"] = etree.tostring(xml)
                    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as archive:
                        for name, payload in entries.items():
                            archive.writestr(name, payload)
                    result = subprocess.run(
                        [sys.executable, "-B", str(SCRIPTS / "audit_docx.py"), str(docx),
                         "--work-dir", str(work)], capture_output=True, text=True, env=_subprocess_env(),
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    failed = json.loads(result.stdout)
                    self.assertFalse(failed["checks"]["every_role_occurrence_is_evidenced"])


class SyntheticTranslationAdversarialTests(unittest.TestCase):
    def test_missing_repeated_or_end_appended_placeholder_is_rejected(self) -> None:
        request = {"id": "x", "protected_tokens": [
            {"placeholder": "⟦K001⟧"}, {"placeholder": "⟦K002⟧"},
        ]}
        for translation in (
            "中文 ⟦K001⟧",
            "中文 ⟦K001⟧ ⟦K001⟧ ⟦K002⟧",
            "中文 ⟦K001⟧ ⟦K002⟧",
        ):
            with self.subTest(translation=translation), self.assertRaises(AssertionError):
                validate_synthetic_placeholder_placement(request, translation)


class PipelineReportTests(unittest.TestCase):
    def test_final_pipeline_status_survives_child_warning_and_multiple_reports(self) -> None:
        output = 'warning\n{"status":"audit"}\n{"status":"final"}\n'
        self.assertEqual(_last_pipeline_json("compile-docx", output), {"status": "final"})


if __name__ == "__main__":
    if "--write-fixture" in sys.argv:
        with tempfile.TemporaryDirectory(prefix="assignment-chain-fixture-") as temp:
            v1 = run_assignment_chain(Path(temp) / "v1")
            from v2_migration_contract_test import build_candidate_v2
            v2 = run_assignment_chain(
                Path(temp) / "v2", build_candidate_v2(load_profile("assignment-en-zh"))
            )
        snapshot = {key: value for key, value in v1.items() if key not in {
            "cross_schema_projection", "expected_schema_changes",
        }}
        SNAPSHOT_FIXTURE.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if v1["cross_schema_projection"] != v2["cross_schema_projection"]:
            raise SystemExit("refusing to freeze a non-equivalent V1/V2 projection")
        PROJECTION_FIXTURE.write_text(
            json.dumps(v1["cross_schema_projection"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {SNAPSHOT_FIXTURE} and {PROJECTION_FIXTURE}")
    else:
        unittest.main()
