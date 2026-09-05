# 私有 CS336 回归本地验收协议（工包 A）

> 私有课程 PDF、译文和真实成品不得进入公开仓库。本文只定义**本地执行协议**与**可提交的
> 摘要/哈希/计数证据格式**；执行在维护者本地进行，仓库中只提交去隐私的摘要结果。

## 1. 输入与安全边界

- 输入：私有 CS336 作业 PDF 及其本地工作目录。
- 禁止：提交 PDF、译文、成品、内部路径、用户名、机器名、token 或任何可重识别信息。
- 输出：仅提交以下第 3 节的摘要 JSON/文本，且人工确认不含绝对路径或秘密。

## 2. 执行协议（本地）

从仓库根，用与 CI 相同的 Python 与固定 validator：

```bash
# Windows
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# 复用 CI 的 canonical suite（Windows 用 python -B）
.\.venv\Scripts\python.exe -B tools/run_test_suite.py full \
  --upstream-validator "<OPENAI_SKILLS_CHECKOUT>/skills/.system/skill-creator/scripts/quick_validate.py"

# 对私有 CS336 作业执行一次完整前向（V1 当前行为）
.\.venv\Scripts\python.exe skills/make-bilingual-study-pdf/scripts/pipeline.py \
  source "<PRIVATE_DIR>/cs336.pdf" --work-dir "<PRIVATE_WORK>" --profile assignment-en-zh
# 后续按 pipeline 提示执行 source-audit / ir / prepare / 翻译 / build / docx / compile-docx / finalize
```

工包 B 提供生产命令后，必须执行两条独立路径；当前工包 A 不在私有原件上实施迁移。

### 2.1 现存 V1 WORK 的安全副本迁移

1. 保留原 WORK，创建不含 symlink/hardlink 的独立安全副本；所有备份置于 active WORK/Skills
   之外。记录三份上游 manifest/Profile/IR 与下游 gate 的 before hash 和状态。
2. 对副本执行 `pipeline.py migrate-profile COPY --dry-run` 两次，核对稳定 JSON，
   完整树 hash/目录清单不变，backup 尚未创建。报告必须包含 manifest binding 的字段差异。
3. 实际执行 `pipeline.py migrate-profile COPY`，记录正式发布顺序、失效集合与三份 after hash；
   校验 manifest Profile binding、IR Profile binding 与 IR manifest hash 三方一致。
4. 确認 source/translation/output/DOCX/compile/visual/QA 旧证据失效，再按 next_action 重建
   source audit 及后续链。只更新报告 hash 不是重新审计。
5. 在独立 disposable 副本上验证各中断点与 source-binding 回滚：恢复原 manifest/Profile/IR
   精确字节，旧下游不复活，source audit 可重新构建。不得在唯一原 WORK 上注入故障。

### 2.2 Fresh V2 的独立完整前向

另外从同一私有 PDF 新建 fresh V2 WORK，运行完整 source→translation→DOCX→compile。
分别将 migrated V2 与 fresh V2 同原 V1 的规范化 Markdown、DOCX 文本/结构及固定工具链
逐页 raster 比较。此路径证明可见等价，不能代替 2.1 的原地迁移、零写入和回滚验收。
人工 visual/final QA 状态如实记录，不得由自动化伪造。

## 3. 可提交证据格式（去隐私摘要）

提交一份不含路径的摘要，例如：

```json
{
  "profile": "assignment-en-zh",
  "profile_before": {"schema_version": 1, "canonical_sha256": "<v1 hash>"},
  "profile_after": {"schema_version": 2, "canonical_sha256": "<v2 hash>"},
  "migration": {
    "dry_run_zero_write": true,
    "manifest_before_sha256": "<v1 manifest hash>",
    "manifest_after_sha256": "<v2 manifest hash>",
    "manifest_changed_fields": ["profile"],
    "upstream_bindings_consistent": true,
    "publish_order": ["manifest.json", "profile.json", "document-ir.json"],
    "invalidated_gates": ["source", "translation", "output", "docx", "compile", "visual", "qa"],
    "interruption_checks": "<per-stage outcome without paths>",
    "rollback_three_files_equal": true,
    "source_audit_rebuilt_after_migration_and_rollback": true
  },
  "fresh_vs_migrated_visible_equal": true,
  "document_ir_before_sha256": "<v1 ir hash>",
  "document_ir_after_sha256": "<v2 ir hash>",
  "source_page_count": 90,
  "node_count": 1024,
  "role_counts": {"problem": 38, "example": 8, "tip": 8},
  "disposition_counts": {"bilingual": 797},
  "problem_ids_sha256": "<sha256 of sorted problem id list>",
  "normalized_markdown_sha256": "<visible bilingual markdown hash>",
  "docx_text_structure_sha256": "<extracted DOCX text and structure hash>",
  "pdf_render_sha256": "<fixed-toolchain rendered-page evidence hash>",
  "byte_hashes_if_deterministic": {"docx": "<optional>", "pdf": "<optional>"},
  "final_gate": "passed"
}
```

字段含义：

- 计数类（`page_count`/`node_count`/`role_counts`/`disposition_counts`）证明结构与
  语义等价；
- `profile_before`/`profile_after` 与 IR hash 记录迁移**预期变化**；它们不属于可见等价
  断言；
- `normalized_markdown_sha256`、DOCX 提取的文本/结构 hash 与固定工具链逐页 PDF 渲染
  证据证明用户可见等价。只有维护者已证明某个 DOCX/PDF 容器产物确定性时，才可把原始
  字节 hash 放进 `byte_hashes_if_deterministic` 并要求相等；
- `final_gate` 证明 QA 强度未退化。

## 4. 与公开合成的分工

- 公开 assignment 合成链的等价证据由
  [`v2_assignment_chain_diff_test.py`](../tests/v2_assignment_chain_diff_test.py) 提供；
- 私有 CS336 只作为本地补充验收，其摘要与公开夹具的结论一起写入 PR 描述，私有字节
  与路径绝不入库。
