# make-bilingual-study-pdf 项目交接：V2.3 实施与验收

> 更新时间：2026-08-14  
> 当前发布分支：`main`
>
> 合并来源：PR #1（开发基线）与 PR #2（V2.3，均经审查后 squash 合并）
>
> 验收记录：`.github/V2.3_ACCEPTANCE.md`

## 0. 当前实施状态

V2.3 已于 2026-08-14 完成独立复验并通过，PR #2 的三项 P1 审查线程均已闭合，
没有剩余阻塞项。获验收的实现代码为
`50f526b9edbc8417cd1c336af52651803f6a159b`，获复验的证据 head 为
`d666febd99ceae776a97c6544cb971e37504f974`；本次收尾只更新交接与验收记录，
并在合并前再次通过同一组 required CI。PR #2 经授权标记 Ready 后 squash 合并
至 `main`。

最终实现会把 PyMuPDF `get_image_info()` 返回的未旋转图像矩形先经
`page.rotation_matrix` 转换，再与旋转感知的 `page.rect` 裁剪并计算不重复并集。
审阅者独立验证了 0°、90°、180°、270°及非零 CropBox，60% 栅格覆盖均精确为
0.6；伪造覆盖率或删除人工审查理由，即使同步重绑 evidence/manifest/IR 哈希，
仍会被冻结 origin PDF 的重新计算拒绝。四页混合扫描回归保持 native=1、
adapter=0、文档比例 0.75，并停在 `manual_source_review_required`。

最终 GitHub Actions
[31809114971](https://github.com/littlemuu/make-bilingual-study-pdf/actions/runs/31809114971)
的 `self-test` 与 `automated-forward` 均通过；MinerU 契约为 16/16。下载 artifact
SHA-256 为 `a909d7ab0a9eb621589f46030a8f2275a0899039aaf64c6bc742fb1ee908dce7`。
两个 Profile 的六张最终渲染与此前逐页人工复核集合逐字节一致；CI 没有伪造新的
人工批准，缺少绑定到当前 PDF 字节的视觉签字时最终 QA 仍按设计 blocked。旧运行
证据保留在 `.github/V2.3_ACCEPTANCE.md`，并明确标为 superseded。
私有复杂论文/讲义与 CS336 90 页基线在本工作区不可用，验收记录明确保留该限制，
未用公开合成测试冒充私有回归。

已落地的主要能力：

- 可选 `mineru-import` 适配器只消费用户提供的 MinerU stable 3.x `pipeline`
  legacy 输出；不安装、不执行 MinerU，对 `vlm`、`hybrid`、`office`、预发布版、
  4.x、缺失 origin 绑定和路径/资产逃逸全部 fail closed。
- `academic-paper-en-zh` 与 `lecture-notes-en-zh` 两个 schema V2 Profile 已加入；
  角色、样式、输出处置与 QA inventory 通用化，`assignment-en-zh` 文件字节及
  canonical hash 保持不变。
- IR、翻译计划、Markdown、DOCX、PDF 与最终 QA 均绑定 Profile、adapter
  evidence、输入及资产哈希；显式结构证据才能形成 `complete` 容器，否则保持
  `anchor-only`。
- 扫描/严重乱码页只能进入 `manual_source_review_required`，不得由 MinerU
  输出自行证明 source audit passed；人工 review 必须绑定 comparison、非空
  contact sheets、逐页覆盖和最终 PDF 哈希。
- MinerU 双字段表格同时冻结结构化 `table_body` 与 `img_path` 视觉节点；V2 DOCX
  编译要求已通过且绑定精确 DOCX 字节的审计，最终 QA 会再次复核完整冻结链。
- 提交了原创、可重生成的 MinerU 3.4.4 pipeline 合成夹具及故障注入测试；
  本地快速矩阵继续包含旧基线 19 项，并分别报告 Profile、MinerU、IR/source、
  output、DOCX 与 visual/freeze 专项结果。完整命令和最终验收证据以
  `.github/V2.3_ACCEPTANCE.md` 为准。

以下章节保留 V2.2 基线与 V2.3 冻结设计，供后续审计设计偏差；若描述与已经
落地的 schema/CLI 冲突，以代码、Profile、测试和验收记录共同形成的当前契约
为准。

本文是施工交接上下文。实现前请先阅读仓库中的 `SKILL.md`、
`references/profile-ir.md`、`references/qa-rules.md` 和
`references/backend-options.md`。不要依赖此前对话才能理解本计划。

## 1. V2.3 开工前的一句话现状（历史基线）

V2.2 当时已把原先面向单份 CS336 作业的流程重构为“版本化 Profile + 统一文档
IR + 可恢复 Pipeline”，并保持默认 `assignment-en-zh` 成品零回归；在 V2.3
开工前，真正实现并承诺支持的输入仍只有 `native-text-pdf`，Profile 的语义样式
和 DOCX 审计也仍带有 `Problem / Example / Tip` 专用假设。

V2.3 随后按本交接文档冻结的边界完成：没有安装或内嵌 MinerU，而是建立可选、
可审计的 MinerU 导入适配器，并让论文、讲义两类 Profile 走通同一套翻译、
DOCX/PDF 和 QA 链路。当前发布结论以上面的实施状态与验收记录为准。

## 2. 已完成的 V2.2

### 2.1 Profile 控制面

- `profiles/assignment-en-zh.json` 是当前唯一受支持 Profile。
- Profile 绑定到每个工作目录的 `profile.json`，恢复任务时不得偷读新版安装
  Profile 替换已经冻结的副本。
- `profile_sha256` 表示规范化 JSON 哈希；`profile_file_sha256` 表示文件字节
  哈希。两者语义已经分开。
- Profile 已集中管理输入门槛、语言、阅读顺序、语义角色、DOCX 字体/主题和
  QA 参数。
- 当前校验器只接受 `native-text-pdf`、`source-then-target`，并仍要求
  `problem / example / tip` 三类样式。

### 2.2 统一文档 IR

- `scripts/document_ir.py` 从 `manifest.json` 与 `blocks.jsonl` 生成
  `document-ir.json`。
- IR 记录稳定节点 ID、原文及哈希、页码与 bbox、可翻译性、保护片段、链接、
  视觉资产和关系证据。
- IR 与绑定 Profile、manifest、blocks 均有哈希关联；任一漂移都会使 IR 失效。
- 原生 PDF 只能证明语义标签所在锚点，因此语义组使用
  `membership: "anchor-only"`。不得因为段落相邻就伪造完整容器成员关系。
- 旧工作目录可显式迁移，但迁移后必须重新跑源审计，并使旧翻译计划及后续
  产物全部失效。

### 2.3 可恢复 Pipeline

`scripts/pipeline.py` 已提供：

- `validate-profile`
- `source`
- `source-audit`
- `ir`
- `prepare`
- `build`
- `docx`
- `compile-docx`
- `finalize`
- `status`

`status` 会检查冻结的 Profile、IR、翻译计划和请求批次，不只读取旧报告里的
`passed` 字样。人工术语表审阅、翻译和逐页视觉验收仍是显式检查点。

### 2.4 V2 DOCX/PDF 路径

- DOCX 由审计后的 Markdown 经 Pandoc AST 和 `python-docx` 确定性生成。
- `Problem` 内部保持完整英文任务在前、中文任务在后，列表、代码与公式留在
  同一语义卡片中。
- 中文目标语言标签只能留在已有容器内部，不得重复开启第二个 callout。
- 编号段落与普通段落共享同一边框原点；语言分隔线只有一条且有界。
- LibreOffice PDF 转换门禁检查 A4、字体嵌入、中文可提取性、Problem 数量、
  空白页、逐页渲染和 PNG 完整解码。
- DOCX 路径的 `status` / `automated_status` 已兼容，最终 QA 能正式收录 MD、
  DOCX、PDF 与对应哈希。

## 3. 已验证基线

### 3.1 自动测试

2026-08-14 重新执行：

```bash
python3 scripts/self_test.py
```

结果：`status = passed`，19/19。覆盖：

- 默认 Profile 与三类作业语义角色；
- Profile 绑定、IR 证据等级与漂移检测；
- URL、Unicode 数学符号与数学斜体保护；
- 截断 PNG 检测与单页阻塞式修复；
- Problem 编号边框原点与唯一分隔线；
- 目标语言标签不重复开启 callout；
- DOCX 编译、视觉门禁与最终 QA；
- Profile 规范化哈希、文件哈希和请求批次冻结；
- 正常、缺失、重复、陈旧哈希、占位符破坏、术语遗漏和英文复制等翻译分支。

测试环境出现 Fontconfig 缓存目录不可写的提示，但断言与退出码均通过；这不是
功能失败。若后续把“零警告”作为 CI 要求，应为 Fontconfig 指定可写缓存目录，
不要删除字体核验。

### 3.2 真实 CS336 前向与零回归

V2.2 已用原始 47 页英文 PDF 做全新前向测试：

- 1024 个 IR 节点；
- 38 个 Problem、8 个 Example、8 个 Tip；
- 21 个外链、87 个视觉资产；
- 独立文本五元组覆盖率 `0.9886`；
- 源审计无失败、无警告；
- 797 个可翻译片段分成 15 批；零响应时状态为 `incomplete`，不是误判失败或
  误建成品；
- Profile、IR、翻译计划和请求批次均已冻结哈希。

完整成品回归：

- DOCX `word/document.xml` 哈希与 V2.1 基线一致；
- 90/90 页渲染 PNG 字节一致；
- 38 个 Problem、164 个编号段、8 个 Example、8 个 Tip、21 个链接和 2 张
  技术图全部一致；
- 无空白页、无截断 PNG，字体门禁通过。

真实课程 PDF、译文和 90 页成品不属于仓库测试夹具。不要把它们提交到公开
仓库；若本地仍可用，可作为发布前的私有回归门。

## 4. V2.3 开工时的限制与技术债（历史基线）

以下内容记录 V2.3 开工时的未支持项，现由实现、测试和验收记录逐项取代；保留
这些条目用于审计施工范围，不应再把它们解释成当前发布状态：

1. `profile.py` 将输入适配器硬限制为 `native-text-pdf`。
2. Profile 语义样式仍限制在 `problem / example / tip`。
3. `document_ir.py` 假设输入一定是 `manifest.json + blocks.jsonl`，并把所有
   语义组固定写成 `anchor-only`。
4. `pipeline.py docx` 和 `audit_docx.py` 仍通过 Problem、Example、Tip 三个专用
   计数参数验收，而非通用语义角色清单。
5. 论文的标题/摘要/章节/图表/参考文献以及讲义的定义/定理/证明/警告没有正式
   Profile 契约。
6. 扫描件没有独立 OCR 完整性 oracle。MinerU 自己的解析结果不能同时充当
   “解析结果”和“完整性证明”。
7. 当前技能不追求原版面像素级覆盖翻译；V2.3 也不改变这一目标。

## 5. V2.3 冻结决策

施工中除非发现代码事实冲突，否则遵守这些边界：

1. **默认路径零回归。** `assignment-en-zh` 的命令、IR 和成品行为保持兼容。
2. **MinerU 显式选择。** 不静默切换 OCR，不在技能中自动安装 MinerU、下载
   模型或启动远程服务。
3. **V2.3 先做 importer。** 适配器消费用户明确提供、已经生成的 MinerU 输出
   目录；调用 MinerU CLI 的 runner 属于后续可选层。
4. **结构化 JSON 是主证据。** 首选 `content_list.json` 提供顺序化内容，结合
   `middle.json` 保存页结构、backend、版本和 bbox 等证据。Markdown 只作对照，
   不作为唯一 IR 来源。
5. **不接开发中格式。** 官方把 `content_list_v2.json` 标为 development / subject
   to change；V2.3 初版不要以它作为稳定契约。
6. **证据决定成员关系。** 只有 MinerU 结构明确证明容器层级时才能产生完整
   `member_node_ids`；否则仍为 `anchor-only`。
7. **逐项处置，不静默丢弃。** 每个 MinerU content item 必须映射成 IR 节点、
   视觉资产、显式省略项或失败项。
8. **扫描件不提前承诺 passed。** 没有独立 OCR oracle 时，扫描/严重乱码页面
   必须停在明确的人工源审阅状态，不能仅因 MinerU 成功返回而通过源门禁。
9. **外部版本不写死。** 记录 MinerU `_version_name`、`_backend` 与输入文件哈希，
   对未知主版本 fail closed；运行时按能力/字段校验，而非只比较版本字符串。
10. **授权重新核验。** 截至 2026-08-14，MinerU 采用基于 Apache 2.0、附带额外
    条款的 MinerU Open Source License。接入前和发布前均核对官方当前许可；
    不复制或打包 MinerU 代码、模型与权重。

官方依据：

- [MinerU 输出格式](https://opendatalab.github.io/MinerU/reference/output_files/)
- [MinerU 官方仓库](https://github.com/opendatalab/MinerU)
- [MinerU 许可证](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)
- [MinerU Releases](https://github.com/opendatalab/MinerU/releases)

## 6. 目标架构

```text
原始 PDF ───────────────┐
                        ├─ native-text-pdf adapter ─┐
预生成 MinerU 输出目录 ─┘                          │
                          mineru-import adapter ────┤
                                                   ▼
                                      规范化 source records
                                                   │
                                      Profile-bound document IR
                                                   │
                           glossary → translation → deterministic build
                                                   │
                                      DOCX/PDF → layered QA
```

适配器只负责“源文档 → 有证据的规范记录”。翻译、术语、占位符、渲染和最终
QA 继续复用现有链路，不允许 MinerU 直接绕过这些门禁生成最终双语成品。

## 7. 工包 A：MinerU Import Adapter

### 7.1 建议接口

新增显式命令，命名可以调整，但职责不得混合：

```bash
python3 scripts/pipeline.py import-mineru SOURCE.pdf MINERU_OUTPUT_DIR \
  --work-dir WORK_DIR --profile academic-paper-en-zh
```

该命令不执行 MinerU；只导入、规范化、生成 IR、运行源审计并初始化术语表。

建议新增 `scripts/adapters/`：

- `base.py`：规范化记录与 adapter result 契约；
- `native_pdf.py`：包装现有原生 PDF 行为，避免两套调度逻辑；
- `mineru.py`：MinerU 输出发现、校验、规范化与证据保留。

若为减少本轮改动而不移动旧提取代码，也可先让 registry 调用现有
`extract_pdf.py`，但 `pipeline.py` 不应继续硬编码单一提取器。

### 7.2 MinerU 输入契约

至少要求：

- 原始 PDF；
- 与其对应的 `{name}_content_list.json`；
- `{name}_middle.json`；
- JSON 引用的 `images/` 资产；
- 可选 `{name}_layout.pdf`、`{name}_span.pdf` 和 Markdown，仅用于调试/人工审阅。

导入时校验：

- 原始文件名/哈希、页数与所有 `page_idx`；
- `bbox = [x0, y0, x1, y1]`，且坐标符合 MinerU 0–1000 映射约定；
- `_backend`、`_version_name` 存在并写入 adapter evidence；
- 所有图片路径都解析在 MinerU 输出目录内部，拒绝绝对路径与 `..` 路径穿越；
- 引用资产存在、可完整解码并记录 SHA-256；
- JSON item 的顺序、类型和源指针稳定；
- 未知类型、缺字段、重复 ID、越界页码或无出处内容 fail closed。

### 7.3 类型映射

以官方 `content_list.json` 契约为起点：

| MinerU type | 规范化处理 |
|---|---|
| `text` | 依据 `text_level` 映射 heading / paragraph；保存原始级别 |
| `equation` | 公式节点；保护 LaTeX，保留公式图片（若有） |
| `image` / `chart` | 视觉节点；分别处理 caption、footnote、sub_type |
| `table` | 表格节点；保留 HTML body、图片、caption、footnote |
| `code` | 代码主体只出现一次；caption/footnote 可翻译；保留 sub_type |
| `list` | 列表或参考文献列表；保留 sub_type 和顺序 |
| 页眉/页脚/页码/旁注/页脚注 | 显式分类，按 Profile 决定保留、翻译或 `artifact_omitted` |

不要把整张表格 HTML 或代码主体作为普通自然语言交给翻译模型。保护策略必须先
于翻译计划生成。

### 7.4 稳定 ID 与证据

同一份冻结的 MinerU 输出必须重复生成完全相同的 IR。节点至少保存：

- 页码与页内顺序；
- `content_list` 数组索引 / JSON Pointer；
- 对应 `middle.json` 证据指针（能可靠匹配时）；
- 原始 type / sub_type / text_level；
- bbox 与坐标系；
- MinerU backend、version；
- 原文或结构体的规范化 SHA-256；
- 引用图片的相对路径与 SHA-256。

不要宣称不同 MinerU 版本之间 ID 必然稳定。跨版本变化由冻结输出哈希和 stale
检测处理。

## 8. 工包 B：通用 Profile 与语义样式

### 8.1 先拆除三个硬编码

1. 把 `profile.py` 的 adapter allow-list 改为 adapter registry 校验。
2. 把 `REQUIRED_ROLES = {problem, example, tip}` 改为通用语义 role + 渲染 style
   契约；未知 style 必须失败，不能回退成随意排版。
3. 把 DOCX 的三个专用 expected count 改为从 Profile/IR 读取通用 role inventory。
   旧 CLI 参数可保留为兼容别名，但新代码不得依赖它们才能完成审计。

### 8.2 `academic-paper-en-zh`

最小语义集合：

- title / author-affiliation（可配置为只保留一次）；
- abstract；
- section / subsection；
- ordinary paragraph；
- figure / chart caption；
- table caption / footnote；
- equation；
- code / algorithm；
- references；
- page footnote。

论文 Profile 不应把每个 section 做成作业式 callout。标题层级、图注与参考文献
属于结构/样式，完整性由 role inventory、顺序和链接/引用处置证明。

### 8.3 `lecture-notes-en-zh`

最小语义集合：

- title / section / subsection；
- definition；
- theorem / lemma / proposition / corollary；
- proof；
- example；
- note / warning / tip；
- equation / figure / table / code；
- exercise（存在时）。

只有源结构证明范围时，definition/theorem/proof 等才生成完整语义容器；仅匹配到
标题文字时继续使用 `anchor-only`。

### 8.4 Profile QA 泛化

建议将单一 `primary_semantic_role` 扩展为：

- 必须出现的角色及最小数量；
- 允许为零但必须逐项处置的角色；
- 角色的渲染策略；
- 辅助内容（页眉、页码等）的保留/省略规则；
- 文档类型专属的顺序或配对约束。

不要只新增两个 JSON 文件便宣称支持论文/讲义；实现、渲染和失败测试必须同时
落地。

## 9. 工包 C：Pipeline 与分层 QA

### 9.1 Pipeline

- `validate-profile` 同时验证 Profile schema 和引用 adapter/style 是否已注册。
- `source` 保持现有原生 PDF 行为；`import-mineru` 走新适配器。
- `status` 显示 adapter、parser version、源证据状态和下一条可恢复命令。
- Profile、adapter 输出、IR、translation plan 和 batch hashes 形成完整冻结链。
- 任何 adapter 输出或引用图片变化都让源审计及所有下游产物 stale。

### 9.2 源审计

MinerU 路径至少证明：

- 原始 PDF 每一页有明确状态；
- 每个 content item 有唯一 disposition；
- 所有 JSON 类型都已识别，无静默丢项；
- 视觉资产存在、完整解码、哈希稳定；
- bbox、页号、顺序和结构引用有效；
- 对 native-text 页继续用 Poppler 作为独立文本 oracle；
- 对扫描/乱码页生成源页与 MinerU layout/span 的并列联系表，并停在显式人工
  源审阅检查点，直到独立 OCR 门禁另行实现。

### 9.3 输出审计

- 从 IR 的通用 role inventory 计算预期数量，不再硬编码三类作业角色。
- 论文检查标题层级、摘要、图表/公式/参考文献处置与外链。
- 讲义检查定义/定理/证明等结构的顺序与容器证据。
- 所有 Profile 继续检查英文在前、中文紧随其后、占位符、字体、空白页、PNG
  完整性和逐页视觉审阅。

## 10. 工包 D：测试矩阵与故障注入

### 10.1 单元夹具

测试不能要求 CI 下载 MinerU 模型。提交小型、人工审阅过的 JSON/图片夹具：

- 一页论文：heading、正文、公式、图注、参考文献；
- 一页讲义：definition、theorem、proof、example；
- 一页复杂内容：双栏顺序、表格、代码、footnote；
- 一个最小 `middle.json` 与对应 `content_list.json`；
- 一个未知主版本 / 未知类型夹具。

夹具必须注明其 MinerU schema/version 来源，不提交版权受限的完整论文 PDF。

### 10.2 必测失败分支

- 缺失 `content_list.json` 或 `middle.json`；
- JSON 损坏、backend/version 缺失；
- page index 或 bbox 越界；
- 绝对路径、`..` 路径穿越、图片缺失或截断；
- 未知 content type / sub_type；
- 重复稳定 ID、顺序不确定或 source hash 漂移；
- 伪造完整 semantic membership；
- Profile 引用了未注册 adapter/style；
- 扫描页在没有独立 oracle 时被错误标成 `passed`；
- MinerU 输出变化后旧翻译计划仍被错误复用。

### 10.3 必测成功分支

- 同一 MinerU 夹具两次导入得到完全相同 IR 哈希；
- 两个新 Profile 均通过校验并生成预期 role inventory；
- 论文和讲义各完成最小端到端 MD → DOCX → PDF → QA；
- 原有 19 项自测全部继续通过；
- `assignment-en-zh` 的真实 CS336 私有回归继续 90/90 页一致（源文件可用时）。

## 11. 推荐提交顺序

为便于审阅和回退，分四个原子提交/PR：

1. **Adapter contract + fixtures**  
   registry、MinerU importer、稳定 ID、证据字段、源审计和故障测试；不改 DOCX。
2. **Generic Profile semantics**  
   拆除三角色硬编码，新增论文/讲义 Profile 与 schema 测试；默认 Profile 零回归。
3. **Pipeline + generic DOCX/QA**  
   统一 role inventory、状态诊断、两类最小端到端成品和最终 QA。
4. **Forward test + documentation**  
   用真实复杂论文/讲义做前向测试，补充结果、限制和受支持 MinerU schema；确认
   许可后再更新 `SKILL.md` 的支持声明。

每个提交都先运行：

```bash
python3 scripts/self_test.py
python3 scripts/pipeline.py validate-profile assignment-en-zh
```

新增 Profile 后再分别运行对应的 `validate-profile`。任何阶段若默认 19 项测试或
CS336 私有基线变化，先停止并定位，不要把变化解释为“通用化的正常代价”。

## 12. V2.3 完成定义

只有同时满足以下条件，才可宣称 V2.3 完成：

- MinerU importer 不安装/运行 MinerU，也能从冻结输出目录确定性生成 IR；
- adapter schema、版本、backend、所有输入和引用资产均有哈希证据；
- 未知/损坏/越界/路径穿越/扫描无 oracle 等失败分支 fail closed；
- `academic-paper-en-zh` 与 `lecture-notes-en-zh` 不只是 JSON 存在，而是各有一次
  完整 MD/DOCX/PDF/QA 前向通过；
- 默认 `assignment-en-zh` 行为、19 项测试与可用的 CS336 私有基线零回归；
- 不支持的扫描件不会得到 `qa-report.json: passed`；
- `SKILL.md`、Profile/IR 契约和后端说明与真实实现一致；
- 发布说明记录实测 MinerU 版本、backend、fixture schema 和已知限制；
- 最终提交后从 GitHub 反向读取关键文件，确认公开仓库与验收版本一致。

## 13. 暂不纳入 V2.3

- 自动安装 MinerU、自动下载模型或托管 MinerU 服务；
- 以 MinerU Markdown 直接替代证据化 IR；
- 对所有扫描件宣称自动、可证明的完整翻译；
- 多语言插件系统；
- DOCX/Markdown 原生输入适配器；
- 原版面像素级双语覆盖；
- BabelDOC / PDFMathTranslate / Docling 同轮接入；
- 为追求“通用”而放宽现有缺页、链接、字体、PNG 或视觉验收门禁。

这些能力可以在 V2.3 稳定后分别规划，不能与本轮混做。
