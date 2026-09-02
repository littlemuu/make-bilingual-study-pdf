#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from adapters import get_adapter
from audit_docx import (
    validate_v2_compile_docx_binding,
    validate_v2_docx_audit_binding,
)
from common import json_loads_strict
from job_state import report_status, translation_plan_status
from profile import (
    load_profile,
    load_work_profile,
    profile_contract,
)
from safe_artifacts import (
    ArtifactSafetyError,
    lexical_absolute_path,
    portable_artifact_basename,
    read_artifact_text,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
)
from visual_utils import validate_visual_review_binding


SCRIPT_DIR = Path(__file__).resolve().parent


def _read_json(path: Path, work_dir: Path) -> dict:
    value = json_loads_strict(read_artifact_text(path, boundary=work_dir))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _work_cli_path(value: Path, work_dir: Path, *, label: str) -> Path:
    candidate = lexical_absolute_path(value)
    try:
        candidate.relative_to(work_dir)
    except ValueError as exc:
        raise ArtifactSafetyError(f"{label} must stay inside WORK") from exc
    return candidate


def _portable_basename(value: str, work_dir: Path, *, label: str) -> str:
    """Require one portable filename component for generated deliverables."""
    del work_dir
    return portable_artifact_basename(value, label=label)


def run_script(script: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *arguments],
        check=True,
    )


def role_counts(work_dir: Path) -> dict[str, int]:
    ir = _read_json(work_dir / "document-ir.json", work_dir)
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

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
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
        markdown = _work_cli_path(args.markdown, work_dir, label="Markdown path")
        validate_artifact_file(markdown, boundary=work_dir)
        stem = _portable_basename(
            args.basename or markdown.stem,
            work_dir,
            label="DOCX basename",
        )
        output = work_dir / "output" / f"{stem}.docx"
        counts = role_counts(work_dir)
        resource_path = _work_cli_path(
            args.resource_path or markdown.parent,
            work_dir,
            label="resource path",
        )
        validate_artifact_directory(resource_path, boundary=work_dir)
        command = [
            str(markdown),
            str(output),
            "--resource-path",
            str(resource_path),
            "--profile",
            str(work_dir / "profile.json"),
        ]
        if profile.get("schema_version") == 2:
            if args.build_dir:
                raise SystemExit(
                    "--build-dir is not supported for schema V2; its deterministic "
                    "intermediates are stored under WORK_DIR/output/docx-build"
                )
            for role, count in counts.items():
                command.extend(["--expected-role", f"{role}={count}"])
        else:
            if args.build_dir:
                docx_build_dir = _work_cli_path(
                    args.build_dir, work_dir, label="DOCX build directory"
                )
                validate_artifact_tree(
                    docx_build_dir, work_dir, allow_missing=True
                )
                command.extend(["--build-dir", str(docx_build_dir)])
            command.extend(
                ["--expected-problems", str(counts.get("problem", 0))]
            )
        command.extend(["--work-dir", str(work_dir)])
        if args.title:
            command.extend(["--title", args.title])
        run_script("build_docx.py", *command)
        audit_command = [
            str(output),
            "--profile",
            str(work_dir / "profile.json"),
            "--expected-links",
            str(
                len(
                    _read_json(work_dir / "manifest.json", work_dir).get(
                        "external_uris", []
                    )
                )
            ),
            "--minimum-images",
            str(args.minimum_images),
            "--output",
            str(work_dir / "output" / "docx-audit.json"),
            "--work-dir",
            str(work_dir),
        ]
        if profile.get("schema_version") == 2:
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
        build = _read_json(
            work_dir / "output" / "build-manifest.json", work_dir
        )
        stem = _portable_basename(
            args.basename or Path(build["markdown"]).stem,
            work_dir,
            label="compile basename",
        )
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
            "--work-dir",
            str(work_dir),
        ]
        if profile.get("schema_version") == 2:
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
