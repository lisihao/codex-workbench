# Codex Workbench

当前候选版本：`1.1.3`。

独立于 DSH 的个人开发基础设施。核心是常驻服务与统一账本，不是 Codex Skill，也不是 Codex 插件；后续插件若需要，只作为连接同一账本的薄入口。Mac mini 持有唯一任务账本、后台执行器、Git worktree 和验收证据；本阶段由 MacBook 通过 Tailscale 私网消费同一份状态。手机接入按用户最新决定移入 [`docs/backlog.md`](docs/backlog.md)，不再阻塞当前交付。

核心约束：

- Codex 是唯一用户入口，Claude Code 只是可选后台 Worker。
- 任务、节点、事件、配额快照和审批持久化到 SQLite。
- DAG 节点只有依赖完成且读写作用域不冲突时才能并行执行；父子路径按包含关系互斥，read/read 可并行。
- Worker 完成不等于任务完成，必须经过依赖全部 Worker 的独立 Codex Sol verifier；正式 planner 与 verifier 均固定为 Sol，Claude 只可作为 Worker。退回会自动携带反馈进入 Worker 修复与新一轮 Sol 验证。
- Claude 配额未知、认证未知或剩余不高于 25% 时禁止启动新任务，至少保留 20%。
- 不读取或转发 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`，只允许产品订阅登录态。
- 安装器为无人值守进程建立独立 `CODEX_HOME`；它只软链接用户现有 `auth.json`，不会加载个人 skills、会话、模型缓存或全局配置。
- 已通过的验证 Evidence 在声明输入闭包、命令和运行时身份不变时复用；实现型 Worker 不复用。
- `coordinator.lock` 是进程生命周期的排他权威租约；authority 账本显式绑定规范化 macOS `IOPlatformUUID`，hostname 只用于显示。SQLite coordinator epoch 与逐节点 lease epoch 会拒绝旧进程或不同机器的迟到提交。MacBook、手机和第二个服务进程只能读取或控制 Mac mini 上的同一个协调器。
- 非 fixture 任务只有在契约要求的 diff、测试日志与 verifier verdict 全部存在时才能进入 `accepted`。
- Claude 因认证或保护配额不可用时，同一 attempt 只调用一次 Codex 订阅算子接管，并记录 `node.routed`；不会重启 Claude 或创建第二个任务。
- Claude 调度严格执行四区策略：`>40%` 绿区共享 2 个容量单位（Sonnet 占 1，Opus/Fable 占 2）；`30%–40%` 黄区仅允许 Sonnet 1；`>25%–<30%` 红区以及 `≤25%` 保护区直接路由 Codex。达到 Claude 容量或上一 Claude 节点完成后尚无更新配额快照时，空余总 Worker 槽由 Spark/Luna/Terra 接管，不闲置队列。
- SQLite v7 分开保存原始节点契约、`effective_executor/effective_model`、coordinator/node lease epoch 与运行时任务优先级，并在迁移时清除旧 Evidence ABI 缓存；短指令使用独立 append-only 记录，不修改原任务契约或 scope。
- Evidence 引用在结算和复用时都验证文件存在性与 SHA-256；损坏缓存不会继续复用。
- 同一路径 scope 仅在同一规范化 repository identity 内互斥，不同仓库不会发生伪冲突；微任务使用独立 Codex Spark 池并按 Spark → Luna → Terra → Sol 有界升级，标准任务从 Luna → Terra → Sol 升级。
- GitHub CI 支持自动 push/PR 与显式 `workflow_dispatch`；纯 Python 门禁使用 Ubuntu 双版本矩阵，macOS 真实性由固定标签的 Mac mini 安装验收覆盖。
- A1 不再永久写死为 pending：MacBook 心跳间隔进入 Mac mini 同一条 Evidence 账本。A2 手机真机接入显示为 `deferred backlog`，不参与当前完成门禁。A12 可以导入并验证 PPT/PDF 工件，但在缺少可核验 Claude export/receipt 与配额快照关联时仍保持 `pending`，等待人工验收。
- 不确定执行会原子生成持久 `approval` 回执；手机控制面以任务阶段、阻塞原因和下一步为主视图，可带 task revision 明确选择重试、失败或取消。重复提交同一决策保持幂等，冲突决策 fail loud。
- MacBook/手机可以 revision-fenced 地调整 `-10..10` 任务优先级，或补充最多 500 字的后续 attempt 短指令；调度器按优先级选择 ready 节点，Evidence 复用 key 包含短指令。
- 面板从事件账本投影完成、阻塞、审批、路由和协调器提醒；用户可启用页面打开期间的浏览器前台通知。真正的页面关闭后台 Push 尚未启用，不把它冒充已交付能力。

```bash
scripts/python-runtime -m codex_workbench init --authority
scripts/python-runtime -m codex_workbench serve
scripts/python-runtime -m codex_workbench doctor
scripts/python-runtime -m codex_workbench doctor --require-restart-ready
```

从旧版仅记录 hostname 的 authority 配置升级时，服务会 fail-closed；必须在权威 Mac mini 上显式重新运行 `init --authority`，确认将现有账本绑定到当前 Platform UUID。系统不会把启动时所在机器静默认作旧账本 owner。

`scripts/python-runtime` 会先使用 `CODEX_WORKBENCH_PYTHON` 或项目 runtime，再按固定顺序检查 Homebrew Python 3.11+；不会使用系统 Python 3.9。需要固定解释器时设置绝对路径，例如 `CODEX_WORKBENCH_PYTHON=/opt/homebrew/bin/python3.13 scripts/python-runtime -m codex_workbench doctor`。

默认数据目录为 `~/Library/Application Support/Codex Workbench`，默认只监听 `127.0.0.1:8766`。远程访问由 Tailscale Serve 代理，服务自身不开放公网端口。

在任何计划内整机重启前必须运行 `doctor --require-restart-ready`。它会同时验证 FileVault、自动登录、用户 LaunchAgent、Tailscale、断电自启和系统睡眠策略；任一项不满足就拒绝把该机器视为无人值守可恢复。FileVault 开启时，必须由本地用户单独授权一次 `fdesetup authrestart` 或接受重启后的本地解锁，Workbench 不保存系统密码。

## 自然语言任务入口

`request` 先让 Codex Sol 在只读沙箱中编译任务契约和 DAG，再把节点交给后台协调器。Claude 登录或配额无法证明时，Planner 只会使用 Codex。

```bash
codex-workbench request \
  "修复解析器并补齐回归测试" \
  --repository /Users/example/Projects/example \
  --allowed-scope src/parser \
  --allowed-scope tests/parser \
  --acceptance-command "python3 -m unittest tests.test_parser" \
  --queue
```

默认控制面是 `gpt-5.6-sol` 规划与独立验收。`model-routing-v2` 将低复杂度、边界明确的微任务交给独立池 `gpt-5.3-codex-spark`；在 Claude 认证与配额均被 producer 证明时，标准实现、调试、测试、文档和探索优先使用 Sonnet，高复杂度、架构与审核优先 Opus，再尝试 Fable，Sonnet 作为后备。当前 Claude `/usage` 没有 Fable 专属配额行，因此 Fable 受五小时与全模型周池约束；若未来 producer 暴露独立 Fable 池，再叠加该池门禁。Claude 不可用、达到并发容量或缺少新配额快照时由 Spark/Luna/Terra 填充执行槽。每个执行节点获得独立分支和 worktree，最终 Sol verifier 只读取组合后的 patch，不与 Worker 共用上下文。

路由是任务契约的一部分，而不是运行时猜测。新任务默认使用 `model-routing-v2`；旧账本中的 `model-routing-v1` 继续保持原有 Codex-first 语义，不会被静默重解释。Sol 始终负责规划和最终验证，Claude 只能作为受范围约束的 Worker。CLI 与 Codex MCP 都支持同一组控制字段：`task_type`、`complexity`、`parallelizable`、`claude_allowed` 和正数 `task_points`。

```bash
codex-workbench request \
  "审查跨模块架构并给出可验证修复" \
  --repository /Users/example/Projects/example \
  --allowed-scope src \
  --task-type architecture \
  --complexity high \
  --task-points 3 \
  --queue
```

## Codex 原生入口

MacBook 安装器会把 `codex-workbench` 注册成 Codex stdio MCP。MCP 默认通过现有 `macmini` SSH/Tailscale 通道在 Mac mini 启动，也可用 `--authority-ssh-alias <SSH别名或user@host>` 指定其他权威端点；它不复制 SQLite，也不依赖 DSH。Codex 可直接使用十一个工具：提交自然语言任务、同步 GitHub、列出/查看任务、控制任务、列出/决策持久审批、读取事件、读取 Evidence Artifact、读取当前验收报告与 backlog，以及在契约授权后执行 GitHub 交付。

```bash
scripts/python-runtime scripts/install-macbook-client.py
codex mcp get codex-workbench
```

安装器默认使用 `--ssh-transport auto`：当 SSH 配置解析出的 authority 地址位于 Tailscale `100.64.0.0/10` 时，驾驶舱隧道和 MCP 会复用 `ssh -G` 中完整的用户态 Tailscale socket，通过 tailnet-only TCP Serve `10022` 连接 Mac mini 原生 sshd。认证由普通 SSH key 完成，不依赖会周期要求网页复核的 Tailscale SSH。普通 SSH 主机继续使用系统数据路径；`tailscale-userspace` 只保留为显式 legacy 选项。

## GitHub 与 Tailscale 同步

GitHub 是代码主同步通道。Mac mini 只对干净工作树执行 fast-forward：

```bash
codex-workbench sync github \
  --repository /Users/example/Projects/example \
  --branch main
```

尚未推送的 MacBook 增量使用 Git bundle 经 SSH/Tailscale 流式送达，导入到 `refs/workbench/increment/*`，不会移动 Mac mini 当前分支，也不会复制活跃 `.git`、`node_modules` 或构建目录：

```bash
codex-workbench sync send \
  --repository /Users/example/Projects/example \
  --base-ref origin/main \
  --remote-repository /Users/example/Projects/example \
  --ref-name macbook/task-123
```

随后用 `request --base-sha refs/workbench/increment/macbook/task-123` 固定该增量作为 TaskGraph 基线。

## 授权 GitHub 交付

只有 `external_write_permission=true` 且已由 verifier 接受的任务才能进入交付。显式 `deliver` 命令使用持久 Receipt，依次完成 integration branch、PR、CI，并按参数决定是否 merge/release；网络超时进入 `indeterminate`，不会自动猜测重放。

```bash
codex-workbench deliver TASK_ID \
  --command-id delivery-TASK_ID-v1 \
  --base-branch main \
  --merge --release-tag v1.2.3
```

## Claude 配额保护

Claude 没有稳定的官方 CLI 用量接口，因此系统不会猜测余额。v1.1.3 的被动 quota sidecar 只是**精确锁定 Claude CLI `2.1.239` 的 `/usage` display-text 兼容实现，不是官方 quota API**：它读取 `auth status --json`（兼容未登录时携带有效 JSON 但退出码为 1），再以无会话持久化的 `/usage` 显示文本取样。安装器显式启动一个每分钟取样的常驻 watcher，避免无人值守 Mac mini 的 GUI launchd domain 处于 `on-demand-only` 时把 `StartInterval` 永久挂起；它既不启动 Claude 工作回合，也不使用 API key。

sidecar 遇到明确 `loggedOut` 时写入 `auth_ok=false` 的失败闭锁快照；已登录但 CLI 版本未知、显示文本不符合锁定语法、认证/命令失败时也会先原子替换旧余额为不可用快照，记录本轮错误并在下一分钟继续，避免旧余额继续开闸；非预期进程故障才退出并由 launchd 重启。显示为已使用 `U%` 时，写入的安全剩余下界是 `max(0, 99 - U)%`，不会把向下取整后的显示值当作精确余额。只有同时携带 `producer=codex-workbench.claude-quota`、schema `1`、Claude `2.1.239`、来源 `claude-cli-usage-text-v1` 和 `native-subscription` 认证的兼容采样，才能启动正式 Claude 调度、进入 A6/A7、A8/A12 或单位配额产出指标；旧的手工导入只保留作观察数据，`quota set` 不能声明这组 provenance。这不是已完成的登录态生产验证，真实登录态旅程仍待外部验收。

可以手工运行同一被动采集器（正式安装会创建常驻的一分钟 watcher）：

```bash
codex-workbench quota collect-claude \
  --claude-binary /opt/homebrew/bin/claude \
  --output "$HOME/Library/Application Support/Codex Workbench/claude-quota.json"
```

正式 Claude 调度只接受上述 sidecar 产生的完整 provenance。以下 `quota set` 仅用于导入旧记录、人工观察或迁移验证；它不会打开正式 Claude 调度：

```bash
codex-workbench quota set \
  --auth-ok --auth-method native-subscription \
  --five-hour 80 --weekly-all 70 --weekly-sonnet 65 \
  --five-hour-window 2026-08-26T00 --weekly-window 2026-W35 \
  --source settings-usage
```

任何池未知或快照超过 15 分钟时禁止新 Claude 任务；该节点在同一 attempt 内路由给 Codex，不重复调用 Claude。系统不会读取或转发 `ANTHROPIC_API_KEY`。20% 永久作为 PPT 目标保留池，30% 是新任务 admission guard，25% 是硬停线；每个 Claude 节点结束后还必须等到更新的配额快照，才能再启动新的 Claude 节点。该机制限制连续透支，但被动显示接口无法给单次回合设置可执行消耗上界，因此“任何单回合都绝不跨过 20%”仍需真实窗口证明，不能由代码静态宣称。具名五小时和周窗口让保留率成为可验证 Evidence，而不是根据采样时间猜测。

远程面板同时显示“每 10% Claude 配额产生的 accepted 加权任务点数”。统计只使用同一真实具名窗口内的订阅配额快照和任务 `task_points`；fixture/test/simulation 数据、跨窗口样本以及配额反向增加的窗口不会进入指标。

长期运行时可将 Claude 设置页导出的本地 JSON 文件交给无模型调用的刷新 adapter；服务按周期读取，内容未变化时不重复写账本：

```bash
export CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE="$HOME/Library/Application Support/Codex Workbench/claude-quota.json"
```

文件字段与 `quota set` 一致：`observed_at`、`auth_ok`、`auth_method`、三个或四个 remaining 百分比及可选具名窗口。Adapter 不使用 API key，也不启动 Claude 回合。

调度器按所有适用配额池的最低剩余值执行：

| 区域 | 余额 | 新 Claude 调度 |
|---|---:|---|
| green | `>40%` | 共享容量 2：Sonnet 占 1，Opus/Fable 占 2；不可叠加成 3 个 Claude |
| yellow | `30%–40%` | 禁止 Opus/Fable；Sonnet 最多 1 |
| red | `>25%–<30%` | 不启动 Claude，转 Codex |
| protected | `≤25%` | 不启动 Claude，转 Codex 并保护至少 20% |
| unknown/auth unavailable | N/A | 不启动 Claude，转 Codex |

## 追加式 legacy Evidence 补证

旧任务的 A10 缺口不能通过改写 `tasks`、`nodes` 或既有 `events` 修复。`acceptance remediate-legacy` 将 manifest 原文保存为 ArtifactStore 内容地址工件，复用 `command_receipts` 的 `command_id + request_hash` 幂等性，并只追加一条 `acceptance.evidence_remediated` event；旧账本行与 SQLite schema 均不变。

```bash
codex-workbench acceptance remediate-legacy \
  --manifest /path/to/legacy-evidence-remediation.json \
  --command-id remediate-legacy-TASK_ID-v1
```

读取 A10 时，系统会从 ArtifactStore 重新读取 manifest，并严格复核 source task 的 contract hash、base SHA、首尾与节点级 event cursor/hash、attempt，以及旧结果中原有的 artifact ref；普通或伪造的 generic event 不会成为补证。只有原结果已经原生携带 `result_kind=verifier`、`verdict=accepted`、checks 和 evidence 的历史 Codex Sol 节点，才可直接使用；deterministic verifier 或缺少这些原生字段的旧 Sol 节点都必须另建一个独立、持久、accepted 的 Workbench review task。该任务合同绑定原 task ID/contract hash/repository/base SHA，并由一个 accepted deterministic/Codex review worker 实际物化和检查原 patch，再由依赖该 worker 的真实 Codex Sol verifier 产生 transcript/test/verdict/receipt/evidence；补证逐一绑定 review 事件链和原生 result，不能由 manifest 补写 verdict。

## 当前验收面

`codex-workbench acceptance`、MCP `workbench_acceptance_report`、`/api/acceptance` 与远程面板都从同一份 SQLite 账本计算验收状态。当前门禁包含 A1、A3–A12；只有全部具有真实 Evidence 时命令才返回成功。A2 手机真机接入按用户决定进入 `backlog` 数组，不计入 `complete`。MacBook 离线八小时、Mac mini 整机重启和 Claude 网页 PPT 等外部旅程在没有回执时保持 `pending`，不会被 fixture 冒充完成。

```bash
codex-workbench acceptance
```

## 远程面板

Mac mini 正式服务只绑定 loopback。正式安装时指定实际运行的 userspace tailscaled socket；安装器会持久配置 HTTPS 驾驶舱与原生 SSH TCP Serve：

```bash
scripts/python-runtime scripts/install-macos.py \
  --tailscale-socket /var/run/tailscale/tailscaled.sock
```

MacBook 读取 `/api/snapshot` 与 `/api/events?after=<cursor>`；断线只产生 stale 投影，不会启动第二个协调器。控制操作需要 `codex-workbench token` 返回的本地令牌。手机 UI 与真机回执代码保留，但当前不部署、不验收。

```bash
codex-workbench approval list
codex-workbench approval decide <approval-id> --decision retry --expected-revision <revision>
codex-workbench task priority <task-id> 5 --expected-revision <revision>
codex-workbench task steer <task-id> "后续 attempt 保留公开接口" --expected-revision <revision>
```

如果 MacBook 的 Shadowrocket Fake-IP 覆盖了 Tailscale MagicDNS，安装自恢复驾驶舱隧道，不修改全局代理规则：

```bash
scripts/python-runtime scripts/install-macbook-client.py
open http://127.0.0.1:18766
```

隧道只转发 Mac mini 的 loopback 工作台端口；MacBook 不启动协调器、不复制 SQLite，断开也不会影响后台任务。长连接使用 `ssh -N`，远端不再驻留 heartbeat shell。独立 Heartbeat LaunchAgent 每五分钟通过同一原生 SSH-key 传输执行一次短命令并立即退出，避免 SSH 断开后产生孤儿循环或虚假在线 Evidence。同一客户端出现至少八小时心跳空窗，且期间有任务 accepted 后，A1 才会通过。

完成一次 Claude 网页 PPT 旅程后，由 Mac mini 本地管理员导入真实工件：

```bash
codex-workbench acceptance attest-a12 \
  --artifact /path/to/slides.pptx \
  --export-receipt /path/to/claude-export-receipt.json \
  --quota-window 2026-W35 \
  --source-session-id claude-web-session-id \
  --note "Claude web completed with the reserved quota pool"
```

只有实际存在、SHA-256 与大小匹配、且文件签名/内部结构确认为 PPT、PPTX 或 PDF 的工件才会被账本接收；改扩展名的 fixture 不会通过。A12 还要求 export receipt 与工件 digest、Claude Web session、具名真实配额窗口一致，并证明所有适用池仍不少于 20%；缺少任一证据都保持 `pending`。

原始设计到当前实现的逐项状态见 [`docs/fidelity-matrix.md`](docs/fidelity-matrix.md)。矩阵中的 `partial` 和 `external-pending` 不会被描述成已完成。

## 状态语义

任务状态为：

```text
inbox → planning → ready → queued → running → verifying → accepted
                                           ├→ needs_fix
                                           ├→ needs_approval
                                           └→ blocked
```

Worker 进程成功只会结算节点。最终任务必须由标记为 verifier 的独立节点返回 `accepted`。协调器重启时仍在运行且无法证明终态的节点进入 `indeterminate`，只能通过显式 `task resolve` 选择 retry、fail 或 cancel。
