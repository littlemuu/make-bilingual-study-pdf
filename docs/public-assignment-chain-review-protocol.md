# 公开 assignment V1/V2 文档链复核协议（工包 A）

`tests/v23_e2e_test.py` 在 Ubuntu `automated-forward` 的完整工具链中，从冻结的原生文本
`assignment-native-source-expanded.pdf` 对公开 assignment 同时运行历史 V1 与候选 V2：生产
`pipeline source`、真实 Poppler oracle/source audit、12 种最终 native kind、translation/output、DOCX
构建/审计、LibreOffice PDF 编译、逐页渲染与 contact sheet 生成。它比较
`assignment-en-zh-v1-v2-expanded-projection.json`、规范化 DOCX 段落/样式/边框投影与逐页 PNG
尺寸/content-bbox/hash，并把完整 WORK 作为 CI artifact 上传。
冻结 source PDF SHA-256 为
`0cfe0a798f92722245ead59d9220b3834af1b38cc8e6e531bb417eca295398ef`；manifest、oracle、
blocks、IR 和 source audit 必须由这份字节链生产，禁止手写 passed gate。

自动化不得伪造视觉通过：两条链都必须真实调用 `pipeline.py finalize`，得到非零退出，
保持全部既有 gate 字节 hash 不变且不生成 passed QA；状态仍停在 `needs_visual_review`。
这是故意的 fail-closed 结果，而不是 skipped test。

## 人工视觉与最终 QA

在具备 Pandoc、LibreOffice、Poppler、Fontconfig、Noto CJK 与 DejaVu 字体的隔离本地
环境中运行 `tests/v23_e2e_test.py <EMPTY_OUTPUT_ROOT>`，然后逐页检查以下两份真实
contact sheet 与全分辨率 render：

- `<OUTPUT_ROOT>/assignment-v1/work/output/contact/` 与 `pdf-renders/`；
- `<OUTPUT_ROOT>/assignment-v2/work/output/contact/` 与 `pdf-renders/`。

每页必须确认 A4 页面未空白、中文可读、公式/标识符没有重复或遗漏、Problem 保持完整
英文半区/一条分隔线/完整中文半区，且 Example/Tip 没有吸收相邻段落。确认后才可对每个
WORK 执行：

```powershell
python skills/make-bilingual-study-pdf/scripts/record_visual_review.py <WORK> `
  --status passed --reviewed-pages all `
  --notes "Reviewed every public synthetic assignment render: bilingual halves, formulas, identifiers, Example/Tip boundaries, no clipping or blank pages."
python skills/make-bilingual-study-pdf/scripts/finalize_qa.py <WORK>
```

记录两条链的 Profile/IR before/after hash、DOCX audit 与 compile audit hash、reviewed
pages、contact-sheet hash、`qa-report.json` hash，以及 normalized projection hash。不得
把 `record_visual_review.py` 放进 CI 或测试自动执行；它只记录真实人类图像检查后的
attestation。


## 本轮复审增加的自动证据

- 原始 source PDF、V1 snapshot、V1/V2 projection 保留为历史文件，不改写其字节。
  新增 `*-expanded*` 文件单独冻结图注续段、图内文字与正常中文数学说明。
- `production-v2-base-visible.json` 来自精确 main `a8bb3dc1c7d36270181d4741d4ad5d87557598c7`
  的 run `33961158634` / artifact `9968027885`，保存 academic/lecture 的完整
  DOCX 段落/样式/边框和逐页 raster 投影。forward 要求工具链版本一致且两份投影完全相等；
  工具链漂移必须重新生成两侧证据，不能单独更新 expected hash 来让测试通过。
- Windows 测试使用复制出的安装负载和真实子进程，清除 PYTHONPATH；不加载
  sitecustomize。生产 PNG 发布使用 Pixmap.tobytes + atomic_write_bytes，文件保护不变。
- DOCX 反向测试分别删除中文目标、删除 drawing、重复 drawing、替换媒体内容，均要求
  occurrence 证据失败。ZIP 内只有一份媒体文件不等于只在文档里出现一次。
- Windows 修复作为独立前置提交审阅。前置 PR 尚未合并时，不能宣称已形成新的 main
  基线或进入工包 B；合并后需要再次核对精确 main 与最终 PR head 的 CI 证据。


## 现存 WORK 与重复内容的追加验收

`existing-work-migration.json` 记录测试参考驱动对已有 V1 WORK 安全副本的 dry-run、
manifest/Profile/IR before/after、旧 gate 失效、source audit 重建，以及与 fresh V2 的
DOCX/render 等价。它不表示生产 `migrate-profile` CLI 已在工包 A 上线。
`repeated-visuals-report.json` 来自真实两页 PDF：两个独立数学截图资产具有相同 PNG 字节，
但 ID/path 不同。DOCX 必须保留期望资产/目标文本的次数，PDF 必须保留实际 placement
次数；资源去重不能导致漏计，额外引用也不能被当作合法重复。
