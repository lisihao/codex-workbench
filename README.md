# Codex Workbench

> A self-hosted, macOS-only control plane for durable, evidence-gated AI development.
>
> 将一台常驻 Mac mini 变成唯一的开发权威端，把 MacBook 上的 Codex 变成随时可接续的驾驶舱。

Codex Workbench 是面向拥有一台长期运行 Mac mini 与一台 MacBook 的技术操作者或小团队的可复用自托管开发控制面：它把一个自然语言目标编译为有范围、有依赖、有验收条件的任务图（DAG），在隔离 Git worktree 中并行执行，并且只有独立验收者确认了可复查的 Evidence 后，任务才会进入 `accepted`。

它适合已经有一台可长期运行的 Mac mini、主要在 MacBook 上使用 Codex、并希望把 Claude Code 订阅作为受控后台算力的人。它不是云端 SaaS，不是通用的“一键式 Codex 插件”，也不依赖或管理 DSH、Solar 或 AI4Research。

![Codex Workbench 使用方式与数据流](docs/codex-workbench-usage-dataflow.svg)

## 它解决什么问题

日常 AI 开发很容易在设备切换、网络中断、并行写冲突、重复测试和配额透支中失去上下文。Workbench 将这些问题变成可执行的系统约束：

| 问题 | Workbench 的做法 |
| --- | --- |
| MacBook 合盖或网络中断后，任务难以接续 | Mac mini 是唯一的持久 Authority；MacBook 只消费同一份状态。已绑定会话失联时明确回退本地 checkout，并保留可重试的 outbox。 |
| 多个 Agent 同时改代码互相踩踏 | DAG 只并行运行无依赖、作用域不冲突的节点；每个 Worker 使用独立分支和 Git worktree。 |
| Worktree 自动清理误删了以后需要的数据 | 终态 worktree 先移入本地回收区；只有 NAS 压缩包通过完整恢复验证并产生持久回执后才删除。离家时也可强制走 Tailscale 把恢复包交给 Mac mini 写入 NAS。 |
| “Worker 跑完了”被误当作完成 | Worker 结果不是验收。只有独立 verifier 收齐约定的 diff、检查日志和 verdict 后，任务才可 `accepted`。 |
| 强模型被实现细节占满 | Sol 负责需求编译、跨模块判断和最终验收；边界明确的实现优先交给 Spark、Luna、Terra 或受配额约束的 Claude Worker。 |
| Claude Code 订阅被后台任务耗尽 | Claude Worker 只在认证和新鲜配额快照可证明时启用；未知状态会 fail closed 并转交 Codex。系统保留至少 20% 的目标配额空间，并设有更早的调度门槛。 |
| 不知道哪个模型在当前工作里更合适 | Workbench 使用按领域的公开 benchmark 冷启动先验，再用长期运行账本校准；它不把不同 benchmark 拼成一个排行榜，也不把公开分数冒充本机成功率。 |
| 联网时能看到模型众测数据，断网后调度却失去依据 | 通用 `codex_radar_provider` 将 personal-use consent 允许读取的 Codex Radar 公共 JSON 写入自己的 SQLite；断网复用数据库 last-known-good，过期后回落内置 baseline。 |
| 需要跨模型的质量、稳定性和成本观察 | 可选的 AI Frontier Provider 只采集两个聚合 JSON 和最多八个当前可路由精确模型的 benchmark；Mac mini 保存 SQLite/LKG，断网继续用缓存或内置基线，字段只作外部弱先验。 |
| 验证反复运行、成本高且结论不清 | `code-as-harness/v1` 将 L0–L3 验证层级和 Evidence fingerprint 写入任务契约；相同输入闭包的已通过证据可复用。 |

## 运行模型

```text
                  Codex on MacBook / Codex Remote on phone
                                      │
                        `wb` plugin / Codex MCP binding
                                      │
                                      ▼
┌────────────────────────────────────────────────────────────────────┐
│ Mac mini Authority                                                   │
│                                                                    │
│  SQLite task ledger ──► Sol planner ──► scope-aware parallel DAG   │
│         │                       │                                  │
│         │                       ├── Spark P0 lane / Luna / Terra   │
│         │                       ├── optional Claude Code workers   │
│         │                       └── deterministic build/test tools │
│         │                                                          │
│         ├── public baseline + consented Radar/AI Frontier DB LKG   │
│         │                         + runtime ledger ──► score policy │
│         │                                                          │
│         └──► independent Sol verifier ──► Evidence ──► accepted   │
└────────────────────────────────────────────────────────────────────┘
                                      │
                           snapshots + cursor events
                                      │
                                      ▼
                         MacBook cockpit / local fallback
```

### 核心组件

- **Mac mini Authority**：唯一的 SQLite 写入者、任务账本、协调器、ArtifactStore 和后台执行位置。它可通过 `launchd` 常驻运行；其他设备不会创建第二个协调器或复制第二份账本。
- **MacBook cockpit**：Codex 的 MCP 入口、状态面板和控制端。它读取 Authority 的 snapshot + cursor events，可调整优先级、追加 steering、查看 Evidence 和做受契约约束的控制操作。
- **手机 Codex Remote**：手机端 Codex 的 Remote 页连接 Mac mini 上的 Codex app-server；同一 Workbench 插件与 MCP 让远程会话查看进度、发送新指令并继续既有任务。首次短效配对必须由用户在 Mac mini 终端完成。
- **`wb` Codex 入口**：一个薄插件，将新会话或已有会话绑定到同一份持久任务。它同步经脱敏的会话摘要与受控 Git 上下文，而不持有第二份任务状态。
- **Claude Code Worker**：可选的订阅型执行器，不承担规划或最终验收。认证、CLI 兼容性或配额状态不明确时不会猜测余额，也不会使用 API-key fallback。
- **Claude 登录与配额采集**：Claude Code 原生订阅 OAuth 的凭据由官方 macOS CLI 保存在系统 Keychain；Workbench 不复制、导出或持久化 token，只调用 `auth status` 与受限的 `/usage` 观察。当前受支持的 collector 兼容锁定的 Claude CLI `2.1.239`，既接受对象也接受其真实数组 envelope；认证或解析失败会 fail closed，不循环重新登录。
- **Codex Radar Provider**：独立于 Workbench/DSH 的标准库-only package/CLI（provider plugin 0.2.0）。Mac mini 在 personal-use consent 或官方 receipt 成立后低频采集；`<state_root>/radar.sqlite3` 是 snapshots/raw/models/insights/active 的权威真源，JSON 仅为兼容投影，MacBook 只读 Authority 状态。Radar 不是配额源，也不能绕过路由硬门禁。
- **AI Frontier Provider**：独立于 Workbench/DSH 的标准库-only package/CLI（provider plugin 0.1.0）。Mac mini 在个人自用 consent 后低频采集两个聚合 JSON，并为最多八个当前可路由精确模型读取分类 benchmark；`<state_root>/ai-frontier/ai-frontier.sqlite3` 保存快照、原始 payload、模型、分类和 active LKG。安装不创建 receipt，MacBook 只读；Quality、Consistency、Real Cost 和 frontier/oracle 都不能替代本机验收、订阅 quota 或单模型质量证明。
- **离线回落**：如果 Authority 不可达，已绑定会话继续在 MacBook 当前 checkout 上工作；系统不会在离线端静默创建第二个 Authority，下一次可用连接再重试同步。

## 核心能力与边界

### 并行但不失控

任务首先由 Sol 规划器编译为持久 TaskContract 和 DAG。只有依赖已完成且读写 scope 不冲突的节点才会并行；父子路径互斥，纯读节点可以并行。每个执行节点在独立 worktree/branch 中运行，因此并行提升吞吐量而不会把共享工作树变成竞态源。

路由不是固定“弱模型干活”的盲目规则：低风险、短且可机械验收的工作可进入独立 Spark 池；边界明确的常规实现优先 Luna；较大的独立切片可以升级 Terra；需求拆解、跨模块判断和最终验收留给 Sol。Claude Code 的 Sonnet、Opus 或 Fable 只在其订阅资格和受保护配额可证明时作为 Worker 使用。

Workbench 对 `gpt-5.6-sol`、`gpt-5.6-terra` 和 `gpt-5.6-luna` 的 Codex 进程显式传入 `model_context_window=500000` 与 `model_auto_compact_token_limit=450000`。这是容量与额度消耗之间的默认平衡点；受管 planner/worker 使用 `--ignore-user-config`，因此必须显式传入。`gpt-5.3-codex-spark` 不接收这两个覆盖值，保持其模型自身的上下文合同。

Spark 是一个独立的逻辑队列，不是另一套协调器。它和普通 Worker 共享全局执行器上限，但拥有自己的容量、等待、启动和 busy-slot 计数；默认上限为 `min(4, max_workers)`，可用 `serve --spark-workers N` 调整，`0` 表示关闭 Spark 优先 lane。规划器会主动寻找互不冲突、可单独验收的短切片；无法安全拆分时保留 Luna/Terra 的较大切片。routing-v3 的 Spark 失败不会被当成成功，也不会在 claim 时绕过已固定能力目录静默换模型；需要换档时由后续 planner repair 重新路由，最终仍由 Sol 验收。

### 可恢复的 Worktree 回收与 NAS 归档

每次 worktree 分配都会写入持久账本。任务进入 `accepted` 或 `cancelled` 后，后台维护线程只做以下有序状态转换：

```text
active → quarantine_pending → quarantined
       → archive pending → verified → purged
                                └────→ restored
```

- 没有新鲜家庭 LAN 凭证且未配置远程归档目标时，只使用 `git worktree move` 移到本地回收目录，不删除工作树或分支。
- MacBook 的 location-aware heartbeat 只有在显式 home CIDR 匹配且 LAN 探测成功时，才写入十分钟短效在家凭证；Tailscale 连通、普通私网地址或手机心跳都不能解除删除门禁。
- 配置 NAS 后，Authority 写入 `.partial` 压缩包，执行压缩流校验、完整安全解包、文件清单、关联 Evidence 哈希对账和 Git bundle 克隆恢复；随后原子改名并写 SHA-256/SQLite 回执。只有这份回执存在，才执行 `git worktree remove`、删除专属分支和 `git worktree prune`。
- MacBook 回落运行产生的隔离 worktree 可以执行 `worktree send`。该命令强制 location proxy 使用 Tailscale，把压缩包送到 Mac mini；Mac mini 在本地写入 NAS并完成同样的恢复验证，返回匹配回执后源端才删除。
- 需要外部 GitHub delivery 的 verifier worktree，在 delivery 进入 `merged`/`released` 前不会进入回收候选。

常用观察与恢复命令：

```bash
codex-workbench worktree status
codex-workbench worktree sweep --max-items 10
codex-workbench worktree send <allocation-id> --host macmini
codex-workbench worktree restore <archive-id> --destination <local-path>
```

当前 Codex 会话也可直接调用 MCP 工具 `workbench_worktree_status`、`workbench_reclaim_worktrees` 与 `workbench_restore_worktree`。远程 `send` 没有 direct-SSH fallback：它必须复用已安装的 location-aware profile 并强制 `--force-tailscale`；远端 receipt 缺失或不匹配时保留源 worktree。

### Benchmark 基线、长期校准与快照绑定

Workbench 保留版本化的公开性能证据目录，包括 [OpenAI GPT-5.6 官方评测](https://openai.com/index/gpt-5-6/)、[Terminal-Bench 2.1](https://www.tbench.ai/news/terminal-bench-2-1)、[SWE-Bench Pro](https://scaleapi.github.io/SWE-bench_Pro-os/) 和 [Humanity's Last Exam](https://labs.scale.com/leaderboard/humanitys_last_exam)。新快照使用 `local-outcomes-only-v2`：外部评测只作为独立 `public_evidence`，不再转换为 Beta 伪样本，也不在模型家族或版本之间借分。
同时可选接入 [Martian AI Frontier](https://aifrontier.withmartian.com/) 公共 JSON。Quality、Consistency、Real Cost 与本地完成率不是同一指标。只有 benchmark/version、指标、harness、推理档位、任务类型、score kind 和单位一致，且模型身份精确匹配时，公开证据才可在声明质量门禁之后提供冷启动次级偏好；重复来源去重，冲突、缺失或比较不完整时弃权。frontier/oracle 不绑定单模型。收集到数据不等于数据可比，也不等于已验证调度收益。

长期运行时，Workbench 从 append-only SQLite `events`/`tasks` 重建 first-pass acceptance、最终 acceptance、返工、时延、吞吐和池利用率，并按 provider/model/Agent name/version/reasoning effort/task/complexity/harness/score kind 精确分桶。fixture、deterministic、verifier、Evidence reuse、缺少 result、缺少 `actual_model` 和不支持 provider 的 terminal attempt 会被排除；`agent_version=unattested`、非零/未知进程退出、`blocked` 与 `indeterminate` 只记作 unresolved，不进入模型质量成功/失败分母。Beta 只由这些本地结果更新，冷启动参数明确标为非经验 policy prior。声明能力分数与实测概率不混排：声明分数决定质量门禁和等价区间，本地经验只在可比且有样本的候选组中作次级排序，未知不冒充零分。账本是观察数据，尚无细分 `failure_origin` 或随机对照，因此不能据此声称因果提速。

创建任务时，当前 performance snapshot、能力目录 digest 和 policy version 会固定进 TaskContract/NodeSpec；刷新或升级不会重路由已运行任务。快照是可重建的派生缓存，不是第二份状态真相；`performance refresh` 只读本地账本与缓存，无模型调用和登录操作。

可选的 Codex Radar 集成复用上游 `WineChord/codex-radar` 同步契约，但采集与缓存由通用
`codex_radar_provider` 承担。个人自用 receipt 必须同时有 `consented`、
`local_operator_consent`、`public-json` 和 `accepted_at`；这不是站方授权。上游
`current.json` 仍声明完整 API/衍生集成需站方授权。有效缓存时，Workbench 只接纳精确
model + reasoning effort，并保留原指标、样本量和新鲜度；这些记录不再形成等效弱样本，
缺少可比测量条件时只供参考，超过 31 天停止影响路由。IQ 只保留为元数据，绝不转换成通过率。该
Provider 可由 DSH 后续独立安装和消费，本版本没有修改 DSH。

### 本地预训练需求分类（可选）

[OpenSquilla 适配器与离线安装指南](docs/opensquilla-advisor.md) 使用固定源码和真实模型权重，在本地对子任务需求分为 c0–c3。它只在新 DAG 编译时批量运行；已有任务不重路由。建议可以提高复杂度底线，但不能降低规划器声明的底线，也不能绕过模型角色、质量、配额或推理档位限制。未配置、权重缺失或分类失败时保留原策略，并明确记录不可用原因。

这是预训练需求分类器的 `compatible subset`，不包括 OpenSquilla 网关、集成推理或自学习训练器；分类置信信号不是任务成功概率。安装器只使用本地离线 wheelhouse，保存原安装备份和实际分类回执，不自动重启服务。真实成功率和返工率仍由 Workbench 本地结果账本逐步校准；尚未证明真实交付周期或单位配额收益。

常用观测入口：

```bash
codex-workbench performance status
codex-workbench performance show
codex-workbench performance refresh       # 只重放本地账本，不调用模型
codex-workbench radar consent-personal-use # Authority；只写本地 personal-use receipt
codex-workbench radar status               # 只读本地 consent 与缓存状态
codex-workbench radar show                 # 只读 last-known-good 快照
codex-workbench radar refresh              # Authority；无 consent/授权时零网络
codex-workbench ai-frontier status         # 只读 SQLite/LKG 与 consent 状态
codex-workbench ai-frontier show           # 只读当前快照与 routing boundary
codex-workbench ai-frontier refresh         # Authority；无 consent 时零网络
curl -H "Authorization: Bearer $WB_TOKEN" http://127.0.0.1:8766/api/performance
curl -H "Authorization: Bearer $WB_TOKEN" http://127.0.0.1:8766/api/radar
curl -H "Authorization: Bearer $WB_TOKEN" http://127.0.0.1:8766/api/ai-frontier
curl -H "Authorization: Bearer $WB_TOKEN" http://127.0.0.1:8766/api/scheduler
```

`/api/scheduler` 展示每个 lane 的依赖就绪 `queue_depth`、`dependency_blocked`、`inflight`、`started`、`accepted`、`failed`、`blocked`、`indeterminate`、`retry`、`rework`、`busy_seconds`、`utilization` 和 `accepted_per_hour`，以及 quota pool 状态；分母为零时返回 `N/A`。历史事件的 lane/pool 以持久 NodeSpec 为准，事件载荷只兼容没有 NodeSpec 的旧记录。first-pass/final acceptance 和 duration 由 performance ledger 单独记录，不是 scheduler API 的字段。Codex/Spark 的订阅剩余配额在上游没有可证明接口时保持 `N/A`，不会由 Workbench 编造余额。

### 版本化能力目录与质量优先路由

Authority 会在安装时建立第一份能力目录，并由独立 LaunchAgent 每 6 小时执行一次无模型、无登录的被动刷新。目录记录 Codex/Claude Code CLI 版本、模型选择 ID、角色、任务类型、推理档位、工具特性以及质量/成本/时延/并发策略。功能未变化的刷新复用现有 generation；只有真实能力或 Agent 版本变化才产生新 generation。

新任务将当前 `catalog_id` 与 digest 固定进 TaskContract 和每个节点。硬门禁先检查角色、工具、版本、Claude 配额与并发，再按“验收质量优先、角色适配其次、成本和时延随后”的确定性顺序选路。未知或弃用模型只展示、不调度；新的 Luna/Terra/Spark 家族可继承受限 Worker 策略，新的 Sol 版本不会自动取得规划与最终验收权限。旧任务始终按其已固定的目录运行，目录可查看 diff、显式激活或回滚。

### Code-as-Harness：把开发规则变成可验证契约

Workbench 将 `code-as-harness/v1` 投影到 Codex 与 Claude Code 的受控路径，并把验证层级、作用域、并行策略和 Evidence reuse 规则记录到每个任务契约中。

- `L0`：只读或文档变更，检查相关内容与 diff。
- `L1`：局部改动，运行一个能证伪改动的聚焦检查。
- `L2`：跨文件或共享接口，运行受影响测试与必要的构建/类型检查。
- `L3`：发布、迁移、持久化或显式全量验收，稳定后运行一次项目要求的完整门禁。

这不是对外部平台插件执行状态的宣称。`harness health` 只报告它实际观察到的二进制、Skill、策略块和受控注入状态；不会把文件存在误写成模型调用或登录成功。

### Research 与 Archify：需要时强制，但不把工具当真相

- **Research**：规划器会为架构/探索、高复杂度决策、论文与上游复现、选型迁移、性能基准、兼容性或安全等场景注入 Research 工作流。普通、边界明确的实现不会被迫做研究；深度并行研究需要明确请求。
- **Archify**：仓库固定携带 Archify 的 stable core，并把它用于架构、设计、审核和需求类任务的 typed JSON IR、渲染与 receipt。渲染/Schema 通过只证明工件约束通过，不证明架构事实、运行时因果或推理质量；语义和视觉结论仍需要外部或人工 Evidence。

详情请见 [原设计忠实度矩阵](docs/fidelity-matrix.md) 与 [Archify 集成保真矩阵](docs/archify-fidelity-matrix.md)。
Radar 的 consent、SQLite 离线缓存、保守权重与未来 DSH 消费协议见
[Codex Radar 通用 Provider 与 Workbench 集成](docs/codex-radar-integration.md)。
AI Frontier 的独立 Provider、允许的 endpoint、72 小时采集窗口、personal-use consent、
SQLite/LKG、字段语义和多源调度算法见 [AI Frontier 集成文档](docs/ai-frontier-integration.md)。

### Claude 配额保护

Claude 没有被当作无限资源。Workbench 以被动方式读取兼容的本地订阅用量显示；当认证、版本、用量格式或快照新鲜度无法证明时，Claude 调度会关闭并由 Codex 接管。

| 配额状态 | 调度行为 |
| --- | --- |
| 余额充足 | 在共享并发限制内使用合适的 Claude Worker。 |
| 接近保留阈值 | 限制可用模型与并发，优先保留可预测的剩余额度。 |
| 保护区、未知或认证失败 | 不启动新的 Claude Worker；同一 attempt 转交 Codex，不循环重登。 |

20% 是持续保留目标；30% 是新任务 admission guard，25% 是硬停线。由于上游 CLI 暴露的是显示文本而非可强制消费上限，系统不会声称能从代码层面保证单次模型回合绝不越线。有关可证明边界，见 [忠实度矩阵](docs/fidelity-matrix.md)。

## 快速开始

> Workbench 当前面向熟悉 macOS、SSH、Git 与 Codex 的操作者。请先阅读完整的 [AI 安装与配置指南](docs/AI_INSTALL.md)，再让 AI 或人工执行安装步骤。

### 前提条件

- 一台可常驻运行的 **Mac mini**（Authority）和一台 **MacBook**（cockpit），均为 macOS。
- Python 3.11+、Git、已认证的 Codex CLI；Claude Code 是可选项。
- MacBook 到 Mac mini 的 SSH 连通性；使用远程 cockpit 时推荐 Tailscale。
- Node.js 18+（仅在需要 Archify 渲染/验证时）。
- 已阅读并接受：这是需要由操作者维护的自托管开发控制面，不是多租户托管服务。

当前完整安装方式是从受信任的 Git tag 或提交检出源码后运行仓库安装器。Python wheel/sdist 尚不包含 Archify、Skills、安装脚本和 launchd 资源，因此不要用 `pip install codex-workbench` 代替本指南的源码安装流程。

### 最小安装路径

在两台机器上检出同一受信任版本的源码。先在 Authority 上执行 dry-run；它不会写入文件、启动服务、连接 SSH 或注册 MCP。

```bash
git clone <repository-url> codex-workbench
cd codex-workbench

# Mac mini: inspect the Authority installation plan first
scripts/python-runtime scripts/install-macos.py \
  --nas-archive-root <MOUNTED_NAS_WORKTREE_ARCHIVE_ROOT> \
  --dry-run
```

确认计划和本机前提条件后，按照 [AI 安装与配置指南](docs/AI_INSTALL.md) 在 Mac mini 安装 Authority，并在 MacBook 安装 cockpit/MCP：

```bash
# MacBook: inspect the client plan first
scripts/python-runtime scripts/install-macbook-client.py \
  --authority-ssh-alias <authority-host> \
  --authority-lan-host <HOME_LAN_HOST> \
  --authority-lan-port <HOME_LAN_SSH_PORT> \
  --authority-tailnet-host <TAILNET_HOST> \
  --tailscale-socket <OPTIONAL_USERSPACE_TAILSCALED_SOCKET> \
  --home-network <HOME_CIDR> \
  --home-network <HOME_CIDR_BACKUP> \
  --ssh-transport location-aware \
  --dry-run
```

Authority 安装器不会创建全局 PATH 链接。安装后用实际运行目录中的二进制进行最小健康检查：

```bash
export WB_STATE_ROOT="$HOME/Library/Application Support/Codex Workbench"
export WB_AUTHORITY_BIN="$WB_STATE_ROOT/app/bin/codex-workbench"
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" doctor
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" harness health
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" capabilities status
codex mcp get codex-workbench
```

### 从 Codex 会话进入

安装并信任 `wb` 插件后，在新会话或已有会话中输入：

```text
wb
```

插件会请求将当前会话与受控代码上下文绑定到 Mac mini Authority。安装或 Hook 内容变更后，Codex 需要用户在 `/hooks` 中审核该 Hook；这是 Codex 的显式信任步骤，不会被 Workbench 绕过。

`wb` 插件随仓库内的 [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json) 与 [`plugins/codex-workbench/`](plugins/codex-workbench/) 发行，可通过 Codex Marketplace 安装。首次使用仍需显式添加该 Marketplace source、安装插件并审核 Hook；这不是无需确认的一键启用。完整的安装、升级与回退方式以 [AI 安装与配置指南](docs/AI_INSTALL.md) 为准。部分 Codex 版本不接受自定义顶层 `/WB`，因此默认入口是小写的 `wb`；`$WB` 和从 `/skills` 选择 `WB` 也可作为替代入口。

### 从手机 Codex App 进入

在 Mac mini 上先检查并启用原生 Remote Control：

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile status
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile enable --dry-run
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile enable
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile pair
```

`mobile enable` 只安装 Workbench plugin/MCP，不启动第二个 CLI app-server。原生 Remote host 和二维码必须由 Mac mini 的 ChatGPT/Codex 桌面 App 独占管理：打开 `Settings > Connections > Control this Mac or PC > Set up or Add`，显示二维码后用手机扫描并批准。`mobile pair` 只返回这条桌面操作路径，不生成或保存配对码。之后手机发出的任务由 Mac mini 上的 Codex 会话执行；在会话中输入 `wb` 即进入同一 Workbench Authority。自动化测试只能证明接线状态，真机可见、可发任务仍须一次真实手机旅程验收。

如果在家网段且 MCP 链路可达，客户端会优先走家庭 LAN；否则回落到 Tailscale 原生 SSH TCP Serve（默认端口 10022）。判断失败后会产生 `degraded` receipt 并走 outbox，下一次同一会话重试 `wb` 时继续同步。该策略不是按国家/区域判断，不会修改系统网络与 Tailscale 配置，也不会自动发起登录。

## 一次任务会怎样流转

```text
natural-language objective
        │
        ▼
Sol planner ──► TaskContract + scope-aware DAG
        │
        ├── independent nodes run in isolated worktrees
        │       ├── deterministic checks
        │       ├── Codex workers
        │       └── optional Claude Code workers (quota-gated)
        ▼
independent Sol verifier
        │
        ├── accepted: required diff, checks, verdict and Evidence agree
        └── needs_fix / needs_approval / blocked: durable state, not a hidden retry
```

示例：从命令行提交一个边界明确的任务。将 scope 和验收命令替换为你的仓库实际边界。

```bash
codex-workbench request \
  "修复解析器并补齐回归测试" \
  --repository "$PWD" \
  --allowed-scope src/parser \
  --allowed-scope tests/parser \
  --acceptance-command "python -m unittest tests.test_parser" \
  --task-type debugging \
  --complexity standard \
  --verification-tier L2 \
  --queue
```

## CLI 参考

<details>
<summary>展开查看常用命令</summary>

```bash
# Authority lifecycle and non-model health checks
codex-workbench init --authority
codex-workbench serve
codex-workbench doctor
codex-workbench doctor --require-restart-ready
codex-workbench harness health

# Tasks, events and durable approvals
codex-workbench task list
codex-workbench task get <task-id>
codex-workbench events --task-id <task-id>
codex-workbench approval list
codex-workbench task steer <task-id> "<next-attempt instruction>" --expected-revision <n>
codex-workbench task priority <task-id> <priority> --expected-revision <n>

# Acceptance, routing and quota observation
codex-workbench acceptance
codex-workbench quota show
codex-workbench capabilities status
codex-workbench capabilities diff
codex-workbench capabilities refresh --activate-safe
codex-workbench capabilities rollback
codex-workbench performance status
codex-workbench performance show
codex-workbench performance refresh

# Recoverable worktree lifecycle
codex-workbench worktree status
codex-workbench worktree sweep --max-items 10
codex-workbench worktree send <allocation-id> --host macmini
codex-workbench worktree restore <archive-id> --destination <local-path>

# Tune the logical Spark lane inside the shared executor
codex-workbench serve --max-workers 8 --spark-workers 4

# Native Codex mobile Remote Control
codex-workbench mobile status
codex-workbench mobile enable --dry-run
codex-workbench mobile pair

# Source synchronization and explicitly authorized GitHub delivery
codex-workbench sync github --repository <repository-path> --branch <branch>
codex-workbench deliver <task-id> --base-branch <branch>
```

`deliver` 只会处理契约中明确允许外部写入、且已经由 verifier 接受的任务。网络不确定时会记录 `indeterminate`，而不是猜测重放写操作。

</details>

## 隐私、网络与运行边界

- Workbench 是可复用的自托管开发控制面；仓库源码不包含运行账本、会话、认证文件、配额快照或控制令牌。
- 受控执行器使用既有的 Codex/Claude 原生订阅登录态；不读取或转发 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`，也不以 API key 作为订阅失败的回退。
- Authority 默认只监听本机回环地址。远程 cockpit 通过你配置的 SSH/Tailscale 通道访问，而不是把 SQLite 服务公开到互联网。
- MacBook 和离线端不创建第二个 Authority；它们只能读取、控制或准备后续同步。
- 该项目仅支持 macOS；手机 Remote Control 的接线已经实现，但未完成真实手机配对就不能声称手机旅程通过。页面关闭后的 Web Push 仍在 [backlog](docs/backlog.md)。

## 状态与文档

源码版本/合同为 `1.13.4`；升级前正式服务基线为 `v1.13.3 / 5ec3448`，本文不声明任何用户安装或运行主机已完成升级。1.13.4 为依赖节点的受控脏工作树恢复新增显式 `task resume-blocked-worktree --preserve-untracked`：只有调用方明确选择、文件集与收据精确一致、且每个文件同时落在任务及节点 write scope 内时，才会把合法未跟踪文件纳入内容寻址补丁并在干净 a2 复原；a1 永远不执行 `git add` 或写入。v3 收据固定该文件集和合并补丁，恢复后仍以字节级补丁、声明验收和 scope 校验替换 a1。离线 pnpm 材料化同时显式关闭 pnpm 11 的 release-age 查询：只使用本地缓存，缓存不足时立即失败而不在 registry 重试。1.13.3 修复依赖节点的受控脏工作树恢复：恢复器会从原 a1 的不可变 dependency-input 收据重建已验收祖先补丁闭包，再仅捕获并重放该 worker 自己的差异；不会把上游已验收改动误判为该 worker 的写入。1.13.1 新增受控的脏工作树恢复；1.12.1 修复执行恢复；1.12.0 增加 Astra 显式控制面选择、Claude 精确型号映射与分来源性能清单。默认控制面仍为 Sol；Astra 性能缺失保持 N/A。测试通过、分类可运行或路由发生变化，都不能替代真实交付周期与单位配额收益证据。

该版本将 Git 工作树分配和执行路径解析到真实物理目录。下游节点先纳入已 `accepted` 的祖先补丁，再仅导出本节点新增的差异。`task reconcile-archify` 和 `task retry-blocked` 提供只读 `--dry-run` 提议；操作人应先审阅提议，再以当前 revision/attempt 正式授权恢复。恢复不重建任务、不修改冻结 base，也不把人工无副作用确认冒充自动验证。

- [AI 安装与配置指南](docs/AI_INSTALL.md) — 面向 AI 操作者和人工复核者的部署、连接、回退与验收步骤。
- [原设计忠实度矩阵](docs/fidelity-matrix.md) — 已实现、部分实现和需真实外部 Evidence 的边界。
- [Archify 集成保真矩阵](docs/archify-fidelity-matrix.md) — 上游来源、适配范围及不可夸大的结论。
- [Codex Radar 集成](docs/codex-radar-integration.md) — 通用 Provider、personal-use consent、SQLite 断网缓存、Workbench 先验与未来 DSH 消费合同。
- [AI Frontier 集成](docs/ai-frontier-integration.md) — 自动采集白名单、SQLite LKG、字段语义、质量等价带算法与未来通用消费合同。
- [型号与效果验收](docs/model-performance-evaluation.md) — Astra、精确型号绑定、全模型清单和离线路由对照。
- [Backlog](docs/backlog.md) — 已明确后置的真实外部 Evidence 与通知工作。

## 项目边界

Codex Workbench 是独立项目：它不属于 DSH、Solar 或 AI4Research，也不要求这些项目存在或运行。其目标不是替代你的编辑器、Git 平台或 Agent 工具，而是为它们之间的长期任务、模型路由、并行执行、配额保护和验收 Evidence 提供一个可恢复的控制面。
