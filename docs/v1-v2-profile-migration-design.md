# V1→V2 assignment Profile migration design (work package A)

> 状态：设计合同（工包 A）。本文件只冻结迁移器的输入/输出/失效/回滚合同，不实现
> 生产迁移；生产实现属于工包 B。历史 V1 真值见
> [`tests/fixtures/profiles/assignment-en-zh-v1.json`](../tests/fixtures/profiles/assignment-en-zh-v1.json)
> 与 [`assignment-en-zh-v1-contract.json`](../tests/fixtures/profiles/assignment-en-zh-v1-contract.json)。

## 1. 目标与边界

把精确已知的历史 `assignment-en-zh` schema V1 工作目录一次性迁移到 schema V2，普通
运行时仍只维护一套 Profile contract。迁移器只处理“能证明等价”的历史 V1，任何未知、
自定义、变形或第三方 V1 必须失败关闭，不做猜测式转换。

本工包不新增 `Question`/`Exercise`/`Task`/`Part`/`Hint`/`Note` 选择器，不改变可见
样式、字体、标题、页眉页脚或英文在前中文在后的顺序。

公开 assignment 的真实 DOCX/PDF 证据与人工视觉/final QA 协议见
[`public-assignment-chain-review-protocol.md`](public-assignment-chain-review-protocol.md)。
自动化只证明到 contact sheet，并必须在人工视觉门禁前 fail closed；不得在测试中伪造
`visual-review.json` 或 `qa-report.json` 的 passed 状态。

## 2. 严格输入合同

迁移器只接受**同时**满足以下全部条件的绑定 Profile：

1. `id == "assignment-en-zh"`；
2. `schema_version == 1`；
3. `canonical_profile_sha256(profile) ==
   8ce2863ab72adc1ac11f415576060afbbdf39ab7d4f62fc7f25b88b31539c774`；
4. `semantics.groups` 恰好是 `problem`/`example`/`tip` 三个角色，其
   `source_pattern`/`target_pattern`/`style`/`docx_regroup` 与历史 fixture 字节一致；
5. `profile_contract()` 与冻结的
   [`assignment-en-zh-v1-contract.json`](../tests/fixtures/profiles/assignment-en-zh-v1-contract.json)
   逐字段一致。

任一条件不满足即拒绝（fail closed），报告命中的具体差异，绝不尝试“尽量转换”。

## 3. 目标 V2 语义映射

| 角色 | 选择器来源 | style | grouping | output |
| --- | --- | --- | --- | --- |
| `problem` | 当前 V1 `source_pattern`/`target_pattern` | `problem` | `structural-container` | `bilingual` |
| `example` | 当前 V1 两套 pattern | `example` | `none` | `bilingual` |
| `tip` | 当前 V1 两套 pattern | `tip` | `none` | `bilingual` |
| `heading` | `node_types: ["heading"]` | `section-heading` | `none` | `bilingual` |
| `list-item` | `node_types: ["list"]` | `body` | `none` | `bilingual` |
| `paragraph` | `node_types: ["prose"]` | `body` | `none` | `bilingual` |
| `caption` | `node_types: ["caption"]` | `caption` | `none` | `bilingual` |
| `math-with-text` | `node_types: ["math_with_text"]` | `equation` | `none` | `bilingual` |
| `code` | `node_types: ["code"]` | `code` | `none` | `source-only` |
| `math` | `node_types: ["math"]` | `equation` | `none` | `visual-once` |
| `image` | `node_types: ["image"]` | `visual` | `none` | `visual-once` |
| `artifact` | `node_types: ["artifact"]` | `body` | `none` | `artifact-omitted` |
| `caption-continuation` | `node_types: ["caption_continuation"]` | `caption` | `none` | `bilingual` |
| `visual-content` | `node_types: ["visual_content"]` | `visual` | `none` | `visual-once` |

映射由 [`v2_migration_contract_test.py`](../tests/v2_migration_contract_test.py) 的
`CandidateV2DifferentialTests` 冻结。后三类输出恰好展开 V1
`_legacy_output_policy()` 的完整 native classifier 域；它们是隐式 fallback 的显式表达，
不是新的作业标题/选择器能力。`math_with_text` 仍保留一份原公式 visual 与一份译文，
`caption` 仍保留 visual 后的英文/中文图注。最终 kind 域还包含 make_visuals 后处理：
`caption_continuation` 必须通过 `caption_parent` 与唯一 visual 的
`caption_continuation_ids` 绑定，英文与译文均并入父图注，仅保留 grouped marker；
`visual_content` 必须包含在唯一其他 anchor 的 `contained_block_ids` 中，只在该图内出现，
不独立输出文字、图片或 marker。共享 visual 的 visual-once 指内容保留一次，不是给每个
被包含节点再次输出一张图片。缺失或多义关系必须失败，不能当作 artifact 丢弃。
DOCX 对 math_with_text 比较期望资产记录与实际 drawing 的内容 SHA 多重集：
不同 ID/path 可以有相同 SHA；某个 SHA 的实际引用数必须等于该 SHA 的期望资产数。
重复译文也按期望次数核对，不能假设目标字符串全局唯一。每个资产记录仍代表一次
placement；少一个、多一个或错误媒体均失败。不要求截图里的英文成为可搜索文本。
V2-only role inventory、Profile canonical hash
与 IR schema/shape 变化必须单独记录，不能被误称为 V1 合同的字节变化。

## 4. 一次性迁移命令

建议入口 `pipeline.py migrate-profile WORK_DIR [--profile assignment-en-zh]`，
并复用 `safe_artifacts.py` 的 `inspect_artifact_file` / `read_artifact_bytes` /
`atomic_write_bytes` / `recheck_artifact_file` primitive，不建立新的文件系统框架。

### 4.1 `--dry-run` 输出（零写入、确定）

干跑必须输出稳定的 JSON，包含：

- `matched`: 输入合同是否命中（布尔）；
- `reason`: 未命中时的逐项差异；
- `profile_before`: 冻结的 V1 `{id, canonical_sha256, schema_version}`；
- `profile_after`: 目标 V2 `{id, canonical_sha256, schema_version}`；
- `manifest_before_sha256` / `manifest_after_sha256`：精确 manifest 字节 hash；
- `manifest_field_changes`：逐字段 before/after，本合同只改变 `profile: {id, sha256}`，
  保留 source hash、blocks/oracle/visuals/links/adapter evidence 等其他源证据；
- `document_ir_before_sha256` / `document_ir_after_sha256`：精确 IR 字节 hash；
- `publish_order`: `manifest.json` → `profile.json` → `document-ir.json`；
- `invalidate`: 将失效的下游产物清单（见第 6 节）；
- `next_action`: 下一步安全动作。

干跑不写任何文件、不改变任何字节，可重复执行得到相同结果。

### 4.2 实际迁移的中间状态（每步 fail closed）

迁移是两阶段发布。每步失败都停止并把工作目录留在“无假 `passed` 证据”的状态：

1. 读取并校验现存 V1 的 `manifest.json`、`profile.json`、`document-ir.json` 与其源证据。
   对三份正式对象保存 inode/父目录快照和精确字节/hash；验证 manifest Profile binding
   与当前 Profile canonical hash 一致，IR 的 `source.manifest_sha256` 等于原 manifest
   精确字节 hash。回滚备份包含三者，放在所有 active WORK/Skills roots 之外。
2. 在 owned 临时位置先生成目标 Profile，再构造目标 manifest：只更新其 Profile binding，
   其余源证据不变。先序列化目标 manifest，**使用这份精确序列化字节的 SHA** 构造 V2 IR；
   不得用旧 manifest、canonical JSON hash 或发布后重新序列化的另一个版本代替。
   三个临时文件全部写入/复读并验证，正式 WORK 尚未变更。
3. 预检三份固定目标、全部源证据与失效路径，recheck 三份原始对象及所读源字节。
   在第一次正式上游变更前，从 QA 向上失效 visual、compile、DOCX、output、translation、
   source audit，再清理 output/translation 派生产物。任一失效失败都不得开始发布上游。
4. 按 `manifest.json` → `profile.json` → `document-ir.json` 顺序 compare-and-publish。
   每一步之前 recheck 所有尚未发布的上游对象及其原始 hash，使用已验证临时字节和
   `atomic_write_bytes(..., expected=snapshot)` 发布，不重算内容。每个文件的原子写不等于
   三个文件的跨文件原子事务；中间不一致由先行失效保证不会携带假 passed 证据。
5. 复读三份已发布文件，核对目标 hash、manifest/Profile canonical binding、IR/Profile binding
   及 IR `source.manifest_sha256`。三者全部一致才报告完成，下一步为重新 `source-audit`，
   然后重新 prepare/translation/output/DOCX/compile/visual/final QA。

| 中断点 | 正式上游状态 | 门禁与恢复 |
| --- | --- | --- |
| dry-run / 临时准备 | 三份仍为原 V1 | 原 WORK 字节不变；未改变上游的原报告仍有效 |
| 失效途中 | 三份仍为原 V1 | 停止发布；已失效报告不恢复，剩余报告只绑定原 V1 |
| 全部失效后 | 三份仍为原 V1 | 无下游 passed；可执行 source-binding 回滚 |
| manifest 发布后 | V2 manifest、V1 Profile/IR | 明确不完整；不得重建下游，回滚三者 |
| Profile 发布后 | V2 manifest/Profile、V1 IR | 明确不完整；IR hash 不符，无可用 source gate |
| IR 发布后但复读前 | 三份待核验 | 不报告完成；复读失败停止并回滚三者 |
| 复读全部一致 | 三份一致 V2 | source audit 待重建；不能沿用旧通过报告 |

不允许重写旧报告里的 hash 或 schema_version 冒充新的审计。部分迁移不得仅根据 Profile
已经是 V2 而判为 no-op；幂等判定必须确认 manifest/Profile/IR 三者全部一致。

### 4.3 可执行的现存 WORK 合同

`tests/migrate_profile_contract_test.py` 是**工包 A 的测试参考驱动**，不进入安装负载，
不是 `pipeline.py` 新增的生产迁移命令。它在真实 native V1 WORK 的安全副本上执行
`migrate-profile --dry-run` 和实际三文件发布，复用生产 IR builder 与文件安全 primitive。
回归覆盖零写入、每个发布中断点、三方 hash、旧 gate 失效和三文件 source-binding 回滚；
迁移与回滚后都真正重跑生产 source audit。CI forward 另外复制已通过 DOCX/compile 的
V1 WORK，迁移后重建整条 V2 链，并与 fresh V2 的 DOCX/render 比较。

工包 B 才能将此合同接入生产 CLI；届时必须让同一组断言改为调用真实 `pipeline.py
migrate-profile`，不能用 fresh source 重建替代现存 WORK 迁移。

## 5. 幂等与重复迁移

- 目标已是 V2 且 canonical hash 等于目标 V2 时，迁移为幂等 no-op（或显式报告
  “已迁移”）。
- 目标已是 V2 但 hash 不同，明确拒绝，不覆盖。
- 反复运行不得反复破坏已迁移目录。

## 6. 失效范围

迁移后必须使以下全部失效（`stale` 或删除）：

- `source-audit.json`（Profile/IR 绑定的源审计）；
- `translation/` 下的 plan、requests、responses、`translation-audit.json`；
- `output/` 下的 Markdown、LaTeX、build-manifest、`output-audit.json`；
- `output/` 下的 DOCX、`docx-audit.json`；
- `output/` 下的 PDF、`compile-audit.json`、`pdf-renders`、contact 与
  `visual-review.json`；
- `output/qa-report.json`。

失效依据是绑定 hash 变化（Profile/IR 上游变化使下游 stale），而不是删除字节本身。

## 7. 故障恢复与回滚材料位置

- 回滚材料（迁移前的 V1 `manifest.json`、`profile.json` 与 `document-ir.json` 的精确副本）必须放在
  **所有 active WORK/Skills roots 之外**的可恢复位置，不能在 WORK 内留下第二个可被
  误认的正式 Profile。
- 迁移失败时：报告已完成的步骤、保留的原字节位置与“待人工处理”状态；不自动继续。
- 本工包的回滚是**source binding 回滚**：用第 2 节完整 observed V1 对象 + 原
  `manifest.json`、`document-ir.json` 字节按相同发布保护恢复 V1 manifest/Profile/IR，再复读复核；已经失效或删除的 translation、
  output、DOCX、PDF、visual 与 final QA 证据必须重新构建，不能宣称它们会被完整恢复。
  若未来实现需要完整下游恢复，必须先安全备份并验证第 6 节列出的完整集合。

## 8. 平台安全不变量

沿用 `safe_artifacts.py`：symlink、junction/reparse、hardlink、FIFO、并发替换、短写、
fsync/replace 失败和外部 inode 保护不得弱化；迁移不读也不改写任何外部目标字节。
