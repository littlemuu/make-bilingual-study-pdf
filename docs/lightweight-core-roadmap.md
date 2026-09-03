# 轻量核心产品路线

> 决策日期：2026-09-03  
> 状态：阶段一完成后的当前产品路线  
> 施工基线：`main@3762726ffbdeec42d9fe33241d0ddf4c020c99f7`

本文收束阶段一之后的产品方向。它覆盖“接下来优先做什么”和“哪些能力暂缓”，
并取代 [`architecture-simplification.md`](architecture-simplification.md) 中关于阶段三
必须优先拆分 `adapters/mineru.py` 的旧排序。阶段一已经完成的架构事实、测试证据、
ruleset 迁移和不可削弱的不变量仍以原文档为准，不因本路线改变。

本路线只改变后续优先级，不改变当前 Skill 的运行时行为、安装负载、Profile、
输出格式、QA 语义、VERSION、tag 或 Release 状态。

## 1. 产品决策

默认产品应是一条无需 GPU、模型权重或外部 AI 解析器的原生文本 PDF 路径：

- 使用 PyMuPDF 获取文字、坐标、字体、链接和视觉裁剪；
- 使用 Poppler 提供独立文本 oracle 与页面渲染；
- 生成稳定 ID、可恢复翻译批次、中英对照输出和冻结 QA 证据；
- 对无法可靠验证的扫描页、乱码页或阅读顺序问题明确停下，而不是静默猜测。

MinerU 保留为**显式选择的高级导入后端**。它不属于默认安装依赖，也不应成为
普通论文、讲义或作业必须经过的前置步骤。近期只维护现有经过验证的导入边界，
不围绕 MinerU 扩建主产品。

最短表述是：

> 原生文本是默认核心；文档构建按需启用；MinerU 是冻结的可选高级入口。

## 2. 当前能力事实

必须区分“当前已经实现”与“后续目标”。

| 场景 | 当前入口 | 当前定位 |
| --- | --- | --- |
| `assignment-en-zh` | 原生文本 PDF | 默认轻量路径；规则以 CS336 及相似作业为主要基线 |
| `academic-paper-en-zh` | 已冻结的 MinerU 3.x `pipeline` legacy 输出 | 可选高级路径 |
| `lecture-notes-en-zh` | 已冻结的 MinerU 3.x `pipeline` legacy 输出 | 可选高级路径 |

当前 Skill 不安装、不运行 MinerU，也不下载模型；MinerU 本身不在
`requirements.txt`。但是论文和讲义 Profile 目前仍绑定 `mineru-import`，因此简单论文
和讲义尚不能仅因为本路线更新就直接改走原生适配器。解除这种绑定属于后续功能工作，
必须有公开夹具和差分证据，不能仅改文档或 Profile 字段。

扫描件或严重乱码页面即使经过 MinerU/OCR，也只能作为人工比较材料。缺少可靠的
独立文字 oracle 时，状态必须保持 `manual_source_review_required`，不得自动形成
`passed` 的 source audit 或 final QA。

## 3. 三层能力边界

### 3.1 轻量核心

默认面向可选中文字、阅读顺序可验证的英文 PDF，包含：

- 原生文本抽取与页面证据；
- 稳定 block ID 与 `source_sha256`；
- 术语表、可恢复翻译批次与翻译审计；
- 公式、代码、链接、图片和占位符保护；
- 确定性 Markdown 构建与输出完整性检查；
- 明确的适用范围失败和人工审查边界。

默认入口不得要求 MinerU、Docling、OCR/VLM、GPU、模型下载或云端账户。

### 3.2 文档构建层

当用户需要可编辑 DOCX 或配套 PDF 时，按请求启用 Pandoc、python-docx、
LibreOffice Writer、Fontconfig、Poppler 工具和受支持的 CJK 字体。该层继续保留：

- DOCX 内容与结构审计；
- PDF A4、字体嵌入、中文可提取、空白页和渲染完整性检查；
- 与精确 DOCX/PDF 字节绑定的人工视觉复核。

这些工具属于文档生产依赖，不得与高级 AI 解析器混为同一个“默认依赖包”。

### 3.3 高级解析后端

当前仅保留已实现的 MinerU frozen import，未来其他解析器也必须显式 opt-in。任何
高级后端都只能提供候选结构和证据，不能取代独立 source audit、稳定 ID、冻结绑定或
人工视觉门禁。

## 4. MinerU 冻结策略

近期允许的工作：

- 保留现有 `import-mineru` 命令、Profile 和回归；
- 修复确定的安全问题、数据损坏或已支持格式的回归；
- 保持 hash-equal `origin.pdf`、输入/资源冻结和独立 Poppler 审计；
- 在文档中诚实说明版本、backend、格式和人工门禁限制。

近期不计划：

- 自动安装或执行 MinerU；
- 把模型、权重、Conda 环境、AutoDL notebook 或云端执行器打进 Skill；
- 扩展到 VLM、hybrid、office、预发布、4.x 或新的输出 schema；
- 将 OCR 结果直接视为完整性证明；
- 为了支持少量复杂 PDF，让所有论文和讲义用户强制经过 MinerU；
- 仅因为 `mineru.py` 文件较大就优先拆分它。

以后只有同时出现真实用户需求、可公开回归夹具、明确的许可证/部署边界和可维护的
独立审计方案时，才为 MinerU 扩展另立工单。

## 5. 更新后的施工顺序

### 阶段二：统一 Profile schema

把现有 `assignment-en-zh` 的 schema V1 行为等价迁移到 schema V2，并把普通运行时
收束为一套 Profile contract。该阶段只统一内部表达，不承诺扩展到任意作业格式。

推荐拆为三个原子 PR：

1. **迁移合同与差分真值。** 冻结现有 V1 Profile、语义匹配、IR、构建与 QA 行为；
   设计一次性迁移器和回滚边界。
2. **Profile 与旧工作目录迁移。** 将安装 Profile 改为 V2；只迁移精确已知的历史
   `assignment-en-zh` V1，任何未知或自定义 V1 均失败关闭。
3. **兼容层收尾。** 普通运行时只接受 V2；V1 解析仅保留在一次性迁移器和历史
   fixture 中，并更新 manifest、文档和完整测试证据。

阶段二不得混入 MinerU 扩展、通用作业识别、巨型模块拆分、可见样式变化或发布操作。

### 阶段三：轻量原生文本通用化

阶段二稳定后，优先增加直接改善默认用户路径的能力：

1. **无副作用适用性预检。** 在创建 WORK 前判断 PDF 更接近
   `native-ready`、`advanced-parser-suggested`、`manual-review-required` 或
   `unsupported`，并给出可核验原因。
2. **通用作业/handout Profile。** 在不改坏 CS336 兼容 Profile 的前提下，覆盖
   `Problem`、`Question`、`Exercise`、`Task`、`Part`、`Instructions`、`Hint`、
   `Note` 等常见结构。
3. **简单论文与讲义的原生路径。** 只有公开 fixture 能证明阅读顺序、公式、图表、
   脚注和角色 inventory 不弱于现有门禁时，才解除相应 Profile 对 MinerU 的强绑定。

阶段三的成功标准是：更多普通原生文本 PDF 无需高级解析器即可可靠完成，而不是支持的
parser 名称变多。

## 6. 巨型模块拆分改为需求驱动

`audit_source.py`、`audit_outputs.py`、`audit_docx.py` 和 `adapters/mineru.py` 的体积仍是
技术债，但不再把“一次性拆完四个大文件”设为独立产品阶段。

只有当一个阶段二/三改动被现有职责混杂实质阻塞时，才在同一问题域内先做最小拆分。
默认优先级为：

1. 当前轻量路径正在修改的 `audit_source.py`；
2. 与通用原生输出直接相关的 `audit_outputs.py`；
3. 与 DOCX 结构变化直接相关的 `audit_docx.py`；
4. 只有再次主动开发 MinerU 时才拆 `adapters/mineru.py`。

拆分仍不得引入通用插件框架、manager 层级、依赖注入容器或为拆文件而拆文件。

## 7. 保留的不变量

路线收敛不削弱以下事实：

- 模型只填写带稳定 ID 与源哈希的翻译记录，不直接生成整份最终文档；
- source → Profile → IR → translation → output → DOCX/PDF → visual → final QA
  冻结链保持 fail closed；
- PyMuPDF/高级 parser 结果不能独自证明源文件完整性；
- 扫描/乱码页不得静默 OCR 自动通过；
- 公式、代码、图表、链接、占位符和 disposition inventory 不得丢失；
- DOCX/PDF 字节变化会使旧审查失效；
- 私有 PDF、译文和真实成品不得进入公开仓库；
- tag、Release、ruleset 或已安装 Skill 的替换仍需独立授权。

## 8. 发布边界

本路线不创建 tag 或 Release，也不授权替换用户已安装的 Skill。阶段二是消除 V1/V2
双轨的最低架构前提；是否在阶段二后发布，或等待阶段三提供更通用的轻量入口，应由
单独的 release-candidate 工单基于当时的真实验收证据决定。
