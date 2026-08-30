# Codex Workbench

独立于 DSH 的个人开发基础设施。Mac mini 持有唯一任务账本、后台执行器、Git worktree 和验收证据；MacBook 与手机通过 Tailscale 私网查看同一份状态。

核心约束：

- Codex 是唯一用户入口，Claude Code 只是可选后台 Worker。
- 任务、节点、事件、配额快照和审批持久化到 SQLite。
- DAG 节点只有依赖完成且读写作用域不冲突时才能并行执行；父子路径按包含关系互斥，read/read 可并行。
- Worker 完成不等于任务完成，必须经过依赖全部 Worker 的独立 Codex Sol verifier；退回会自动携带反馈进入 Worker 修复与新一轮 Sol 验证。
- Claude 配额未知、认证未知或剩余不高于 25% 时禁止启动新任务，至少保留 20%。
- 不读取或转发 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`，只允许产品订阅登录态。
- 安装器为无人值守进程建立独立 `CODEX_HOME`；它只软链接用户现有 `auth.json`，不会加载个人 skills、会话、模型缓存或全局配置。
- 已通过的验证 Evidence 在声明输入闭包、命令和运行时身份不变时复用；实现型 Worker 不复用。
- `coordinator.lock` 是进程生命周期的排他权威租约；SQLite coordinator epoch 与逐节点 lease epoch 会拒绝旧进程的迟到提交。MacBook、手机和第二个服务进程只能读取或控制 Mac mini 上的同一个协调器。
- 非 fixture 任务只有在契约要求的 diff、测试日志与 verifier verdict 全部存在时才能进入 `accepted`。
- Claude 因认证或保护配额不可用时，同一 attempt 只调用一次 Codex 订阅算子接管，并记录 `node.routed`；不会重启 Claude 或创建第二个任务。
- Claude 调度严格执行四区策略：`>40%` 绿区最多 Opus/Fable 高阶槽 1、Sonnet 2；`30%–40%` 黄区仅允许 Sonnet 1；`>25%–<30%` 红区以及 `≤25%` 保护区直接路由 Codex。
- SQLite v7 分开保存原始节点契约、`effective_executor/effective_model`、coordinator/node lease epoch 与运行时任务优先级，并在迁移时清除旧 Evidence ABI 缓存；短指令使用独立 append-only 记录，不修改原任务契约或 scope。
- Evidence 引用在结算和复用时都验证文件存在性与 SHA-256；损坏缓存不会继续复用。
- 同一路径 scope 仅在同一规范化 repository identity 内互斥，不同仓库不会发生伪冲突；Codex 修复重试按 Luna → Terra → Sol 有界升级。
- GitHub CI 支持自动 push/PR 与显式 `workflow_dispatch`；纯 Python 门禁使用 Ubuntu 双版本矩阵，macOS 真实性由固定标签的 Mac mini 安装验收覆盖。
- A1、A2 不再永久写死为 pending：MacBook 心跳间隔和已登录手机的真实渲染回执进入 Mac mini 同一条 Evidence 账本。A12 可以导入并验证 PPT/PDF 工件，但在缺少可核验 Claude export/receipt 与配额快照关联时仍保持 `pending`，等待人工验收。
- 不确定执行会原子生成持久 `approval` 回执；手机控制面以任务阶段、阻塞原因和下一步为主视图，可带 task revision 明确选择重试、失败或取消。重复提交同一决策保持幂等，冲突决策 fail loud。
- MacBook/手机可以 revision-fenced 地调整 `-10..10` 任务优先级，或补充最多 500 字的后续 attempt 短指令；调度器按优先级选择 ready 节点，Evidence 复用 key 包含短指令。
- 面板从事件账本投影完成、阻塞、审批、路由和协调器提醒；用户可启用页面打开期间的浏览器前台通知。真正的页面关闭后台 Push 尚未启用，不把它冒充已交付能力。

```bash
scripts/python-runtime -m codex_workbench init --authority
scripts/python-runtime -m codex_workbench serve
scripts/python-runtime -m codex_workbench doctor
scripts/python-runtime -m codex_workbench doctor --require-restart-ready
```

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

默认路由是 `gpt-5.6-sol` 规划、`gpt-5.6-luna` 执行、`gpt-5.6-sol` 独立验收。可以用 `--executor-model gpt-5.6-terra` 或其他当前 Codex 订阅模型覆盖执行层。每个执行节点获得独立分支和 worktree，最终 verifier 只读取组合后的 patch，不与 Worker 共用上下文。

## Codex 原生入口

MacBook 安装器会把 `codex-workbench` 注册成 Codex stdio MCP。MCP 默认通过现有 `macmini` SSH/Tailscale 通道在 Mac mini 启动，也可用 `--authority-ssh-alias <SSH别名或user@host>` 指定其他权威端点；它不复制 SQLite，也不依赖 DSH。Codex 可直接使用十一个工具：提交自然语言任务、同步 GitHub、列出/查看任务、控制任务、列出/决策持久审批、读取事件、读取 Evidence Artifact、读取 A1–A12 验收报告，以及在契约授权后执行 GitHub 交付。

```bash
scripts/python-runtime scripts/install-macbook-client.py
codex mcp get codex-workbench
```

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

Claude 没有稳定的官方 CLI 用量接口，因此系统不会猜测余额。只有本地资格审查和三个配额池都已写入时才允许派发：

```bash
codex-workbench quota set \
  --auth-ok --auth-method native-subscription \
  --five-hour 80 --weekly-all 70 --weekly-sonnet 65 \
  --five-hour-window 2026-08-26T00 --weekly-window 2026-W35 \
  --source settings-usage
```

任何池未知或快照超过 15 分钟时禁止新 Claude 任务；该节点在同一 attempt 内路由给 Codex，不重复调用 Claude。系统不会读取或转发 `ANTHROPIC_API_KEY`。20% 永久作为 PPT 保留池，25% 是吸收在途任务的停线。具名五小时和周窗口让跨窗口保留率成为可验证 Evidence，而不是根据采样时间猜测。fixture、test、controlled 或 simulation 来源的快照不会计入 A6/A7 配额验收。

长期运行时可将 Claude 设置页导出的本地 JSON 文件交给无模型调用的刷新 adapter；服务按周期读取，内容未变化时不重复写账本：

```bash
export CODEX_WORKBENCH_QUOTA_SNAPSHOT_FILE="$HOME/Library/Application Support/Codex Workbench/claude-quota.json"
```

文件字段与 `quota set` 一致：`observed_at`、`auth_ok`、`auth_method`、三个或四个 remaining 百分比及可选具名窗口。Adapter 不使用 API key，也不启动 Claude 回合。

调度器按所有适用配额池的最低剩余值执行：

| 区域 | 余额 | 新 Claude 调度 |
|---|---:|---|
| green | `>40%` | Opus/Fable 高阶槽合计最多 1；Sonnet 最多 2 |
| yellow | `30%–40%` | 禁止 Opus/Fable；Sonnet 最多 1 |
| red | `>25%–<30%` | 不启动 Claude，转 Codex |
| protected | `≤25%` | 不启动 Claude，转 Codex 并保护至少 20% |
| unknown/auth unavailable | N/A | 不启动 Claude，转 Codex |

## A1–A12 验收面

`codex-workbench acceptance`、MCP `workbench_acceptance_report`、`/api/acceptance` 与远程面板都从同一份 SQLite 账本计算验收状态。命令仅在十二项全部有真实 Evidence 时返回成功；MacBook 离线八小时、手机真机读取、Mac mini 整机重启和 Claude 网页 PPT 等外部旅程在没有回执时保持 `pending`，不会被 fixture 冒充完成。

```bash
codex-workbench acceptance
```

## 远程面板

Mac mini 正式服务只绑定 loopback。通过 Tailscale Serve 将其映射成 tailnet-only HTTPS 地址：

```bash
tailscale serve --bg --https=10443 http://127.0.0.1:8766
```

MacBook 和手机读取 `/api/snapshot` 与 `/api/events?after=<cursor>`；断线只产生 stale 投影，不会启动第二个协调器。手机窄屏隐藏工程验收明细，优先显示任务阶段、阻塞/下一步和待处理审批。控制操作需要 `codex-workbench token` 返回的本地令牌。

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

隧道只转发 Mac mini 的 loopback 工作台端口；MacBook 不启动协调器、不复制 SQLite，断开也不会影响后台任务。隧道断开后最多每五分钟尝试一次重连，避免 Tailscale SSH 资格失效时形成认证重启风暴。安装器同时每五分钟从 MacBook 向 Mac mini 写入一次服务端时间心跳；同一客户端出现至少八小时心跳空窗，且期间有任务 accepted 后，A1 才会通过。

手机登录控制面并成功渲染真实快照后，页面只在该浏览器会话写入一次 `client.observed` 回执。服务端根据移动 User-Agent、认证 Cookie 和当前事件游标判定 A2；普通匿名 GET 不能生成验收证据。

完成一次 Claude 网页 PPT 旅程后，由 Mac mini 本地管理员导入真实工件：

```bash
codex-workbench acceptance attest-a12 \
  --artifact /path/to/slides.pptx \
  --quota-window 2026-W35 \
  --source-session-id claude-web-session-id \
  --note "Claude web completed with the reserved quota pool"
```

只有实际存在、SHA-256 与大小匹配、且文件签名/内部结构确认为 PPT、PPTX 或 PDF 的工件才会被账本接收；改扩展名的 fixture 不会通过。当前即使具有 Claude Web session provenance，若没有可核验的 Claude export/receipt 与对应配额快照关联，A12 仍保持 `pending` 并等待人工验收，不会自动标记 `ok`。

## 状态语义

任务状态为：

```text
inbox → planning → ready → queued → running → verifying → accepted
                                           ├→ needs_fix
                                           ├→ needs_approval
                                           └→ blocked
```

Worker 进程成功只会结算节点。最终任务必须由标记为 verifier 的独立节点返回 `accepted`。协调器重启时仍在运行且无法证明终态的节点进入 `indeterminate`，只能通过显式 `task resolve` 选择 retry、fail 或 cancel。
