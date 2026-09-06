# 普通运行时 V1 分支 source audit（工包 A）

本清单保留阶段二 B/C 的职责与删除边界。2026-09-06 测试减重复审取消了
`V1_RUNTIME_ALLOWLIST` 及全源码 AST Counter 门禁：函数名、条件表达式和出现次数
不再作为产品合同，等价重构无需更新一份源码快照。

[`v2_migration_contract_test.py`](../tests/v2_migration_contract_test.py) 继续冻结历史
Profile、完整 contract、语义匹配及 V1/V2 行为；实际转换链继续验证内容与成品投影。
B/C 审阅仍须检查下表对应的调用路径。工包 C 用真实 load/validate/CLI 行为证明普通
入口拒绝 V1、接受 V2；V1 仅能作为迁移输入或历史 fixture，不能仅凭删除某个函数名
宣称收尾完成。测试调整计划见
[`lightweight-core-roadmap.md`](lightweight-core-roadmap.md#2026-09-06-复审先减少测试维护负担)。

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

清理 Profile V1 时，仍须区分独立产物格式的 schema 1（如 glossary、manifest 和
adapter evidence）；不得因为版本号相同就删除它们的校验。当前迁移不新增
`Question`、`Exercise`、`Task` 等选择器，原生内容域的输出等价要求保持不变。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -B tests\v2_migration_contract_test.py
```

该命令验证历史输入与候选 V2 行为，不再扫描或固定普通运行时的源码表达式。
