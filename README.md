# make-bilingual-study-pdf

这个仓库维护一个可独立安装的 Codex Skill，用于把英文作业、论文和讲义制作成
英文在前、简体中文紧随其后的学习版文档，并保留可审计的完整性证据。

唯一的可安装负载位于
[`skills/make-bilingual-study-pdf/`](skills/make-bilingual-study-pdf/)；仓库根目录不是 Skill
安装路径。发布目标版本由
[`VERSION`](skills/make-bilingual-study-pdf/VERSION) 唯一确定。

## 当前工程路线

2026-09-01 的架构复审确认：内容完整性、冻结绑定、扫描页人工门禁和逐页视觉复核
属于产品核心，不得以“简化”为由削弱。

阶段一（行为保持型减重）已完成：`status`/`finalize` 统一为同一个 gate evaluator，
运行时与仓库维护工具收敛到共享文件系统 helper，CI 拆分为 PR 快车（`pr-fast`）、
`main` 完整车（`main-full`）、定时/手动安全车（`safety`）与显式 release candidate，
live branch/tag ruleset 已迁移到新聚合 context。该阶段不迁移 Profile schema、不改变
任何可见成品、不修改运行时依赖，也不创建 tag 或 Release。

2026-09-03 的产品路线收敛进一步确定：**原生文本 PDF 是默认轻量核心，MinerU 是
显式选择、只导入预生成结果的高级后端。** 近期保留现有 MinerU importer 和回归，但
不扩展版本、backend、云端执行或 OCR 自动通过能力。下一阶段先完成
`assignment-en-zh` 的 V1→V2 等价迁移；再优先建设原生 PDF 适用性预检和通用
作业/handout 能力。巨型模块拆分改为由真实功能改动驱动，不再优先拆 MinerU。

当前后续产品路线见
[`docs/lightweight-core-roadmap.md`](docs/lightweight-core-roadmap.md)；阶段一的架构
决策、实现边界与历史收尾证据保留在
[`docs/architecture-simplification.md`](docs/architecture-simplification.md)。现有
[`.github/PROJECT_HANDOFF.md`](.github/PROJECT_HANDOFF.md) 与
[`.github/V2.3_ACCEPTANCE.md`](.github/V2.3_ACCEPTANCE.md) 继续保留为 V2.3 历史
实施与验收证据；当前命令、CI 层级和发布边界见
[`docs/development.md`](docs/development.md)。

### 阶段一收尾证据

截至 2026-09-03，Issue #4 的实现收尾锚点为 PR #14 的合并提交
`babac48eacad015daf73454fab52deba8d6cc820`。该精确 SHA 的 push Baseline
run `33724559094` 已真实完成并通过 `workflow-lint`、`self-test`、
`windows-filesystem`、`installer-parity`、`automated-forward` 与最终
`main-full` 聚合；`pr-fast` 在 push 事件中按层级设计跳过。

当前 live ruleset 状态为：默认分支只要求 strict `pr-fast`；`refs/tags/v*` 要求
`main-full` 与 `safety`。三个聚合门禁均使用精确冻结的事件条件，并对失败、取消、
跳过或缺失的依赖结果 fail closed。迁移时间线、补偿探针与回滚证据见
[`docs/ruleset-migration-plan.md`](docs/ruleset-migration-plan.md)。阶段一没有创建 tag
或 Release，也没有替换用户已安装的 Skill；后续工作必须另立工单。

## 交付能力

- 完整的中英对照 Markdown；
- 可编辑 DOCX 学习版；
- 与 DOCX 对应的 PDF，或按需保留的 XeLaTeX/PDF 路径；
- 记录源文件、翻译、构建、渲染和人工视觉检查结果的 QA 报告。

脚本负责文档结构与呈现，模型只填写带稳定 ID 的翻译记录，不直接生成整份
DOCX 或 LaTeX 文档。

| Profile | 适用文档 | 当前输入适配器 | 产品定位 |
| --- | --- | --- | --- |
| `assignment-en-zh` | 英文作业与题目集 | 原生文本 PDF | 默认轻量路径；以 CS336 及相似作业为主要基线 |
| `academic-paper-en-zh` | 英文学术论文 | 已冻结的 MinerU 3.x `pipeline` legacy 输出 | 可选高级路径 |
| `lecture-notes-en-zh` | 英文讲义 | 已冻结的 MinerU 3.x `pipeline` legacy 输出 | 可选高级路径 |

上表描述当前真实行为：论文和讲义 Profile 目前仍要求预生成 MinerU 输出；路线更新
本身没有把它们改成原生适配器。MinerU 不属于 Skill 的安装依赖，适配器只消费用户
已经生成的输出，不安装、不运行 MinerU，也不下载模型。扫描或严重乱码页面必须停在
人工源文件审查门禁，不能仅凭解析器输出自动通过。

## 安装或升级

建议始终从精确 tag 安装，而不是从会继续变化的分支安装。以下命令只有在对应
GitHub tag 已由受控发布流程创建后才可用；尚未发布时会正常失败。使用 Codex
自带的 `skill-installer` 时，将 `<SKILL_INSTALLER_DIR>` 替换为本机路径：

```text
python "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py" --repo littlemuu/make-bilingual-study-pdf --path skills/make-bilingual-study-pdf --ref v2.3.0
```

安装器默认写入当前 Codex 的 Skill 目录；只有在明确知道目标位置时才追加
`--dest "<SKILLS_DIR>"`。保留安装器默认的自动下载/稀疏检出策略；需要认证时由它按既定
顺序回退。也可以直接请 Codex 使用上述精确 tag 和子目录执行同一操作；不要把
tag 替换成分支，也不要退回仓库根目录安装。

安装器不会覆盖已有的同名目录。升级前，必须先把旧目录移到所有会被扫描的
**active Skills roots 之外**的可恢复备份位置；仅在原 Skills root 内改名不是有效备份，
仍可能被发现为第二个同名 Skill。检查 `CODEX_HOME/skills`、`~/.codex/skills`、
`~/.agents/skills` 及产品配置的其他 roots；备份目录不得位于任何一个 active root 内。
安装并验证新版本后，再决定是否删除备份；不要把新旧版本混合复制到同一目录。

## 安装后验证与运行环境

先在安装目录运行只依赖 Python 标准库的严格负载校验：

```text
python scripts/release_check.py --expected-version 2.3.0
```

功能自检需要 Python 3.11 和 Skill 内 `requirements.txt` 中的依赖。先选择一个完全位于
所有 active Skills roots 之外的 `<VENV_DIR>`；这可避免把环境文件混入 Skill 注册目录
或严格负载。Linux/macOS：

```text
python3.11 -m venv "<VENV_DIR>"
"<VENV_DIR>/bin/python" -m pip install -r requirements.txt
"<VENV_DIR>/bin/python" -m pip check
```

Windows PowerShell：

```text
py -3.11 -m venv "<VENV_DIR>"
& "<VENV_DIR>\Scripts\python.exe" -m pip install -r requirements.txt
& "<VENV_DIR>\Scripts\python.exe" -m pip check
```

`self_test.py` 还要求 Poppler 的 `pdftoppm` 和 `pdftotext` 在 `PATH`。完整
DOCX/PDF 路径另需 Pandoc、LibreOffice Writer（`soffice`）、Fontconfig
（`fc-match`）、`pdffonts` 和受支持的 CJK 字体。XeLaTeX 路径是可选能力，另需
XeLaTeX、`latexmk`、`xeCJK`、`unicode-math` 和 Latin Modern Math。MinerU 本身及模型
不是安装依赖。

准备好依赖后，从 Skill 安装目录运行。Linux/macOS：

```text
"<VENV_DIR>/bin/python" scripts/self_test.py
"<VENV_DIR>/bin/python" scripts/pipeline.py validate-profile assignment-en-zh
"<VENV_DIR>/bin/python" scripts/pipeline.py validate-profile academic-paper-en-zh
"<VENV_DIR>/bin/python" scripts/pipeline.py validate-profile lecture-notes-en-zh
```

Windows PowerShell：

```text
& "<VENV_DIR>\Scripts\python.exe" scripts/self_test.py
& "<VENV_DIR>\Scripts\python.exe" scripts/pipeline.py validate-profile assignment-en-zh
& "<VENV_DIR>\Scripts\python.exe" scripts/pipeline.py validate-profile academic-paper-en-zh
& "<VENV_DIR>\Scripts\python.exe" scripts/pipeline.py validate-profile lecture-notes-en-zh
```

安装或验证 Skill 不会读取、转换任何用户 PDF。使用后若仅产生了 Python 缓存，
可用 `release_check.py --ignore-generated-cache` 复检；发布 CI 始终使用严格模式。

## 使用方式

在支持 Skill 的产品中，可以直接提出类似请求：

> 使用 `$make-bilingual-study-pdf`，把这份英文论文制作成经过完整性检查的中英
> 对照 Markdown、可编辑 DOCX 和配套 PDF。

新任务和恢复任务也可以使用 Profile-aware 入口。Linux/macOS：

```text
"<VENV_DIR>/bin/python" scripts/pipeline.py source SOURCE.pdf --work-dir WORK_DIR --profile assignment-en-zh
"<VENV_DIR>/bin/python" scripts/pipeline.py import-mineru SOURCE.pdf MINERU_OUTPUT_DIR --work-dir WORK_DIR --profile academic-paper-en-zh
"<VENV_DIR>/bin/python" scripts/pipeline.py status WORK_DIR
```

Windows PowerShell：

```text
& "<VENV_DIR>\Scripts\python.exe" scripts/pipeline.py source SOURCE.pdf --work-dir WORK_DIR --profile assignment-en-zh
& "<VENV_DIR>\Scripts\python.exe" scripts/pipeline.py import-mineru SOURCE.pdf MINERU_OUTPUT_DIR --work-dir WORK_DIR --profile academic-paper-en-zh
& "<VENV_DIR>\Scripts\python.exe" scripts/pipeline.py status WORK_DIR
```

完整工作流包含源文件清点与审计、术语表冻结、可恢复翻译批次、确定性构建、
PDF 渲染以及逐页人工视觉复核。只有最终 `output/qa-report.json` 为 `passed` 时，
才能宣称交付完成。完整的代理执行契约见
[`SKILL.md`](skills/make-bilingual-study-pdf/SKILL.md)；实现边界见
[`profile-ir.md`](skills/make-bilingual-study-pdf/references/profile-ir.md)。

## 明确边界

- 不接受加密 PDF，也不追求原版面像素级复刻；
- 不会静默切换到 OCR；
- MinerU VLM、hybrid、office、预发布版、4.x 及无法绑定原始 PDF 的输出会失败关闭；
- 人工视觉审查必须绑定当前 PDF 字节，重建 DOCX 或重新转换 PDF 会使旧审查失效；
- 私有原文、译文和真实文档回归不应提交到公开仓库。

## 仓库开发与发布校验

开发、完整测试矩阵与发布门禁见
[`docs/development.md`](docs/development.md)。阶段一架构减重的事实与历史收尾见
[`docs/architecture-simplification.md`](docs/architecture-simplification.md)；阶段一之后
的当前产品路线见
[`docs/lightweight-core-roadmap.md`](docs/lightweight-core-roadmap.md)。V2.3 的历史验收证据保留在
[`.github/V2.3_ACCEPTANCE.md`](.github/V2.3_ACCEPTANCE.md)，不属于安装负载，也不参与后续版本元数据校验。

从仓库根目录运行当前负载的标准库校验：

```text
python skills/make-bilingual-study-pdf/scripts/release_check.py
```
