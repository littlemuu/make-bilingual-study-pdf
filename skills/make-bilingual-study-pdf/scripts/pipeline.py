#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from adapters import get_adapter
from audit_docx import (
    validate_v2_compile_docx_binding,
    validate_v2_docx_audit_binding,
)
from common import read_json, read_jsonl, sha256_file
from profile import (
    canonical_profile_sha256,
    load_profile,
    load_work_profile,
    profile_contract,
)


SCRIPT_DIR = Path(__file__).resolve().parent


def translation_plan_status(work_dir: Path, plan_path: Path) -> str:
    try:
        plan = read_json(plan_path)
        profile = load_work_profile(work_dir)
        if plan.get("schema_version") != 2:
            return "invalid"
        if plan.get("profile_id") != profile["id"]:
            return "stale"
        if plan.get("profile_sha256") != canonical_profile_sha256(profile):
            return "stale"

        bound_files = {
            "profile_file_sha256": work_dir / "profile.json",
            "document_ir_sha256": work_dir / "document-ir.json",
            "source_manifest_sha256": work_dir / "manifest.json",
            "source_blocks_sha256": work_dir / "blocks.jsonl",
            "source_audit_sha256": work_dir / "source-audit.json",
            "glossary_sha256": work_dir / "translation" / "glossary.json",
        }
        for field, path in bound_files.items():
            if not path.is_file() or plan.get(field) != sha256_file(path):
                return "stale"

        batches = plan.get("batches")
        if not isinstance(batches, list) or plan.get("batch_count") != len(batches):
            return "invalid"
        request_ids: list[str] = []
        segment_total = 0
        for number, batch in enumerate(batches, start=1):
            if batch.get("part") != number:
                return "invalid"
            request_path = work_dir / "translation" / batch["request_file"]
            if not request_path.is_file():
                return "stale"
            if batch.get("request_sha256") != sha256_file(request_path):
                return "stale"
            entries = read_jsonl(request_path)
            ids = [entry.get("id") for entry in entries]
            if (
                not ids
                or batch.get("segment_count") != len(ids)
                or batch.get("first_id") != ids[0]
                or batch.get("last_id") != ids[-1]
            ):
                return "invalid"
            request_ids.extend(ids)
            segment_total += len(ids)
        if plan.get("expected_segment_count") != segment_total:
            return "invalid"
        if plan.get("expected_ids") != request_ids:
            return "invalid"
        return "passed"
    except (KeyError, TypeError, ValueError, OSError):
        return "invalid"


def run_script(script: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *arguments],
        check=True,
    )


def report_status(work_dir: Path) -> dict:
    work_dir = Path(os.path.abspath(work_dir.expanduser()))
    artifacts = {
        "profile": work_dir / "profile.json",
        "manifest": work_dir / "manifest.json",
        "adapter_evidence": work_dir / "adapter-evidence.json",
        "document_ir": work_dir / "document-ir.json",
        "source_audit": work_dir / "source-audit.json",
        "glossary": work_dir / "translation" / "glossary.json",
        "translation_plan": work_dir / "translation" / "plan.json",
        "translation_audit": work_dir / "translation" / "translation-audit.json",
        "merged_translations": work_dir / "translation" / "translations-merged.jsonl",
        "build_manifest": work_dir / "output" / "build-manifest.json",
        "output_audit": work_dir / "output" / "output-audit.json",
        "docx_audit": work_dir / "output" / "docx-audit.json",
        "compile_audit": work_dir / "output" / "compile-audit.json",
        "visual_review": work_dir / "output" / "visual-review.json",
        "qa_report": work_dir / "output" / "qa-report.json",
    }
    exists = {name: path.is_file() for name, path in artifacts.items()}

    def is_schema_v2() -> bool:
        try:
            profile = read_json(artifacts["profile"]) if exists["profile"] else {}
            ir = read_json(artifacts["document_ir"]) if exists["document_ir"] else {}
            compile_report = (
                read_json(artifacts["compile_audit"])
                if exists["compile_audit"]
                else {}
            )
            return (
                profile.get("schema_version") == 2
                or ir.get("schema_version") == 2
                or "docx_audit_bindings" in compile_report
            )
        except (ValueError, OSError):
            return False

    def status(name: str) -> str | None:
        path = artifacts[name]
        if not path.is_file() or path.suffix != ".json":
            return None
        if name == "translation_plan":
            return translation_plan_status(work_dir, path)
        try:
            report = read_json(path)
            value = report.get("status") or report.get("automated_status")
            if name == "source_audit" and value == "passed":
                bound_inputs = {
                    "source_manifest_sha256": artifacts["manifest"],
                    "source_blocks_sha256": work_dir / "blocks.jsonl",
                    "profile_file_sha256": artifacts["profile"],
                    "document_ir_sha256": artifacts["document_ir"],
                }
                if artifacts["adapter_evidence"].is_file():
                    bound_inputs["adapter_evidence_sha256"] = artifacts[
                        "adapter_evidence"
                    ]
                for field, input_path in bound_inputs.items():
                    expected = report.get(field)
                    if input_path.is_file() and (
                        expected is None or sha256_file(input_path) != expected
                    ):
                        return "stale"
                if report.get("profile_sha256") != canonical_profile_sha256(
                    load_work_profile(work_dir)
                ):
                    return "stale"
            if name == "docx_audit" and value == "passed" and is_schema_v2():
                docx_reference = report.get("docx")
                if not isinstance(docx_reference, str) or not docx_reference:
                    return "invalid"
                # DOCX audit reports may be copied out of CI for human review.
                # Their absolute diagnostic path belongs to the producing host;
                # the frozen identity is the byte hash, so resolve the portable
                # basename inside this work directory's output folder.
                docx_name = docx_reference.replace("\\", "/").rsplit("/", 1)[-1]
                if not docx_name or docx_name in {".", ".."}:
                    return "invalid"
                docx_path = (work_dir / "output" / docx_name).resolve()
                try:
                    docx_path.relative_to((work_dir / "output").resolve())
                except ValueError:
                    return "invalid"
                _, _, binding_errors = validate_v2_docx_audit_binding(
                    work_dir, docx_path, path
                )
                if binding_errors:
                    return "stale"
            if name == "compile_audit" and value == "passed" and is_schema_v2():
                _, binding_errors = validate_v2_compile_docx_binding(
                    work_dir, report, artifacts["docx_audit"]
                )
                if binding_errors:
                    return "stale"
            return value
        except (ValueError, OSError):
            return "invalid"

    profile_id = None
    if artifacts["profile"].is_file():
        try:
            profile_id = load_work_profile(work_dir)["id"]
        except ValueError:
            profile_id = "invalid"

    manifest: dict = {}
    if artifacts["manifest"].is_file():
        try:
            manifest = read_json(artifacts["manifest"])
        except (ValueError, OSError):
            manifest = {}
    adapter = manifest.get("adapter") or {}
    adapter_id = adapter.get("id") or (
        load_work_profile(work_dir)["input"]["adapter"]
        if artifacts["profile"].is_file() and profile_id != "invalid"
        else None
    )

    if not exists["manifest"]:
        next_action = (
            "run `pipeline.py source SOURCE.pdf --work-dir WORK_DIR` for the native adapter, "
            "or `pipeline.py import-mineru SOURCE.pdf MINERU_OUTPUT_DIR --work-dir WORK_DIR "
            "--profile PROFILE` for a frozen MinerU output"
        )
    elif not exists["profile"] or not exists["document_ir"]:
        next_action = "run `pipeline.py ir WORK_DIR` to bind the default profile and create the IR"
    elif not exists["source_audit"]:
        next_action = "run `pipeline.py source-audit WORK_DIR`"
    elif status("source_audit") in {
        "needs_manual_review",
        "manual_source_review_required",
    }:
        next_action = (
            "complete the independent per-page source review; translation preparation and "
            "final QA remain blocked while source evidence needs manual review"
        )
    elif status("source_audit") != "passed":
        next_action = "repair extraction, then rerun `pipeline.py source-audit WORK_DIR`"
    elif not exists["glossary"]:
        next_action = "run `init_glossary.py WORK_DIR`, then review the glossary"
    elif not exists["translation_plan"]:
        next_action = "after reviewing the glossary, run `pipeline.py prepare WORK_DIR`"
    elif status("translation_plan") != "passed":
        next_action = "repair changed plan inputs/requests, then rerun `pipeline.py prepare WORK_DIR`"
    elif status("translation_audit") != "passed":
        next_action = "fill/resume response JSONL files, then run `audit_translation.py WORK_DIR --progress`"
    elif status("output_audit") != "passed":
        next_action = "run `pipeline.py build WORK_DIR`"
    elif status("docx_audit") != "passed":
        next_action = (
            "run `pipeline.py docx WORK_DIR --markdown AUDITED_STRUCTURED.md`; "
            "the Markdown must already contain complete semantic callout containers"
        )
    elif status("compile_audit") != "passed":
        next_action = "run `pipeline.py compile-docx WORK_DIR`"
    elif status("visual_review") != "passed":
        next_action = "inspect every final render and record the visual review"
    elif status("qa_report") != "passed":
        next_action = "run `pipeline.py finalize WORK_DIR`"
    else:
        next_action = "complete"
    return {
        "work_dir": str(work_dir),
        "profile": profile_id,
        "adapter": {
            "id": adapter_id,
            "backend": adapter.get("backend"),
            "version": adapter.get("version"),
            "evidence": adapter.get("evidence"),
            "evidence_sha256": adapter.get("evidence_sha256"),
        },
        "artifacts": exists,
        "gate_statuses": {
            name: status(name)
            for name in (
                "source_audit",
                "translation_plan",
                "translation_audit",
                "output_audit",
                "docx_audit",
                "compile_audit",
                "visual_review",
                "qa_report",
            )
        },
        "next_action": next_action,
    }


def role_counts(work_dir: Path) -> dict[str, int]:
    ir = read_json(work_dir / "document-ir.json")
    inventories = ir.get("inventories", {})
    generic = inventories.get("role_inventory")
    if isinstance(generic, dict):
        return {
            role: int(item.get("occurrence_count", 0))
            for role, item in generic.items()
            if isinstance(item, dict)
        }
    return inventories.get("semantic_role_counts", {})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Profile-aware entry point for deterministic stages. Translation and visual "
            "review remain explicit human/model checkpoints."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-profile")
    validate.add_argument("profile", nargs="?", default="assignment-en-zh")

    source = subparsers.add_parser("source")
    source.add_argument("pdf", type=Path)
    source.add_argument("--work-dir", type=Path, required=True)
    source.add_argument("--profile", default="assignment-en-zh")
    source.add_argument("--render-dpi", type=int, default=120)

    import_mineru = subparsers.add_parser("import-mineru")
    import_mineru.add_argument("pdf", type=Path)
    import_mineru.add_argument("mineru_output_dir", type=Path)
    import_mineru.add_argument("--work-dir", type=Path, required=True)
    import_mineru.add_argument("--profile", required=True)
    import_mineru.add_argument("--render-dpi", type=int, default=120)
    import_mineru.add_argument("--force", action="store_true")

    source_audit = subparsers.add_parser("source-audit")
    source_audit.add_argument("work_dir", type=Path)

    ir = subparsers.add_parser("ir")
    ir.add_argument("work_dir", type=Path)
    ir.add_argument("--profile", default="assignment-en-zh")
    ir.add_argument("--force", action="store_true")

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("work_dir", type=Path)
    prepare.add_argument("--max-source-chars", type=int, default=8000)
    prepare.add_argument("--force", action="store_true")

    build = subparsers.add_parser("build")
    build.add_argument("work_dir", type=Path)
    build.add_argument("--basename")
    build.add_argument("--force", action="store_true")

    docx = subparsers.add_parser("docx")
    docx.add_argument("work_dir", type=Path)
    docx.add_argument("--markdown", type=Path, required=True)
    docx.add_argument("--resource-path", type=Path)
    docx.add_argument("--basename")
    docx.add_argument("--title")
    docx.add_argument("--minimum-images", type=int, default=0)
    docx.add_argument("--build-dir", type=Path)

    compile_docx = subparsers.add_parser("compile-docx")
    compile_docx.add_argument("work_dir", type=Path)
    compile_docx.add_argument("--basename")
    compile_docx.add_argument("--dpi", type=int, default=144)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("work_dir", type=Path)

    status = subparsers.add_parser("status")
    status.add_argument("work_dir", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "validate-profile":
        try:
            profile = load_profile(args.profile)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            json.dumps(
                {
                    "status": "passed",
                    "profile": profile["id"],
                    "adapter": profile["input"]["adapter"],
                    "target_language": profile["translation"]["target_language"],
                    "semantic_roles": [
                        role["role"] for role in profile_contract(profile)["roles"]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "status":
        print(json.dumps(report_status(args.work_dir), ensure_ascii=False, indent=2))
        return

    if args.command == "source":
        profile = load_profile(args.profile)
        script = get_adapter(profile["input"]["adapter"]).script_for("source")
        run_script(
            script,
            str(args.pdf),
            "--work-dir",
            str(args.work_dir),
            "--profile",
            args.profile,
            "--render-dpi",
            str(args.render_dpi),
        )
        run_script("audit_source.py", str(args.work_dir))
        run_script("init_glossary.py", str(args.work_dir))
        print(json.dumps(report_status(args.work_dir), ensure_ascii=False, indent=2))
        return

    if args.command == "import-mineru":
        profile = load_profile(args.profile)
        script = get_adapter(profile["input"]["adapter"]).script_for("import")
        command = [
            str(args.pdf),
            str(args.mineru_output_dir),
            "--work-dir",
            str(args.work_dir),
            "--profile",
            args.profile,
            "--render-dpi",
            str(args.render_dpi),
        ]
        if args.force:
            command.append("--force")
        run_script(script, *command)
        run_script("audit_source.py", str(args.work_dir))
        run_script("init_glossary.py", str(args.work_dir))
        print(json.dumps(report_status(args.work_dir), ensure_ascii=False, indent=2))
        return

    work_dir = Path(os.path.abspath(args.work_dir.expanduser()))
    if args.command == "source-audit":
        run_script("document_ir.py", str(work_dir), "--check")
        run_script("audit_source.py", str(work_dir))
    elif args.command == "ir":
        command = [str(work_dir), "--profile", args.profile]
        if args.force:
            command.append("--force")
        run_script("document_ir.py", *command)
    elif args.command == "prepare":
        command = [str(work_dir), "--max-source-chars", str(args.max_source_chars)]
        if args.force:
            command.append("--force")
        run_script("prepare_translation.py", *command)
    elif args.command == "build":
        run_script("audit_translation.py", str(work_dir))
        command = [str(work_dir)]
        if args.basename:
            command.extend(["--basename", args.basename])
        if args.force:
            command.append("--force")
        run_script("build_outputs.py", *command)
        run_script("audit_outputs.py", str(work_dir))
    elif args.command == "docx":
        profile = load_work_profile(work_dir)
        markdown = args.markdown.expanduser().resolve()
        stem = args.basename or markdown.stem
        output = work_dir / "output" / f"{stem}.docx"
        counts = role_counts(work_dir)
        command = [
            str(markdown),
            str(output),
            "--resource-path",
            str((args.resource_path or markdown.parent).resolve()),
            "--profile",
            str(work_dir / "profile.json"),
        ]
        if profile.get("schema_version") == 2:
            if args.build_dir:
                raise SystemExit(
                    "--build-dir is not supported for schema V2; its deterministic "
                    "intermediates are stored under WORK_DIR/output/docx-build"
                )
            command.extend(["--work-dir", str(work_dir)])
            for role, count in counts.items():
                command.extend(["--expected-role", f"{role}={count}"])
        else:
            command.extend(
                ["--expected-problems", str(counts.get("problem", 0))]
            )
        if args.title:
            command.extend(["--title", args.title])
        if args.build_dir and profile.get("schema_version") != 2:
            command.extend(["--work-dir", str(args.build_dir)])
        run_script("build_docx.py", *command)
        audit_command = [
            str(output),
            "--profile",
            str(work_dir / "profile.json"),
            "--expected-links",
            str(len(read_json(work_dir / "manifest.json").get("external_uris", []))),
            "--minimum-images",
            str(args.minimum_images),
            "--output",
            str(work_dir / "output" / "docx-audit.json"),
        ]
        if profile.get("schema_version") == 2:
            audit_command.extend(["--work-dir", str(work_dir)])
            for role, count in counts.items():
                audit_command.extend(["--expected-role", f"{role}={count}"])
        else:
            audit_command.extend(
                [
                    "--expected-problems",
                    str(counts.get("problem", 0)),
                    "--expected-examples",
                    str(counts.get("example", 0)),
                    "--expected-tips",
                    str(counts.get("tip", 0)),
                ]
            )
        run_script("audit_docx.py", *audit_command)
    elif args.command == "compile-docx":
        profile = load_work_profile(work_dir)
        build = read_json(work_dir / "output" / "build-manifest.json")
        stem = args.basename or Path(build["markdown"]).stem
        counts = role_counts(work_dir)
        command = [
            str(work_dir / "output" / f"{stem}.docx"),
            str(work_dir / "output" / f"{stem}.pdf"),
            "--render-dir",
            str(work_dir / "output" / "pdf-renders"),
            "--audit-output",
            str(work_dir / "output" / "compile-audit.json"),
            "--cjk-font",
            profile["render"]["docx"]["cjk_font"],
            "--dpi",
            str(args.dpi),
        ]
        if profile.get("schema_version") == 2:
            command.extend(["--work-dir", str(work_dir)])
            for role, count in counts.items():
                command.extend(["--expected-role", f"{role}={count}"])
        else:
            command.extend(
                ["--expected-problems", str(counts.get("problem", 0))]
            )
        run_script("compile_docx_pdf.py", *command)
    elif args.command == "finalize":
        run_script("finalize_qa.py", str(work_dir))
    print(json.dumps(report_status(work_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode or 1) from exc
