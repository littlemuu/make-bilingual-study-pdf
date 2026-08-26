# make-bilingual-study-pdf

把英文作业、学术论文和讲义制作成“英文在前、简体中文紧随其后”的学习版文档，
并用可恢复的翻译记录、稳定哈希和独立审计防止静默漏译。

当前稳定版本是 **v2.3.0**；仓库中的 [`VERSION`](VERSION) 是唯一发布版本源。

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

建议始终从精确 tag 安装，而不是从会继续变化的分支安装。使用 Codex 自带的
`skill-installer` 时，将 `<SKILL_INSTALLER_DIR>` 替换为本机路径：

```text
python "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py" --repo littlemuu/make-bilingual-study-pdf --path . --ref v2.3.0 --name make-bilingual-study-pdf
```

安装器默认写入当前 Codex 的 Skill 目录；只有在明确知道目标位置时才追加
`--dest "<SKILLS_DIR>"`。保留默认自动下载/稀疏检出策略，遇到需要认证的仓库时
可由安装器按既定顺序回退。

也可以直接请 Codex：

> 使用 skill-installer，从 `littlemuu/make-bilingual-study-pdf` 的 `v2.3.0`
> tag 安装仓库根目录，技能名为 `make-bilingual-study-pdf`。

安装器不会覆盖已有的同名目录。升级前应先把旧目录移到可恢复的备份位置，安装
完成并验证通过后再决定是否删除备份；不要把新旧版本混合复制到同一目录。

从安装目录验证版本和 V2.3 必需文件：

```text
python scripts/release_check.py --expected-version 2.3.0
python scripts/self_test.py
python scripts/pipeline.py validate-profile assignment-en-zh
python scripts/pipeline.py validate-profile academic-paper-en-zh
python scripts/pipeline.py validate-profile lecture-notes-en-zh
```

完整的开发与发布测试矩阵见
[`references/development.md`](references/development.md)。安装或验证 Skill 不会读取、
转换任何用户 PDF。

## 使用方式

在支持 Skill 的产品中，可以直接提出类似请求：

> 使用 `$make-bilingual-study-pdf`，把这份英文论文制作成经过完整性检查的中英
> 对照 Markdown、可编辑 DOCX 和配套 PDF。

新任务和恢复任务也可以使用 Profile-aware 入口：

```text
python scripts/pipeline.py source SOURCE.pdf --work-dir WORK_DIR --profile assignment-en-zh
python scripts/pipeline.py import-mineru SOURCE.pdf MINERU_OUTPUT_DIR --work-dir WORK_DIR --profile academic-paper-en-zh
python scripts/pipeline.py status WORK_DIR
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

更详细的操作契约见 [`SKILL.md`](SKILL.md)，V2.3 的实现边界和验收证据见
[`references/profile-ir.md`](references/profile-ir.md) 与
[`.github/V2.3_ACCEPTANCE.md`](.github/V2.3_ACCEPTANCE.md)。

## 发布校验

```text
python scripts/release_check.py
```

该检查只使用 Python 标准库，核对 `VERSION`、README 中固定的安装 tag、三个
Profile 及 V2.3 安装负载。在 GitHub tag 工作流中，它还会拒绝与 `VERSION`
不一致的 tag。
