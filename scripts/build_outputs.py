#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import (
    problem_ids,
    read_json,
    read_jsonl,
    repair_pdf_linebreaks,
    sha256_file,
    write_json,
)


LATEX_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(text: str) -> str:
    return "".join(LATEX_REPLACEMENTS.get(character, character) for character in text)


def latex_url(text: str) -> str:
    return text.replace("\\", "%5C").replace("{", "%7B").replace("}", "%7D")


def prose_text(text: str) -> str:
    return repair_pdf_linebreaks(text)


def markdown_escape(text: str) -> str:
    text = prose_text(text)
    text = text.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]<>#])", r"\\\1", text)


def markdown_quote(text: str) -> str:
    lines = text.strip().splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def heading_level(source: str) -> int:
    match = re.match(r"^(\d+(?:\.\d+)*)\s+", prose_text(source))
    if not match:
        return 2
    return min(4, match.group(1).count(".") + 2)


def should_group_paragraphs(
    previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    if previous["page"] != current["page"]:
        return False
    if previous["kind"] not in {"prose", "list"}:
        return False
    if current["kind"] not in {"prose", "list"}:
        return False
    if not previous.get("translatable") or not current.get("translatable"):
        return False
    if previous.get("caption_parent") or current.get("caption_parent"):
        return False
    previous_box = previous["bbox"]
    current_box = current["bbox"]
    vertical_gap = current_box[1] - previous_box[3]
    if vertical_gap < -2 or vertical_gap > 10:
        return False
    if abs(previous_box[0] - current_box[0]) > 44:
        return False
    previous_text = prose_text(previous["source"])
    current_text = prose_text(current["source"])
    if len(previous_text) + len(current_text) > 1800:
        return False
    incomplete_tail = not re.search(r"[.!?;:。！？；：][\"')\]]?$", previous_text)
    tail_word = re.search(r"([A-Za-z]+)\W*$", previous_text)
    connective_tail = bool(
        tail_word
        and tail_word.group(1).lower()
        in {
            "a",
            "an",
            "and",
            "as",
            "at",
            "by",
            "for",
            "from",
            "in",
            "of",
            "or",
            "the",
            "to",
            "with",
        }
    )
    return incomplete_tail or connective_tail


def latex_heading_command(source: str) -> str:
    level = heading_level(source)
    return {2: "section", 3: "subsection", 4: "subsubsection"}[level]


def response_marker(block: dict[str, Any], mode: str = "segment") -> str:
    return (
        f"<!-- bilingual:{mode} id={block['id']} "
        f"source_sha256={block['source_sha256']} -->"
    )


def visual_markdown(path: str, alt: str) -> str:
    return f"![{markdown_escape(alt)}]({path})"


def visual_latex(path: str) -> str:
    return (
        "\\begin{center}\n"
        f"\\includegraphics[width=0.92\\linewidth,height=0.62\\textheight,keepaspectratio]"
        f"{{{latex_escape(path)}}}\n"
        "\\end{center}"
    )


def make_bilingual_markdown(
    block: dict[str, Any], translation: str, source_override: str | None = None
) -> str:
    source = source_override if source_override is not None else block["source"]
    kind = block["kind"]
    marker = response_marker(block)
    if kind == "heading":
        level = heading_level(source)
        return (
            f"{marker}\n{'#' * level} {markdown_escape(source)}\n\n"
            f"{markdown_quote('**' + markdown_escape(translation) + '**')}"
        )
    if kind == "callout":
        return (
            f"{marker}\n**{markdown_escape(source)}**\n\n"
            f"{markdown_quote('**' + markdown_escape(translation) + '**')}"
        )
    if kind in {"caption", "caption_continuation"}:
        return (
            f"{marker}\n*{markdown_escape(source)}*\n\n"
            f"{markdown_quote('*' + markdown_escape(translation) + '*')}"
        )
    return (
        f"{marker}\n{markdown_escape(source)}\n\n"
        f"{markdown_quote(markdown_escape(translation))}"
    )


def make_bilingual_latex(
    block: dict[str, Any], translation: str, source_override: str | None = None
) -> str:
    source = prose_text(source_override if source_override is not None else block["source"])
    translated = prose_text(translation)
    anchor = f"\\SegmentAnchor{{{latex_escape(block['id'])}}}"
    kind = block["kind"]
    if kind == "heading":
        command = latex_heading_command(source)
        return (
            f"{anchor}\n\\{command}{{{latex_escape(source)}}}\n"
            "\\begin{BilingualTranslation}\n"
            f"\\textbf{{{latex_escape(translated)}}}\n"
            "\\end{BilingualTranslation}"
        )
    if kind == "callout":
        return (
            f"{anchor}\n\\begin{{BilingualCallout}}\n{latex_escape(source)}\n"
            "\\end{BilingualCallout}\n"
            "\\begin{BilingualTranslation}\n"
            f"\\textbf{{{latex_escape(translated)}}}\n"
            "\\end{BilingualTranslation}"
        )
    if kind in {"caption", "caption_continuation"}:
        return (
            f"{anchor}\n\\begin{{center}}\\small\\itshape\n"
            f"{latex_escape(source)}\\par\n"
            f"\\color{{TranslationColor}}{latex_escape(translated)}\n"
            "\\end{center}"
        )
    return (
        f"{anchor}\n{latex_escape(source)}\n\n"
        "\\begin{BilingualTranslation}\n"
        f"{latex_escape(translated)}\n"
        "\\end{BilingualTranslation}"
    )


def make_translation_only_markdown(block: dict[str, Any], translation: str) -> str:
    return f"{response_marker(block)}\n{markdown_quote(markdown_escape(translation))}"


def make_translation_only_latex(block: dict[str, Any], translation: str) -> str:
    return (
        f"\\SegmentAnchor{{{latex_escape(block['id'])}}}\n"
        "\\begin{BilingualTranslation}\n"
        f"{latex_escape(prose_text(translation))}\n"
        "\\end{BilingualTranslation}"
    )


def copy_visuals(
    work_dir: Path, output_dir: Path, visuals: list[dict[str, Any]]
) -> tuple[dict[str, str], list[dict[str, str]]]:
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    paths: dict[str, str] = {}
    copied: list[dict[str, str]] = []
    for visual in visuals:
        source = work_dir / visual["path"]
        if not source.is_file():
            raise SystemExit(f"missing required visual: {source}")
        target = assets_dir / source.name
        shutil.copy2(source, target)
        relative = f"assets/{target.name}"
        paths[visual["id"]] = relative
        copied.append(
            {
                "id": visual["id"],
                "path": relative,
                "sha256": sha256_file(target),
            }
        )
    return paths, copied


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministically merge audited translations into Markdown and XeLaTeX."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--basename")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    source_audit_path = work_dir / "source-audit.json"
    translation_audit_path = work_dir / "translation" / "translation-audit.json"
    merged_path = work_dir / "translation" / "translations-merged.jsonl"
    for path in (source_audit_path, translation_audit_path, merged_path):
        if not path.is_file():
            raise SystemExit(f"missing required artifact: {path}")
    if read_json(source_audit_path).get("status") != "passed":
        raise SystemExit("source audit is not passed")
    if read_json(translation_audit_path).get("status") != "passed":
        raise SystemExit("translation audit is not passed")

    manifest = read_json(work_dir / "manifest.json")
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    translations_list = read_jsonl(merged_path)
    translations = {entry["id"]: entry["translation"] for entry in translations_list}
    if len(translations) != len(translations_list):
        raise SystemExit("merged translations contain duplicate IDs")

    default_stem = Path(manifest["source_pdf"]).stem + "_bilingual"
    basename = args.basename or default_stem
    if not re.fullmatch(r"[A-Za-z0-9._-]+", basename):
        raise SystemExit("--basename may contain only letters, digits, dot, underscore, and hyphen")
    output_dir = work_dir / "output"
    output_dir.mkdir(exist_ok=True)
    markdown_path = output_dir / f"{basename}.md"
    latex_path = output_dir / f"{basename}.tex"
    build_manifest_path = output_dir / "build-manifest.json"
    collisions = [path for path in (markdown_path, latex_path, build_manifest_path) if path.exists()]
    if collisions and not args.force:
        raise SystemExit(
            "refusing to overwrite output artifacts; use --force: "
            + ", ".join(path.name for path in collisions)
        )

    visual_paths, copied_visuals = copy_visuals(
        work_dir, output_dir, manifest.get("visuals", [])
    )
    visuals_by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for visual in manifest.get("visuals", []):
        visuals_by_anchor[visual["anchor_id"]].append(visual)
    continuation_ids = {
        continuation
        for visual in manifest.get("visuals", [])
        for continuation in visual.get("caption_continuation_ids", [])
    }
    paragraph_followers: dict[str, list[str]] = {}
    follower_ids: set[str] = set()
    index = 0
    while index < len(blocks):
        leader = blocks[index]
        group = [leader]
        cursor = index + 1
        while cursor < len(blocks) and should_group_paragraphs(
            group[-1], blocks[cursor]
        ):
            candidate = blocks[cursor]
            if (
                group[-1]["id"] in visuals_by_anchor
                or candidate["id"] in visuals_by_anchor
                or candidate["id"] in continuation_ids
            ):
                break
            group.append(candidate)
            cursor += 1
        if len(group) > 1:
            paragraph_followers[leader["id"]] = [item["id"] for item in group[1:]]
            follower_ids.update(item["id"] for item in group[1:])
            index = cursor
        else:
            index += 1
    blocks_by_id = {block["id"]: block for block in blocks}
    links_by_id = {item["id"]: item for item in manifest.get("links", [])}

    markdown_parts = [
        "<!-- Generated by make-bilingual-study-pdf; edit translations upstream, then rebuild. -->",
        f"<!-- source-sha256: {manifest['source_sha256']} -->",
    ]
    latex_parts: list[str] = []
    dispositions: dict[str, str] = {}
    current_page = None
    emitted_external_uris: set[str] = set()

    for block in blocks:
        block_id = block["id"]
        if block["page"] != current_page:
            current_page = block["page"]
            markdown_parts.append(
                f'\n<a id="source-page-{current_page}"></a>\n<!-- source-page: {current_page} -->'
            )
            latex_parts.append(f"\\SourcePage{{{current_page}}}")

        if block["kind"] == "artifact":
            dispositions[block_id] = "artifact_omitted"
            continue
        if block["kind"] == "visual_content":
            dispositions[block_id] = "preserved_inside_visual"
            continue
        if block_id in follower_ids:
            dispositions[block_id] = "bilingual_grouped"
            continue
        if block_id in continuation_ids:
            dispositions[block_id] = "grouped_with_caption"
            continue

        anchored_visuals = visuals_by_anchor.get(block_id, [])
        for visual in anchored_visuals:
            relative = visual_paths[visual["id"]]
            visual_piece = visual_markdown(
                relative, f"Source visual from page {block['page']}"
            )
            if block["kind"] in {"image", "math"}:
                visual_piece = response_marker(block, "visual") + "\n" + visual_piece
            markdown_parts.append(visual_piece)
            latex_parts.append(
                f"\\SegmentAnchor{{{latex_escape(block_id)}}}\n{visual_latex(relative)}"
            )

        if block["kind"] in {"image", "math"}:
            if not anchored_visuals:
                raise SystemExit(f"{block['kind']} block lacks a visual crop: {block_id}")
            dispositions[block_id] = f"{block['kind']}_visual"
            continue
        if block["kind"] == "math_with_text":
            if not anchored_visuals:
                raise SystemExit(f"math-with-text block lacks a visual crop: {block_id}")
            if block_id not in translations:
                raise SystemExit(f"missing audited translation for {block_id}")
            markdown_parts.append(
                make_translation_only_markdown(block, translations[block_id])
            )
            latex_parts.append(
                make_translation_only_latex(block, translations[block_id])
            )
            dispositions[block_id] = "bilingual_math_visual"
            continue
        if block["kind"] == "code":
            markdown_parts.append(
                response_marker(block, "source-only")
                + "\n```text\n"
                + block["source"]
                + "\n```"
            )
            latex_parts.append(
                f"\\SegmentAnchor{{{latex_escape(block_id)}}}\n"
                "\\begin{Verbatim}[fontsize=\\small,breaklines=true,breakanywhere=true]\n"
                + block["source"]
                + "\n\\end{Verbatim}"
            )
            dispositions[block_id] = "source_code_once"
            continue

        if block_id not in translations:
            raise SystemExit(f"missing audited translation for {block_id}")
        source_override = None
        translation = translations[block_id]
        paragraph_group = [
            blocks_by_id[item] for item in paragraph_followers.get(block_id, [])
        ]
        if paragraph_group:
            source_override = " ".join(
                [block["source"]] + [item["source"] for item in paragraph_group]
            )
            translation = " ".join(
                [translation] + [translations[item["id"]] for item in paragraph_group]
            )
            for follower in paragraph_group:
                markdown_parts.append(response_marker(follower, "grouped"))
                latex_parts.append(
                    f"\\SegmentAnchor{{{latex_escape(follower['id'])}}}"
                )
        continuation_blocks: list[dict[str, Any]] = []
        for visual in anchored_visuals:
            for continuation_id in visual.get("caption_continuation_ids", []):
                continuation = blocks_by_id[continuation_id]
                continuation_blocks.append(continuation)
        if continuation_blocks:
            source_override = " ".join(
                [block["source"]] + [item["source"] for item in continuation_blocks]
            )
            translation = " ".join(
                [translation] + [translations[item["id"]] for item in continuation_blocks]
            )
            for continuation in continuation_blocks:
                markdown_parts.append(response_marker(continuation, "grouped"))

        markdown_parts.append(
            make_bilingual_markdown(block, translation, source_override=source_override)
        )
        latex_parts.append(
            make_bilingual_latex(block, translation, source_override=source_override)
        )
        dispositions[block_id] = "bilingual"

        block_uris = []
        for link_id in block.get("links", []):
            link = links_by_id.get(link_id, {})
            uri = link.get("uri")
            if uri:
                block_uris.append(uri)
                emitted_external_uris.add(uri)
        if block_uris:
            markdown_parts.append(
                "\n".join(
                    f"[Source link](<{uri}>)" for uri in sorted(set(block_uris))
                )
            )
            latex_parts.append(
                "\n".join(
                    f"\\url{{{latex_url(uri)}}}" for uri in sorted(set(block_uris))
                )
            )

    unaccounted = sorted(set(blocks_by_id) - set(dispositions))
    if unaccounted:
        raise SystemExit(f"unaccounted source blocks: {unaccounted}")

    external_uris = set(manifest.get("external_uris", []))
    if external_uris:
        markdown_parts.append("\n## Source links / 原文链接")
        latex_parts.append("\\section*{Source links / 原文链接}")
        for uri in sorted(external_uris):
            markdown_parts.append(f"- <{uri}>")
            latex_parts.append(f"\\url{{{latex_url(uri)}}}\\par")
        emitted_external_uris.update(external_uris)

    markdown_text = "\n\n".join(markdown_parts).rstrip() + "\n"
    template_path = Path(__file__).resolve().parent.parent / "assets" / "bilingual-template.tex"
    template = template_path.read_text(encoding="utf-8")
    first_heading = next(
        (prose_text(block["source"]) for block in blocks if block["kind"] == "heading"),
        Path(manifest["source_pdf"]).stem,
    )
    latex_text = (
        template.replace("%%__TITLE__%%", latex_escape(first_heading))
        .replace("%%__SOURCE_HASH__%%", manifest["source_sha256"])
        .replace("%%__BODY__%%", "\n\n".join(latex_parts))
    )
    if "%%__" in latex_text:
        raise SystemExit("unresolved template placeholder")

    markdown_path.write_text(markdown_text, encoding="utf-8", newline="\n")
    latex_path.write_text(latex_text, encoding="utf-8", newline="\n")
    output_problem_ids = sorted(
        set(problem_ids("\n".join(block["source"] for block in blocks)))
    )
    expected_problem_ids = sorted(manifest.get("problem_ids", []))
    if output_problem_ids != expected_problem_ids:
        raise SystemExit("Problem ID inventory changed before output generation")
    if emitted_external_uris != external_uris:
        raise SystemExit("not every external URI was emitted")

    build_manifest = {
        "schema_version": 1,
        "source_pdf_sha256": manifest["source_sha256"],
        "source_manifest_sha256": sha256_file(work_dir / "manifest.json"),
        "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
        "source_audit_sha256": sha256_file(source_audit_path),
        "translation_audit_sha256": sha256_file(translation_audit_path),
        "translations_merged_sha256": sha256_file(merged_path),
        "markdown": markdown_path.name,
        "markdown_sha256": sha256_file(markdown_path),
        "latex": latex_path.name,
        "latex_sha256": sha256_file(latex_path),
        "assets": copied_visuals,
        "block_count": len(blocks),
        "disposition_counts": dict(Counter(dispositions.values())),
        "dispositions": dispositions,
        "problem_ids": expected_problem_ids,
        "external_uris": sorted(external_uris),
    }
    write_json(build_manifest_path, build_manifest)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "markdown": markdown_path.name,
                "latex": latex_path.name,
                "blocks": len(blocks),
                "assets": len(copied_visuals),
                "disposition_counts": build_manifest["disposition_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
