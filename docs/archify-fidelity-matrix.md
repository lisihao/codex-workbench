# Archify v2.16.0 集成保真矩阵

状态：`partial / adapted`。`vendor/archify/` 是上游 `archify/` 稳定包的完整本地副本；Workbench 只增加了来源锁、角色路由边界、receipt 真值校验和双端 Skill 安装器。它没有把渲染通过升级为架构事实，也没有把旧的 DSH Archify 2.14 快照当作稳定实现。

## 1. 来源锁定

| 字段 | 固定值 | 证据 |
|---|---|---|
| Repository | `https://github.com/tt-a1i/archify` | `vendor/archify/SOURCE-LOCK.json:1-17` |
| Tag | `v2.16.0` | `vendor/archify/SOURCE-LOCK.json:4` |
| Peeled commit | `c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de` | `vendor/archify/SOURCE-LOCK.json:5` |
| Package version | `2.16.0` | `vendor/archify/package.json:1-3`；锁 `:6` |
| License | MIT | `vendor/archify/package.json:7`、`vendor/archify/LICENSE`、锁 `:7-12` |
| Runtime | Node `>=18` | `vendor/archify/package.json:10-12`；`vendor/archify/bin/archify.mjs:1211-1217` |

`SOURCE-LOCK.json` 同时记录上游根 `LICENSE` 与包内 `archify/LICENSE` 的出处；当前 vendor 包保留一个等价 MIT notice。`vendor/archify/` 之外没有复制 DSH adapter。上游 DSH adapter `archify-dsh-v0.1.0` 内嵌 Archify `2.14.0`，因此不能满足本集成的稳定版本要求。

## 2. Source-to-integration fidelity matrix

状态含义：`faithful` = 代码/资源按稳定包保留；`adapted` = Workbench 增加边界但不改上游语义；`excluded` = 有意不纳入，需按表中理由处理；`historical` = 仅作评测证据，不能当作当前性能事实。

| 上游能力/契约 | 上游证据（固定 commit） | 当前集成落点 | 状态与验收 |
|---|---|---|---|
| 五种 typed JSON IR：architecture、workflow、sequence、dataflow、lifecycle | `vendor/archify/SKILL.md:15-35,55-73`；`vendor/archify/schemas/README.md:6-20` | 完整 `vendor/archify/schemas/`、五个 renderer、五类示例；`src/codex_workbench/archify.py:26-32` 只声明同一类型集合 | faithful；`doctor` + 五 fixture `validate --quality showcase --json` |
| 严格 schema、未知字段拒绝、带 id/label 的结构化诊断 | `vendor/archify/schemas/README.md:17-20,199-211`；`vendor/archify/renderers/shared/validator.mjs:38-86` | 原样保留 schemas、生成的 standalone validators、diagnostics | faithful；`node bin/archify.mjs validate ... --json`，禁止使用 `npm install` 才能工作的替代 validator |
| 作者约束：稳定 ID、关系方向/标签、自动路由、`showcase` 质量档 | `vendor/archify/SKILL.md:19-35,75-94`；`vendor/archify/schemas/README.md:151-177,216-223` | Skill 精确投影到 `skills/archify/SKILL.md`；vendor 中保留 authoring contract 与示例 | faithful；`9/9`、`0 errors/0 warnings` 是 showcase 通过门槛 |
| Workflow v2 可读布局与 v1 固定兼容 | `vendor/archify/renderers/workflow/README.md:47-91,101-152` | 完整 `renderers/workflow/`、`migrations/workflow-v2.mjs`、v1 fixture | faithful；`validate workflow ... --layout-json`、`migrate workflow ... --to-schema 2 --json` |
| Architecture delta：稳定 ID、topology/semantic/geometry 分类、authored/revision-pinned proof | `vendor/archify/delta/architecture-delta.mjs:53-70,77-130,226-317` | 完整 `vendor/archify/delta/`；review role 允许 `compare`；Python receipt 只接受 complete + authored/revision-pinned | faithful + adapted；`compare architecture ... --quality showcase --json` |
| Atomic delivery：输入/产物 SHA-256、字节数、失败保留 last-good | `vendor/archify/references/delivery-contract.md:3-19`；CLI `vendor/archify/bin/archify.mjs:750-1114` | 原样保留 CLI；`validate_receipt()` 在 `src/codex_workbench/archify.py:419-427` 要求 specification/artifact/output | faithful + adapted；`deliver ... --json` 后再做 visual-check |
| Visual evidence：四种 desktop viewport、两种主题截图、contact sheet，自动 receipt 永远 `visualReview: pending` | `vendor/archify/SKILL.md:96-114`；`vendor/archify/references/delivery-contract.md:21-41`；CLI `:1156-1204` | 原样保留 `bin/visual-check.mjs`；validator `src/codex_workbench/archify.py:435-442,457-465` 不把 pending 当 reviewed | faithful + adapted；`visual-check <artifact.html> --json`，另需人工/图像审核才能标记 passed |
| Repo evidence / deployment ownership profile | `vendor/archify/schemas/README.md:175-194`；上游 CLI architecture `--repo-root` 入口 `vendor/archify/bin/archify.mjs:83-109,1808-1823` | vendor 保留全部 repo-evidence renderer 代码；角色合同要求 revision-pinned evidence 或外部语义合同 | adapted；本层不发现 owner、不证明 live environment |
| 设计与产品边界：证据 console、颜色/排版、可访问性、读者首屏 | 上游根 [`DESIGN.md`](https://github.com/tt-a1i/archify/blob/c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de/DESIGN.md#L82-L98) `:82-98,100-148,224-245`；[`PRODUCT.md`](https://github.com/tt-a1i/archify/blob/c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de/PRODUCT.md#L7-L38) `:7-38` | 作为审计来源与 Skill authoring 约束，不复制大型站点文档；上游包的 viewer/runtime 与模板完整保留 | faithful source reading；renderer 通过不等于语义或审美正确 |
| 根 README 的 typed IR、稳定 release、验证/交付边界 | 上游根 [`README.md`](https://github.com/tt-a1i/archify/blob/c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de/README.md#L13-L32) `:13-32,94-118,181-220,255-278` | 关键 runtime 文件按包完整 vendor；集成文档保存锁定 URL/行号，避免将 README 宣传数字当实测基线 | adapted；宣传 benchmark 不作为当前能力证明 |
| ordinary-model-floor benchmark | 上游 [`README.md`](https://github.com/tt-a1i/archify/blob/c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de/benchmarks/ordinary-model-floor/README.md#L1-L125) `:1-125`、`benchmark.mjs:118-278,453-523`、`manifest.json:1-33` | 不复制 benchmark runner、不触发模型；历史结果只能标 `historical`，不能宣称当前 uplift | historical；需独立同机/同模型/同量化 A/B 才能下结论 |
| DSH/deepseek-harness 插件 | 上游 [`README.md`](https://github.com/tt-a1i/archify/blob/c826e6c3a7abad19c0f3cd1ca57207d54b1ad8de/integrations/deepseek-harness/README.md#L3-L49) `:3-49` 明确 community/experimental 且为 Archify 2.14 | 不 vendor、不启用 native plugin、不修改 DSH；只保留稳定 v2.16 core | excluded；避免旧 snapshot 污染与 plugin 运行时耦合 |
| MIT notices / controlled Skill projection | 上游包 `vendor/archify/LICENSE`；`vendor/archify/SKILL.md:1-8` | `vendor/archify/LICENSE` + `SOURCE-LOCK.json`；`skills/archify/SKILL.md` 与 vendor SKILL 字节级相同 | faithful；`verify_vendor()`、`verify_skill_projection()` |

完整 stable core 的关键路径由 `REQUIRED_VENDOR_PATHS` 显式枚举（`src/codex_workbench/archify.py:34-61`），并且实际复制包含 package 下的全部 190 个文件；allow-list 只是防止删减/旧快照冒充，不是替代上游源码的重写。

## 3. 四种 role contract 与 truthful receipt

### Role routing

`ROLE_CONTRACTS` 位于 `src/codex_workbench/archify.py:104-159`，提供 `architecture`、`design`、`review`、`requirements` 四类角色；序列化入口为 `role_contract()`（`:162-170`）。每个角色都固定允许的 diagram types、CLI 命令、semantic gate、visual gate 和 forbidden claims：

| role | 适用任务 | semantic gate | 必须拒绝的过度结论 |
|---|---|---|---|
| architecture | 组件/边界/部署架构图 | 外部需求或 revision-pinned repo evidence | renderer pass ≠ architecture truth；authored reachability ≠ runtime blast radius |
| design | 五种 typed mode 的可读技术叙事 | 先有 external requirements contract | 排版通过 ≠ 语义正确；visual pending ≠ 已审美验收 |
| review | base/head delta、receipt、视觉审查 | 独立 requirements/code-review evidence | compare ≠ runtime risk、merge safety 或低风险证明 |
| requirements | 将必需节点、方向、路径、证据写入契约 | required + directional external semantic contract | schema validity ≠ requirements satisfaction |

`validate_receipt()` 的实现位于 `src/codex_workbench/archify.py:380-465`：

1. `renderer_pass` 只由 schema/receipt/9 个 artifact checks、composition、SHA/bytes、compare completeness 等机械条件决定。
2. 普通上游 receipt 的 `semantic_pass` 只有明确给出 semantic proof 才为 true；执行器接收的 Workbench receipt 还必须使用 `semantic: {"ok": true, "source": {"path": "...", "sha256": "...", "bytes": N}}`，并在当前 request worktree/read-write scope 内重新读取、重算和绑定该文件。为 role 调用或 `require_semantic=True` 时缺少它必失败。
3. `visual_pass` 与 renderer 分离；稳定上游 `visual-check` 的 `visualReview: "pending"` 得到非通过状态，`require_visual_review=True` 时必失败。
4. 代码常量 `RENDERER_PASS_NOT_SEMANTIC`（`:63-65`）把这条边界直接放进 verdict，不能由调用者把普通 `ok: true` 解释成领域真值。

因此 Workbench 的 artifact contract 是：候选 JSON → `validate`（showcase 9/9）→ `deliver`（冻结 specification/artifact bytes + SHA）→ `visual-check`（自动证据，review 仍 pending）→ 外部 semantic/visual receipt。任何一层的非零退出都不能写成成功。

## 4. 能力、边界、依赖与验收

### 实际能力

- 架构：组件、服务、边界、连接、repo evidence、architecture delta。
- 设计表达：inline SVG 的 standalone HTML、dark/light、pan/zoom/search/focus、关系 trace、可选 motion、导出路径；详见 `vendor/archify/SKILL.md:116-135`。
- 推理辅助：严格 typed IR、关系方向与 stable ID、workflow v2 rank/layout constraints、`semanticChecks`、delta 的 topology/semantic/geometry 分类。
- 交付审计：结构/几何九项 artifact checks、原子 deliver receipt、visual-check containment/captures、迁移 receipt。
- CLI 实际入口：`render`、`compare architecture`、`deliver`、`preview`、`validate`、`migrate workflow`、`inspect`、`check`、`visual-check`、`guide`、`brands`、`examples`、`doctor`、`demo`；完整 usage 在 `vendor/archify/bin/archify.mjs:15-34`。其中 `preview` 是用户主动请求的本地循环，不能作为无人值守验收；`brands capture` 需要用户提供官方 URL，不能把未锁定网络结果当事实。

### 不应宣称的能力

- 不会自动证明节点、标签、关系是否符合产品真实需求；未知事实应留空或补证据。
- 不会由 authored graph 推出运行时因果、影响半径、故障风险、merge safety、owner 或 live deployment 状态。
- visual-check 的截图和 containment 只是自动证据，不能替代人看 exact artifact 的 perceptual review。
- benchmark README 的历史模型结果不等于当前 Workbench/DSH uplift；没有同机同模型 A/B 就不下净收益结论。

### 依赖与成本

| 项目 | 实际边界 |
|---|---|
| Node | CLI/renderer 要求 Node `>=18`；当前验证机为 Node `v26.7.0` |
| npm | 核心 render/validate 使用已生成的 standalone validators，不需要 `node_modules`；本集成不执行 `npm install`、不全局安装 |
| Chrome | 仅 visual-check 需要 Chrome/Chromium；不可用时上游返回 exit `2`/`skipped`，不能伪造 reviewed |
| 模型/网络 | 基础渲染、doctor、五 fixture 不需要模型调用；本任务未触发模型、SSH 或远端部署 |
| 磁盘/维护 | 完整 package 约 7.3 MB、190 files；保留上游源码意味着随版本更新需重新 lock、重新验收 |
| Workbench 耦合 | Python adapter 通过 planner/executor/store/verifier 接入 receipt 链；两个设备安装器投影同一 pinned core；README、版本与 fidelity matrix 同步记录该能力 |

### 视觉质量提升 vs 架构推理质量提升

| 维度 | 净收益 | 成本/限制 | 结论 |
|---|---|---|---|
| 图示表达质量 | 高：五类 renderer、统一主题/模板、可读布局、9 项几何/组成门、原子 HTML 交付，能显著减少“能画但不可读”的输出 | Node 运行时、较大的 standalone HTML、Chrome 视觉证据与人工 review；自动检查不是审美判断 | 值得 vendor，适合 architecture/design/review 的 artifact 产出 |
| 架构推理质量 | 中：typed schema、stable IDs、semanticChecks、workflow v2、delta classification 让假设、方向和变更更显式 | 事实仍由外部 requirements/repo evidence 提供；Archify 不执行系统、不观测 live runtime、不推出 causal truth | 只有叠加 role routing + semantic receipt 才有净推理收益；单独安装 Skill 不能保证 |
| 运营复杂度 | 可控：本地完整 vendor、无 DSH plugin 依赖、两端安装器有 marker 和拒绝覆盖策略 | 版本锁/上游 drift/receipt contract 需要维护；不能把旧 DSH adapter 直接复用 | 采用两层结构，先稳定 core，后评估薄 plugin |

## 5. Skill、Plugin 还是两层结构

建议：**两层结构**。

1. `skills/archify/SKILL.md` 是 discoverability/prompt contract 的精确投影，面向 Codex 和 Claude 的 role routing；它不能承载 renderer 源码，也不能声称自己是第三方 native plugin。
2. `vendor/archify/` 是固定 commit 的完整执行 core；`src/codex_workbench/archify.py` 是 Workbench adapter，负责来源锁、角色 contract、receipt 真值边界。`scripts/install-archify.py` 将完整 core 与 marker 一起安全安装到两个 agent 端。
3. 未来若 Workbench plugin API 稳定，再加一个**薄 plugin**作为调用/发现层，调用同一 vendor core 和 Python validator；禁止复制第二份 renderer、降级到 2.14、或让 plugin 自己解释 `renderer_pass`。

强制服从方式：

```text
user request
    │ role router: architecture | design | review | requirements
    ▼
role contract (allowed types + commands + semantic gate + forbidden claims)
    ▼
typed JSON IR ──validate showcase──> 9/9 renderer receipt
    │                                  │ renderer_pass only
    ├──────── external semantic evidence/contract ───────┘
    ▼
deliver receipt (spec/artifact SHA + bytes, atomic last-good)
    ▼
visual-check receipt (pending) ──human/image review──> visual_review: passed
```

调用层应把 `role_contract(role)` 和 `validate_receipt(receipt, role=role, require_visual_review=...)` 作为硬门，而不是只读自由文本 Skill。`semantic.source` 必须可追溯到 requirements/revision-pinned evidence；review 不能把 compare 的 `authored` proof 说成运行时风险证明。

## 6. 安装与命令

默认目标是 `~/.codex/skills/archify/` 与 `~/.claude/skills/archify/`。安装器先验证 source lock、完整 vendor、Skill projection，以及 Codex/Claude 两个目标；目标或任一 ancestor（包括 broken symlink）为 symlink 时拒绝。已有同名目录只有包含 `.codex-workbench-archify.json` 且 marker 为本 Workbench 所有时才允许更新。两个完整 core 会先在各自 parent 的 sibling staging 中准备，再以同文件系统 `os.replace` 原子换入；第二端或中途失败会恢复已换入端并清理 staging/backup，成功升级不会保留旧 tree 文件。

只读预检（不触碰全局目标）：

```bash
tmp_home="$(mktemp -d /private/tmp/archify-install.XXXXXX)"
python3 scripts/install-archify.py \
  --source "$PWD" \
  --codex-root "$tmp_home/codex" \
  --claude-root "$tmp_home/claude" \
  --dry-run
```

实际安装仍应显式确认目标 home；本轮验收只使用 `/private/tmp`。安装 marker 包含 `managed_by`、agent、repository、tag、commit、version、license；完整 vendor tree 由 `_copy_tree()`（`:225-244`）复制，不以单独的旧 Skill 文本替代。

## 7. 可复现实测命令

以下命令不安装依赖、不调用模型、不使用 SSH；五 fixture 代表五种 renderer。提交/合并前应在稳定 checkout 重跑受影响命令：

```bash
scripts/python-runtime -m py_compile \
  src/codex_workbench/archify.py \
  scripts/install-archify.py \
  tests/test_archify.py

PYTHONPATH=src scripts/python-runtime -m unittest discover -s tests -p 'test_archify*.py'

node vendor/archify/bin/archify.mjs doctor
node vendor/archify/bin/archify.mjs validate architecture \
  vendor/archify/examples/web-app.architecture.json --quality showcase --json
node vendor/archify/bin/archify.mjs validate workflow \
  vendor/archify/examples/release-delivery.workflow.json --quality showcase --json
node vendor/archify/bin/archify.mjs validate sequence \
  vendor/archify/examples/async-job-roundtrip.sequence.json --quality showcase --json
node vendor/archify/bin/archify.mjs validate dataflow \
  vendor/archify/examples/event-stream.dataflow.json --quality showcase --json
node vendor/archify/bin/archify.mjs validate lifecycle \
  vendor/archify/examples/agent-run.lifecycle.json --quality showcase --json

node vendor/archify/bin/archify.mjs deliver architecture \
  vendor/archify/examples/web-app.architecture.json /private/tmp/archify-delivered.html \
  --quality showcase --json
node vendor/archify/bin/archify.mjs compare architecture \
  vendor/archify/examples/checkout-platform.base.architecture.json \
  vendor/archify/examples/checkout-platform.head.architecture.json \
  /private/tmp/archify-delta.html --quality showcase --json
node vendor/archify/bin/archify.mjs migrate workflow \
  vendor/archify/test/fixtures/v1-workflow-explicit-coordinates.workflow.json \
  /private/tmp/archify-workflow-v2.json --to-schema 2 --json
node vendor/archify/bin/archify.mjs visual-check \
  /private/tmp/archify-delivered.html --json
```

本 checkout 的 focused evidence 包含：`doctor` 报告 15 项 `[ok]` 并输出 `Archify is ready.`；五种 fixture 均为 `9` checks、`composition: pass`、`errors: 0`、`warnings: 0`；递归 strict-schema lint、真实文件 receipt 的 path/SHA-256/bytes scope binding、双端 installer 的 symlink/第二端故障回滚和 stale-tree 清理均有离线测试。visual-check 自动 receipt 的 `visualReview` 仍是 `pending`，这不是人工视觉通过声明。
