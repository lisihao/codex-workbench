# Codex Workbench 原设计忠实度矩阵

基线：用户提供的 578 行《Codex 工作台完整设计方案》及后续范围变更。本矩阵描述 1.5.0 代码候选，不把接口、fixture 或一次进程启动等同于真实端到端完成。

状态语义：

- `implemented`：实现与聚焦测试均存在。
- `partial`：主体存在，但原设计中的一部分仍未实现。
- `external-pending`：代码和证据入口已存在，仍需真实设备、时间窗口或用户产品旅程。
- `not-implemented`：尚无对应实现。

| 原设计能力 | 状态 | 1.5.0 实现与证据 | 明确差距 |
|---|---|---|---|
| Mac mini 单一 24×7 权威 | implemented | SQLite authority machine ID、排他进程锁、coordinator epoch、node lease epoch、launchd | 真实整机重启仍由 A3 验收 |
| MacBook/手机只消费同一总账 | implemented | snapshot + cursor event API；客户端不写第二份 SQLite | 无 |
| Codex 唯一用户入口 | implemented | Codex stdio MCP、CLI 与个人 `wb` 薄插件均进入同一 Mac mini TaskContract/SQLite；插件不持有第二份任务状态 | 当前两台设备已核验的 Codex CLI 均不支持插件自定义顶层 `/WB`；默认输入 `wb`，`$WB` 与 `/skills` 仍可用；能力按设备实际版本核验，不要求版本相同 |
| WB 新旧会话接管 | implemented | UserPromptSubmit Hook 生成经脱敏的 Context Bundle；同步对话、Git patch、untracked/显式关联文件；Mac mini ArtifactStore + SQLite receipt + 隔离 worktree 导入后才返回 active；断网明确回退 MacBook 并保留 outbox | Codex 首次安装/Hook 内容变化后必须由用户在 `/hooks` 完成官方信任流程 |
| Sol 需求编译与 DAG | implemented | Sol planner 生成 DAG；图验证、依赖闭包、scope 冲突门禁 | 真实 Sol 质量由具体订阅回合决定 |
| 多场景必经 Research Skill | compatible-subset | Authority 安装器将一份完整 Research Skill 复制到隔离 planner HOME；`research-skill/v2` 先用可测试路由判定架构/探索、高复杂度、论文/上游、选型/迁移、性能/基准、最佳实践、兼容性/安全、竞品/可行性等场景，再向 Sol planner 注入强制 `$Research` directive | 默认由单一 Sol planner 执行 Standard 方法，只有用户明确要求深度、广泛或并行研究时才扩展多研究节点；当前可证明 Skill 安装与 prompt wiring，尚无 host-side runtime research receipt |
| Sol 规划、分层执行、Sol 验收 | implemented | Sol 固定 planner/verifier；routing-v2 以独立 Codex Spark 池处理低复杂度微任务，在配额安全时以 Sonnet 承担标准生产，以 Opus/Fable 承担架构挑战，Luna/Terra 接管回退 | routing-v1 仅为旧合同兼容，不作为新任务默认 |
| Claude Opus/Sonnet/Fable Worker | implemented | 原生订阅资格、结构化 JSON Schema、工具/权限映射、实际模型证明 | A4 真实 Sonnet 工件仍需一次最小真实回合 |
| Codex Worker | implemented | 原生订阅 CLI、结构化 worker/verifier 结果；低复杂度使用独立 `gpt-5.3-codex-spark` 池并按 Spark→Luna→Terra→Sol 有界升级；A4 已有 Luna + Sol 真实 Evidence | 无 |
| 确定性执行器 | implemented | 构建、测试、lint 等 argv 执行，不消耗模型 | 无 |
| 一个 Worker 一个 worktree/branch | implemented | base SHA、规范化仓库、独立 worktree、作用域验证 | 无 |
| DAG 并行与冲突消除 | implemented | 无依赖且 scope 不冲突才并行；read/read 可并行；父子 scope 互斥 | Planner 产生冲突时 fail loud，不猜测自动串行化 |
| 结构化任务契约 | implemented | repository/base/objective/scope/dependency/acceptance/model/quota/timeout/retry/权限/任务点 | 无 |
| `code-as-harness` 统一治理 | compatible-subset | Workbench canonical Skill 安装到 Codex/Claude Code 的全局 skills 目录，标记化 `AGENTS.md` / `CLAUDE.md` policy 保留用户内容；TaskContract 持久化 L0–L3；Sol planner、Codex/Claude Worker 与 verifier 接收同一治理指令；health 分开验证二进制、真实 Skill/policy 与无 CLI 静态注入 | canonical Skill 覆盖 Workbench 所需的证据确认、代码级 harness、并行、Evidence reuse 与 active-objective continuation；不声称外部平台专属 rich-card、thread-routing 或 plugin runtime 已执行 |
| Archify 架构工件 Skill | compatible-subset | 固定 vendored Archify `v2.16.0` stable core（MIT，tag/commit 见 `vendor/archify/SOURCE-LOCK.json`）并以 thin adapter 暴露四类 role contract；Sol planner 与匹配的 Codex/Claude Worker/verifier 仅在架构类 artifact 需要时注入 `$archify`、typed JSON IR、`validate`/`deliver` receipt 和外部 semantic evidence；两设备安装器先同时预检两端再写入 | Archify renderer/schema 的 9/9 与 composition pass 只证明 artifact 约束，不证明语义正确、运行时因果或推理质量；semantic evidence 与真实视觉 reviewer 仍须由外部/人工证据提供，不把它包装成完整 native plugin |
| 持久状态机与崩溃恢复 | implemented | SQLite 状态、Receipt、事件、indeterminate + approval；协调器失败触发 launchd 重启 | A3 真机重启证据 external-pending |
| Worker 不等于验收 | implemented | 只有独立 Sol verifier 可将任务置为 accepted；Evidence fail-closed | 无 |
| Claude 20% 目标保留、25% 停线 | compatible-subset | 五小时、周全模型与 Sonnet 专属池；Fable 受全模型周池约束，若未来 producer 暴露独立 Fable 池则叠加其上；30% admission guard、25% 硬停线、共享容量、每个 Claude 节点后要求新快照；未知即 Codex | 被动显示接口不能约束单回合消耗；绝不跨越 20% 仍需真实窗口 Evidence，不能静态保证 |
| Claude 被动 quota sidecar | implemented | 精确锁定 Claude CLI `2.1.239` 的 `/usage` display-text 兼容解析；显式 kickstart 的常驻 watcher 每分钟采集，不依赖 headless GUI domain 的 `StartInterval`；`loggedOut` 或采集错误立即原子写失败闭锁；以 `max(0, 99-used)` 作为剩余下界；正式 Claude admission 只信任完整 producer/schema/source/version provenance | 不是官方 quota API；尚未完成登录态生产验证，A6/A7 仍需兼容 native-subscription 的真实窗口 Evidence |
| 单位配额产出指标 | implemented | 每具名窗口的 accepted 加权任务点 / Claude 消耗；排除 fixture/test | 长周期趋势需真实窗口积累 |
| 配额触线自动转 Codex | implemented | 同一 attempt 记录 node.routed，不调用 Claude 后再重启 | A8 真实配额窗口证据 external-pending |
| MacBook 完整驾驶舱 | implemented | 任务/DAG/契约/Evidence/配额/告警、暂停恢复、优先级、steering、approval | 原文“接管异常 Agent”当前是控制与下一 attempt 指令，不是交互式终端 attach |
| 手机精简驾驶舱 | deferred | 响应式 UI 与 A2 receipt 代码保留 | 用户已将手机接入移至回加拿大后的 backlog；当前不部署、不作为门禁 |
| 手机后台通知 | deferred | 页面打开时的浏览器 Notification 代码保留 | 与手机接入一并转 backlog |
| 位置感知传输路由（家庭 LAN / Tailscale） | implemented | `workbench-location-proxy.py` 每次连接按显式 home CIDR 和有界 LAN probe 选路；MCP、Hook、tunnel、heartbeat 与 Git 增量同步共享同一 `ProxyCommand` profile；失败沿用 `degraded` + outbox | 尚未使用操作者的真实家庭 CIDR、LAN endpoint 与 Tailnet endpoint 完成双地点链路回放 |
| GitHub 主同步与增量传输 | implemented | clean fast-forward；显式刷新目标 remote-tracking ref，不依赖本地 `remote.*.fetch`；SSH/Tailscale Git bundle 导入独立 refs；MacBook 自动以 `tailscale nc` 绕过错误 CGNAT 系统路由 | 无 |
| 授权后 PR/CI/merge/release | implemented | accepted + external-write contract + durable delivery receipt；不确定时 indeterminate | 真实仓库权限仍由调用任务决定 |
| 认证与费用保护 | implemented | Codex/Claude native-subscription；API key 不转发；认证失败不循环重登；A9 已记录 Claude 失效后单次接管并由 Luna 执行、Sol 验收 | 无 |
| 无人值守 readiness | implemented | doctor 检查 FileVault、authrestart、launchd、Tailscale、自动开机和睡眠 | UPS/现场断电能力属于外部基础设施 |
| 当前验收账本 | implemented | A1、A3–A12 由持久真实 Evidence 计算；A2 显式列入 backlog；fixture/test 不能冒充；A5/A9/A10/A11 已有真实持久证据 | A1/A3/A4/A6/A7/A8/A12 仍需对应真实旅程或时间窗口 |
| 旧 A10 Evidence 追加补证 | implemented | manifest 存 ArtifactStore；command receipt 幂等；只追加 remediation event；重新绑定 source task/base/hash/cursor/attempt 与原 artifact refs | deterministic 或缺少原生结构化 verdict/checks/evidence 的历史 verifier 必须绑定另一个独立 accepted review task；该任务的 deterministic/Codex worker 实际物化并检查 source patch，依赖它的真实 Codex Sol 节点产生事件链和原生结果工件；自写 transcript/receipt 不算 Evidence |
| Claude Web PPT 保留池证明 | external-pending | 工件签名、内容地址、export receipt、session、配额窗口和 ≥20% 联合校验 | 需用户完成一次真实 Claude Web 导出 |
| 与 DSH 解耦 | implemented | 独立仓库、包、SQLite、进程、发布节奏；无 DSH 运行时依赖 | 无 |

## 当前忠实度结论

1. 控制面、执行面、配额治理、持久状态、工作树隔离和证据验收已按原设计实现。
2. 后续用户要求的 quota-productive routing-v2 是有意调整：Sol 仍负责规划/验收；低复杂度使用独立 Codex Spark 池，标准生产在安全配额时优先 Sonnet，高复杂度/架构审核优先 Opus/Fable，容量或配额不足由 Spark/Luna/Terra 填充。
3. 1.5.0 在完成当前 A1、A3–A12 外部旅程前应称为“设计兼容实现”，不能称为“全部验收完成”；尤其不应把 quota sidecar 的夹具验证描述为登录态生产验证。
4. 手机接入与页面关闭后的 Web Push 是用户明确后置的 backlog；交互式终端式 Agent attach 仍未实现，三者均不冒充当前交付。
