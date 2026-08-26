# make-bilingual-study-pdf

把英文作业、学术论文和讲义制作成“英文在前、简体中文紧随其后”的学习版文档，
并用可恢复的翻译记录、稳定哈希和独立审计防止静默漏译。

发布目标版本由 [`VERSION`](VERSION) 唯一确定。

## 交付物

- 完整的中英对照 Markdown；
- 可编辑 DOCX 学习版；
- 与 DOCX 对应的 PDF，或按需保留的 XeLaTeX/PDF 路径；
- 记录源文件、翻译、构建、渲染和人工视觉检查结果的 QA 报告。

脚本负责文档结构与呈现，模型只填写带稳定 ID 的翻译记录，不直接生成整份
DOCX 或 LaTeX 文档。

## 支持范围

| Profile | 适用文档 | 输入适配器 |
| --- | --- | --- |
| `assignment-en-zh` | 英文作业与题目集 | 原生文本 PDF |
| `academic-paper-en-zh` | 英文学术论文 | 已冻结的 MinerU 3.x `pipeline` legacy 输出 |
| `lecture-notes-en-zh` | 英文讲义 | 已冻结的 MinerU 3.x `pipeline` legacy 输出 |

MinerU 适配器只消费用户已经生成的输出，不安装、不运行 MinerU，也不下载模型。
扫描或严重乱码页面必须停在人工源文件审查门禁，不能仅凭解析器输出自动通过。

## 安装或升级

建议始终从精确 tag 安装，而不是从会继续变化的分支安装。以下命令只有在对应
GitHub tag 已由受控发布流程创建后才可用；尚未发布时会正常失败。使用 Codex
自带的 `skill-installer` 时，将 `<SKILL_INSTALLER_DIR>` 替换为本机路径：

```text
python "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py" --repo littlemuu/make-bilingual-study-pdf --path skills/make-bilingual-study-pdf --ref v2.3.0
```

安装器默认写入当前 Codex 的 Skill 目录；只有在明确知道目标位置时才追加
`--dest "<SKILLS_DIR>"`。保留默认自动下载/稀疏检出策略，遇到需要认证的仓库时
可由安装器按既定顺序回退。

也可以直接请 Codex 从安装命令中的精确 tag 和子目录执行同一操作。不要把 tag
替换成会继续变化的分支，也不要退回仓库根目录安装。

安装器不会覆盖已有的同名目录。升级前应先把旧目录移到可恢复的备份位置，安装
完成并验证通过后再决定是否删除备份；不要把新旧版本混合复制到同一目录。

安装完成后先在安装目录运行只依赖 Python 标准库的严格负载校验：

```text
python scripts/release_check.py --expected-version 2.3.0
```

功能自检需要 Python 3.11 和 `requirements.txt` 中的依赖。先选择一个完全位于
整个 Skills 根目录之外的 `<VENV_DIR>`；这可避免把环境文件混入 Skill 注册目录
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
DOCX/PDF 路径另需 Pandoc、LibreOffice Writer（`soffice`）、Fontconfig、
`pdffonts` 和受支持的 CJK 字体。XeLaTeX 路径是可选能力，另需 XeLaTeX、
`latexmk`、`xeCJK`、`unicode-math` 和 Latin Modern Math。MinerU 本身及模型不是
安装依赖。

准备好依赖后，从安装目录运行。Linux/macOS：

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

完整的仓库开发与发布测试矩阵见
[`docs/development.md`](https://github.com/littlemuu/make-bilingual-study-pdf/blob/main/docs/development.md)。
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
才能宣称交付完成。

## 明确边界

- 不接受加密 PDF，也不追求原版面像素级复刻；
- 不会静默切换到 OCR；
- MinerU VLM、hybrid、office、预发布版、4.x 及无法绑定原始 PDF 的输出会失败关闭；
- 人工视觉审查必须绑定当前 PDF 字节，重建 DOCX 或重新转换 PDF 会使旧审查失效；
- 私有原文、译文和真实文档回归不应提交到公开仓库。

更详细的操作契约见 [`SKILL.md`](SKILL.md)，V2.3 的实现边界见
[`references/profile-ir.md`](references/profile-ir.md)。历史验收证据保留在仓库的
[`V2.3_ACCEPTANCE.md`](https://github.com/littlemuu/make-bilingual-study-pdf/blob/main/.github/V2.3_ACCEPTANCE.md)，
不属于安装负载或当前版本元数据。

## 发布校验

```text
python scripts/release_check.py
```

该检查只使用 Python 标准库，按 `release-manifest.json` 核对安装目录的完整文件
集合、大小和 SHA-256，同时检查 `VERSION`、README 的完整安装参数、Skill 名称及
三个 Profile。任意缺失、额外或改变的负载文件都会失败；显式 `--tag` 或 GitHub
tag 环境变量也必须精确等于 `v` 加 `VERSION`。
