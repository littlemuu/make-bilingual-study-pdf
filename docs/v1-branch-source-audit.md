# 普通运行时 V1 分支 source audit（工包 A）

本清单是阶段二 B/C 的删除边界，而非人工维护的行号笔记。可执行真值在
[`tests/v2_migration_contract_test.py`](../tests/v2_migration_contract_test.py) 的
`V1_RUNTIME_ALLOWLIST`：测试使用 AST 扫描整个普通运行时 `scripts/` 目录中的 V1
函数、`V1_` 符号、Profile contract 表、全部 `schema_version` /
`source_schema_version` 比较（包括 `== 2`），以及 `schema_v2` / `requires_docx` 派生布尔。
这会覆盖 `job_state.py` 的状态推导和 `pipeline.py` 的 DOCX/audit/compile 分派；新增未登记
分支、allowlist 项已消失，都会 fail closed。清单是显式 Counter：同一表达式的每个 AST 实例均计数，pipeline 三处分派计为 3；新增第四处或删去一处也会失败。

工包 C 完成后，普通 `validate_profile`、Profile contract、IR、输出、DOCX、compile 和
status 路径不得再保留这些条目；只允许一次性 migrator 与历史 fixture 测试读取 V1。

## 当前职责清单

| 文件 | 当前 V1 职责 | 工包 C 目标 |
| --- | --- | --- |
| `profile.py` | `_validate_v1`、V1 contract normalization、V1 semantic match/group | 仅给 migrator/fixture 保留解析，不进入普通 load/validate |
| `document_ir.py` | `_build_document_ir_v1` 与 V1 分派 | 删除生产 V1 IR builder |
| `prepare_translation.py` / `build_outputs.py` / `audit_outputs.py` | V1 implicit prose/output policy 与 `source_schema_version` fallback | 只消费 V2 semantic contract |
| `build_docx.py` / `audit_docx.py` / `docx_ast.py` | V1 Problem 双半区、expected-problems、V1 audit checks | 只保留 V2 role inventory 和迁移调用图所需最小读法 |
| `compile_docx_pdf.py` / `job_state.py` / `pipeline.py` | V1 compile/status/CLI 分派 | 普通命令只接受 V2 WORK |
| `release_check.py` | `assignment-en-zh` schema 1 contract | 验证全部安装 Profile 为 schema 2 |
| `audit_source.py` / `audit_translation.py` / `translation_utils.py` | freeze-chain 内遗留 schema-1 metadata 读取 | 仅保留与非-Profile 产物 schema 有关的项；不得伪装为 Profile V1 兼容 |

`V1_RUNTIME_ALLOWLIST` 也记录以上最后一类 schema-1 metadata 分支，避免工包 C 删除
Profile V1 路径时误删仍被独立产物格式使用的校验。审计不把 `Question`、`Exercise`、
`Task`、`Part`、`Hint` 或 `Note` 当作迁移目标；当前 assignment 行为仍只冻结
`problem`、`example`、`tip`，并只按既有 native kind 显式化 heading/list/prose/caption/
math-with-text/code/math/image/artifact/caption-continuation/visual-content 的 V1 output policy。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -B tests\v2_migration_contract_test.py
```

该命令包含真实源码、漏项和新增分支三种回归。它不依赖源行号，因此重排或格式化不会让
审计失效。
