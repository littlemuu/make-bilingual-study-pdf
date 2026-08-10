#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from common import read_json, read_jsonl, sha256_file, sha256_text, write_json, write_jsonl
from translation_utils import protect_source, restore_placeholders


SCRIPT_DIR = Path(__file__).resolve().parent


def run(script: str, work_dir: Path, expect_success: bool) -> dict:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), str(work_dir)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if (completed.returncode == 0) != expect_success:
        raise AssertionError(
            f"{script} returned {completed.returncode}, expected success={expect_success}\n"
            f"{completed.stdout}"
        )
    return read_json(work_dir / "translation" / "translation-audit.json")


def write_responses(work_dir: Path, mutate=None) -> None:
    translation_dir = work_dir / "translation"
    requests = read_jsonl(translation_dir / "requests" / "part-0001.jsonl")
    responses = []
    for request in requests:
        placeholders = " ".join(
            item["placeholder"] for item in request["protected_tokens"]
        )
        prefix = (
            "这是完整的翻译结果测试译文。"
            if request["id"] == "p001-b002"
            else "这是完整的测试译文。"
        )
        translation = f"{prefix}{placeholders}".strip()
        responses.append(
            {
                "id": request["id"],
                "source_sha256": request["source_sha256"],
                "translation": translation,
            }
        )
    if mutate:
        mutate(responses, requests)
    write_jsonl(translation_dir / "responses" / "part-0001.jsonl", responses)


def setup(work_dir: Path) -> None:
    work_dir.mkdir()
    blocks = [
        {
            "id": "p001-b001",
            "page": 1,
            "bbox": [10, 10, 300, 30],
            "source": "Use torch.Tensor with batch_size 32 in this complete example.",
            "kind": "prose",
            "translatable": True,
            "links": [],
            "protected_spans": [],
        },
        {
            "id": "p001-b002",
            "page": 1,
            "bbox": [10, 40, 300, 60],
            "source": (
                "This deliberately longer sentence checks that the translated result remains "
                "complete faithful accurate readable and useful for every careful student."
            ),
            "kind": "prose",
            "translatable": True,
            "links": [],
            "protected_spans": [],
        },
    ]
    for block in blocks:
        block["source_sha256"] = sha256_text(block["source"])
    write_jsonl(work_dir / "blocks.jsonl", blocks)
    write_json(
        work_dir / "manifest.json",
        {"source_sha256": "0" * 64, "problem_ids": [], "external_uris": []},
    )
    write_json(work_dir / "source-audit.json", {"status": "passed"})
    translation_dir = work_dir / "translation"
    translation_dir.mkdir()
    write_json(
        translation_dir / "glossary.json",
        {
            "schema_version": 1,
            "target_language": "zh-CN",
            "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
            "terms": [
                {
                    "source": "translated result",
                    "targets": ["翻译结果"],
                    "case_sensitive": False,
                    "enforce": True,
                    "notes": "self-test",
                }
            ],
        },
    )
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "prepare_translation.py"),
            str(work_dir),
            "--max-source-chars",
            "1000",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> None:
    results = []
    wrapped_url_block = {
        "source": "Read https://example.\ncom/study_fixture for details.",
        "protected_spans": [
            {"start": 5, "end": 21, "role": "code"},
            {"start": 22, "end": 39, "role": "code"},
        ],
    }
    protected_text, protected_tokens = protect_source(wrapped_url_block)
    assert len(protected_tokens) == 1 and protected_tokens[0]["role"] == "url"
    assert protected_tokens[0]["value"] == "https://example.com/study_fixture"
    assert restore_placeholders(protected_text, protected_tokens) == (
        "Read https://example.com/study_fixture for details."
    )
    results.append("wrapped URL becomes one canonical placeholder")

    with tempfile.TemporaryDirectory(prefix="bilingual-skill-self-test-") as temp:
        work_dir = Path(temp) / "work"
        setup(work_dir)

        write_responses(work_dir)
        report = run("audit_translation.py", work_dir, True)
        assert report["status"] == "passed"
        results.append("valid response passes")

        def missing(responses, _requests):
            responses.pop()

        write_responses(work_dir, missing)
        report = run("audit_translation.py", work_dir, False)
        assert report["status"] == "incomplete" and report["missing_ids"]
        results.append("missing response is incomplete")

        def duplicate(responses, _requests):
            responses.append(dict(responses[0]))

        write_responses(work_dir, duplicate)
        report = run("audit_translation.py", work_dir, False)
        assert report["duplicate_ids"]
        results.append("duplicate response fails")

        def stale_hash(responses, _requests):
            responses[0]["source_sha256"] = "f" * 64

        write_responses(work_dir, stale_hash)
        report = run("audit_translation.py", work_dir, False)
        assert report["invalid_source_hash_ids"]
        results.append("stale source hash fails")

        def broken_placeholder(responses, requests):
            placeholder = requests[0]["protected_tokens"][0]["placeholder"]
            responses[0]["translation"] = responses[0]["translation"].replace(
                placeholder, "⟦BROKEN⟧"
            )

        write_responses(work_dir, broken_placeholder)
        report = run("audit_translation.py", work_dir, False)
        assert report["placeholder_failures"]
        results.append("changed placeholder fails")

        def missing_glossary_target(responses, _requests):
            responses[1]["translation"] = "这是完整的中文测试译文。"

        write_responses(work_dir, missing_glossary_target)
        report = run("audit_translation.py", work_dir, False)
        assert report["glossary_failures"]
        results.append("enforced glossary omission fails")

        def source_copy(responses, requests):
            responses[1]["translation"] = "译文：" + requests[1]["source_for_translation"]

        write_responses(work_dir, source_copy)
        report = run("audit_translation.py", work_dir, False)
        assert report["source_copy_ids"]
        results.append("substantially copied English fails")

    print(json.dumps({"status": "passed", "tests": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
