# 架构减重路线与施工要求

> 决策日期：2026-09-01  
> 状态：工包 A-C 已合并；工包 D workflow 实施中，live ruleset 待单独授权
> 工包 D 基线：`main@21cf4105b98da106a221b2ace87492d4978bfa53`
> 当前原则：先做行为保持型减重，再讨论兼容层迁移和大模块拆分

## 1. 背景与结论

当前 Skill 的内容完整性设计是有效的：稳定 ID、可恢复翻译批次、独立源文件审计、
扫描页人工门禁、DOCX/PDF 冻结链、字体与逐页视觉复核均直接服务于真实交付质量，
不属于应被删减的复杂度。

需要减重的是围绕这些核心能力逐步叠加的工程外壳：

- `pipeline.py status` 与 `finalize_qa.py` 分别实现了相近的 gate 状态评估；
- `safe_artifacts.py` 已是统一工作产物安全层，但 `profile.py` 仍保留一套独立的
  路径检查、no-follow 读取与原子替换实现；
- EOL、manifest 与 repository release 工具之间存在重复的仓库树遍历、路径规范和
  文件身份检查；
- 首次公开发布前同时维护 Profile schema V1/V2，兼容成本已经扩散到多个运行时模块；
- 每个 PR 都运行完整跨平台文件系统与多路径安装器证明，验证频率高于当前项目阶段
  的实际需要；
- 若干审计与适配器脚本已经同时承担多种变化原因，后续修改和审阅成本偏高。

因此本项目采用三阶段减重，而不是推倒重写：

1. **阶段一：行为保持型去重。** 统一 gate evaluator、统一文件系统 helper、分层 CI；
2. **阶段二：首个公开 tag 前统一 Profile schema。** 将 assignment Profile 迁移到 V2，
   旧 V1 仅保留为一次性迁移输入和回归夹具；
3. **阶段三：按职责拆分巨型模块。** 优先拆 MinerU、source audit、DOCX audit 与
   output audit，不引入新的通用插件框架。

阶段一是当前唯一获准施工范围。阶段二和阶段三需要独立工单、独立审查和明确授权。

## 2. 不可削弱的产品不变量

任何“简化”“重构”或“减少门禁”的改动都必须保留以下行为：

1. 模型只能填写带稳定 ID 和 `source_sha256` 的翻译响应，不能直接生成整份
   DOCX、LaTeX 或最终 PDF。
2. 源 PDF、Profile、adapter evidence、document IR、翻译计划、输出、DOCX、PDF、
   visual review 与最终 QA 的冻结绑定不得变弱。
3. PyMuPDF/adapter 结果不能独自证明完整性；原生文本仍需独立 Poppler oracle，
   扫描或严重乱码页仍须停在 `manual_source_review_required`。
4. 不得静默启用 OCR、自动安装或执行 MinerU、下载模型、改用其他 renderer，或在
   缺失依赖时伪造通过结果。
5. DOCX 重建或 PDF 重新转换后，旧 visual review 必须失效；人工视觉批准仍绑定
   精确 PDF 字节。
6. 中文可提取性不能替代字体核验；A4、真实 CJK 字体解析与嵌入、逐页 PNG 完整
   解码、空白页和内容计数门禁必须保留。
7. 不得放宽外链、占位符、公式、代码、图表、语义 role inventory 或 source/output
   disposition 的完整性要求。
8. 私有课程 PDF、译文和真实成品不得进入公开仓库；公开 CI 继续使用可重生成的
   合成夹具。
9. 任何旧的 `passed` 报告在上游字节变化后都必须被判为 stale 或先行失效，不能因
   重构减少复核。
10. 发布、打 tag、创建或发布 Release、替换用户已安装 Skill，均不属于普通重构
    PR 的隐含权限。

若某项减重无法证明保持上述不变量，应停止该项改动，而不是用“内部实现已不同”解释
回归。

## 3. 阶段一：行为保持型减重

### 3.1 工包 A：统一 Job/Gate evaluator

新增一个无副作用、纯读取的状态评估模块，例如：

```text
skills/make-bilingual-study-pdf/scripts/job_state.py
```

模块职责：

- 读取当前工作目录及 gate 报告；
- 调用各 gate 已有的 binding validator；
- 计算 `missing / invalid / stale / blocked / passed`；
- 计算当前 deliverables 及其 SHA-256；
- 推导唯一的 `next_action`；
- 返回结构化 `JobState`，但不写文件、不删除旧报告、不执行外部命令。

调用关系必须改为：

- `pipeline.py status` 只格式化并输出 `JobState`；
- `finalize_qa.py` 使用同一个 evaluator 得到当前事实，再将结果冻结成
  `qa-report.json`；
- 其他命令可以调用 evaluator 做前置检查，但不得再实现第二套完整状态机。

验收要求：

- 对同一工作目录，`status` 与 `finalize` 对每个 gate 的状态判断完全一致；
- 缺字段、旧哈希、变更 deliverable、缺失 DOCX、旧 visual review、schema V1/V2
  等现有分支均有回归；
- evaluator 不相信最终 QA 中记录的 `passed` 字样，仍从当前上游字节重新验证；
- `finalize_qa.py` 的写入、旧 QA 失效和失败退出语义保持不变。

### 3.2 工包 B：统一运行时工作产物安全层

`safe_artifacts.py` 继续作为工作目录内文件系统操作的唯一通用实现。将
`profile.py` 中重复的目录身份遍历、no-follow 文件读取、hard-link/reparse 检查、
临时文件发布和原子替换迁移到该层。

允许保留在 `profile.py` 的内容仅限：

- Profile schema 与语义 contract 校验；
- canonical hash；
- Profile 选择、绑定策略与迁移规则；
- 面向调用者的 Profile 专用错误上下文。

不得为了“统一”而降低现有安全行为。重构后仍须覆盖：

- POSIX symlink 与 FIFO；
- Windows junction/reparse point 与 hard link；
- case-only、NFC/NFD 物理别名；
- 目标在检查与发布之间变化；
- 失败写入或替换保留原文件，并清理本次拥有的临时文件；
- `--force` 只能替换安全的本地普通文件，不能触及外部 inode。

### 3.3 工包 C：统一仓库维护工具的 payload helper

新增仓库级私有 helper，例如：

```text
tools/_payload_fs.py
```

供以下工具复用：

- `tools/check_skill_eol.py`；
- `tools/build_release_manifest.py`；
- `tools/repository_release_check.py` 中适用的树遍历与路径验证部分。

统一内容包括：

- 从文件系统 anchor 到 repository、`skills/` 与 Skill root 的目录链验证；
- 不跟随 symlink/reparse point 的树遍历；
- portable path、Windows 保留名、NFC/casefold 冲突检查；
- descriptor-based 稳定读取与 SHA-256；
- 同目录临时文件和原子替换。

`scripts/release_check.py` 必须继续保持标准库-only、自包含、可在独立安装目录运行，
因为它承担对安装包本身的 bootstrap 校验。它可以保留一套最小独立实现，但不得继续
扩张成第二个通用运行时框架。

验收要求：

- EOL 与 manifest 工具对同一不安全路径给出一致的拒绝边界；
- binary passthrough、unknown suffix、CRLF 修复失败、外部目标不变等现有回归全部
  保留；
- install manifest 的路径、字节数、SHA-256 与 tree hash 计算语义不变；
- 修改后相关 Python 总体实现应呈净减少；若新增代码超过删除代码，PR 必须解释
  新增的不可替代职责。

### 3.4 工包 D：CI 分层与 ruleset 迁移

CI 分层的目标是降低验证频率，不是删除 release 证据。

建议层级：

| 层级 | 触发 | 必须覆盖 |
| --- | --- | --- |
| PR 快车 | 每个 pull request | workflow lint、metadata/manifest、核心单测、Linux 合成前向、一个干净安装冒烟 |
| main 完整车 | push 到 `main` | 全功能矩阵、Windows 文件系统、完整 schema V2 前向、至少一个真实 installer parity |
| 定时/手动安全车 | schedule 或 workflow_dispatch | macOS APFS alias、四安装路径、认证失败回退、完整故障注入矩阵 |
| release candidate | 显式授权 | 精确 SHA、完整安装与 payload 校验、tag/Release 前后状态检查 |

实施约束：

- 当前 branch/tag ruleset 依赖既有 check context；工作流与 ruleset 必须分两步迁移，
  不能先删 job 导致 `main` 永久等待不存在的 required check；
- ruleset 变更是仓库设置写操作，必须单独列出旧值、新值和回滚方式，并获得明确授权；
- release candidate 仍需绑定同一 SHA 已通过的主分支证据；
- macOS、四安装路径和认证失败 fallback 可以降低到定时/手动频率，但在发布候选前
  必须有新鲜成功证据；
- 不得通过把重测试标记为永远 skipped 来伪造分层完成。

阶段一代码 PR 可以先完成 evaluator/helper 去重；CI/ruleset 迁移允许作为同一 Issue
下的后续独立 PR，避免代码重构和仓库设置同时变化。

工包 D 的实现边界如下：

- `tools/run_test_suite.py` 是测试命令与 suite 成员关系的唯一声明处；workflow 和
  development 文档只选择 suite，不再复制完整 Python 命令清单；
- `.github/workflows/baseline.yml` 同时承载 PR 快车与 `main` 完整车，并在迁移期继续
  真实执行六个 live required context；叶子证据 job 无 job-level `if`，聚合 job 则是
  唯一使用 `always()` 的例外，并把任何非 `success` 依赖显式转成失败；
- `.github/workflows/safety.yml` 独占 APFS、四安装路径、认证失败 fallback 和完整故障
  注入，且只在当前 default-branch head 上产生成功的 `safety` 证据；
- `.github/workflows/release.yml` 除精确 SHA 的 `main-full` 证据外，还要求同一 SHA 在
  168 小时内通过 `safety`，再进入任何 tag/Release 写操作；
- `tools/check_workflow_contract.py` 是 workflow 静态契约的唯一实现，敌对回归覆盖删除
  context、叶子条件 skip、聚合缺少 `always()`/依赖结果、缺安全车证据和文档命令
  漂移；`tools/check_job_results.py` 唯一负责严格的 all-success 结果判定；
- live ruleset 本 PR 不写入。精确旧值、新值、迁移顺序、验证和回滚统一记录在
  [`ruleset-migration-plan.md`](ruleset-migration-plan.md)。只有 ruleset 获得另行授权并
  验证后，后续清理 PR 才能移除六个过渡 context 以及 PR 上的临时 Windows/macOS
  兼容成本。

### 3.5 工包 E：文档与历史边界

- `README.md` 只描述当前能力、当前安装方式和当前工程路线，不继续堆叠历史验收细节；
- `docs/development.md` 记录当前有效命令、CI 层级和发布边界；
- `.github/PROJECT_HANDOFF.md` 与 `.github/V2.3_ACCEPTANCE.md` 视为历史实施/验收证据，
  不作为未来架构的唯一规范；
- 完成阶段一后，在本文件补充最终模块边界、迁移结果、测试证据和未完成项；
- 不改写或删除历史验收事实，只在必要时增加 superseded/current 指针。

## 4. 阶段一明确不做

本轮工单不得包含：

- 将 `assignment-en-zh` 从 schema V1 迁移到 V2；
- 删除 V1 兼容分支、改变旧工作目录迁移语义；
- 新增 parser、renderer、语言对或 Profile；
- 重写 MinerU adapter 的内容映射；
- 拆分所有巨型模块；
- 改变任何 Profile JSON 字节或 semantic role/style/output contract；
- 改变 Markdown、DOCX、PDF 的版式与可见输出；
- 修改运行时依赖、安装命令、Skill 名称或 VERSION；
- 创建 tag、Draft Release、公开 Release，或更新已安装用户 Skill；
- 以减少 CI 消耗为理由跳过 release candidate 的完整校验。

这些事项必须另开工单，防止“去重”与行为迁移混为一体。

## 5. 推荐施工顺序

为降低审阅风险，阶段一按以下原子 PR 顺序实施：

1. **Job evaluator。** 先统一 `status/finalize`，不触碰文件系统 helper 和 workflow；
2. **Runtime artifact helper。** 迁移 Profile binding，保持所有平台回归；
3. **Repository payload helper。** 去重 EOL/manifest/repository tools；
4. **CI tiering。** 先改 workflow 并保留 required contexts，再单独申请 ruleset 迁移；
5. **Closeout。** 更新路线文档、实际测试矩阵、已删除重复代码与剩余技术债。

每个 PR 必须：

- 以当前 `main` 为基线；
- 明确列出行为不变量和非目标；
- 提供改前/改后状态判定或产物哈希对照；
- 不顺手修改无关格式、Profile、样式或发布元数据；
- 未通过 required CI 时保持 Draft；
- 不自行合并、打 tag、创建 Release 或更改 ruleset。

## 6. 阶段一完成定义

只有同时满足以下条件，才可关闭阶段一工单：

1. `pipeline status` 与 `finalize_qa` 使用同一 Job/Gate evaluator；
2. `profile.py` 不再维护一套独立的通用文件系统安全实现；
3. EOL 与 manifest 工具复用同一仓库级 payload helper；
4. `release_check.py` 仍可在独立安装目录、仅用标准库完成严格校验；
5. 现有 Profile、IR、翻译、MD、DOCX、PDF、visual review 与 QA 语义零回归；
6. 所有现有安全回归继续通过，外部 link/hardlink/reparse/FIFO 目标保持字节不变；
7. CI 已分层，且 branch/tag ruleset 没有悬空 required check；
8. 发布候选仍能生成等价或更强的完整证据；
9. 目标模块中的重复实现和总代码量有可核验下降；
10. README、development、当前路线和 Issue/PR 交接相互一致。

若 CI 分层因 ruleset 授权尚未执行，可以将代码去重部分标记完成，但阶段一总工单仍
保持 open，并明确记录唯一剩余阻塞项。

## 7. 后续阶段门槛

### 阶段二：V1 → V2

仅在以下条件具备后开工：

- 阶段一稳定合并；
- 首个公开 tag 尚未创建，或已制定明确的兼容版本策略；
- 可运行私有 CS336 作业回归；
- 一次性迁移器、旧工作目录 fixture 和回滚方案已设计；
- assignment Profile 的 V2 表达能够证明与现有成品语义等价。

### 阶段三：巨型模块拆分

按真实职责拆分，不预建通用插件系统。优先顺序：

1. `adapters/mineru.py`；
2. `audit_source.py`；
3. `audit_docx.py`；
4. `audit_outputs.py`。

拆分验收以 import 方向、职责边界、测试定位和变更影响范围改善为准，不以文件数量
增加为目标。
