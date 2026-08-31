# Codex Workbench 原设计忠实度矩阵

基线：用户提供的 578 行《Codex 工作台完整设计方案》。本矩阵描述 1.0.1 代码候选，不把接口、fixture 或一次进程启动等同于真实端到端完成。

状态语义：

- `implemented`：实现与聚焦测试均存在。
- `partial`：主体存在，但原设计中的一部分仍未实现。
- `external-pending`：代码和证据入口已存在，仍需真实设备、时间窗口或用户产品旅程。
- `not-implemented`：尚无对应实现。

| 原设计能力 | 状态 | 1.0.1 实现与证据 | 明确差距 |
|---|---|---|---|
| Mac mini 单一 24×7 权威 | implemented | SQLite authority machine ID、排他进程锁、coordinator epoch、node lease epoch、launchd | 真实整机重启仍由 A3 验收 |
| MacBook/手机只消费同一总账 | implemented | snapshot + cursor event API；客户端不写第二份 SQLite | 无 |
| Codex 唯一用户入口 | implemented | Codex stdio MCP 与 CLI 均提交同一 TaskContract | 无独立 DSH 依赖 |
| Sol 需求编译与 DAG | implemented | Sol planner 生成 DAG；图验证、依赖闭包、scope 冲突门禁 | 真实 Sol 质量由具体订阅回合决定 |
| Sol 规划、低阶执行、Sol 验收 | implemented | Sol 固定 planner/verifier；Luna 低复杂度，Terra 复杂实现 | 相较原文“Sonnet 常规开发”，按用户后续成本/速度要求改为 Codex-first |
| Claude Opus/Sonnet/Fable Worker | implemented | 原生订阅资格、结构化 JSON Schema、工具/权限映射、实际模型证明 | A4 真实 Sonnet 工件仍需一次最小真实回合 |
| Codex Worker | implemented | 原生订阅 CLI、结构化 worker/verifier 结果、Luna→Terra→Sol 有界升级 | A4 真实 Luna 工件仍需一次最小真实回合 |
| 确定性执行器 | implemented | 构建、测试、lint 等 argv 执行，不消耗模型 | 无 |
| 一个 Worker 一个 worktree/branch | implemented | base SHA、规范化仓库、独立 worktree、作用域验证 | 无 |
| DAG 并行与冲突消除 | implemented | 无依赖且 scope 不冲突才并行；read/read 可并行；父子 scope 互斥 | Planner 产生冲突时 fail loud，不猜测自动串行化 |
| 结构化任务契约 | implemented | repository/base/objective/scope/dependency/acceptance/model/quota/timeout/retry/权限/任务点 | 无 |
| 持久状态机与崩溃恢复 | implemented | SQLite 状态、Receipt、事件、indeterminate + approval；协调器失败触发 launchd 重启 | A3 真机重启证据 external-pending |
| Worker 不等于验收 | implemented | 只有独立 Sol verifier 可将任务置为 accepted；Evidence fail-closed | 无 |
| Claude 20% 硬保留、25% 停线 | implemented | 五小时、周全模型、Sonnet/Fable 池；green/yellow/red/protected；未知即 Codex | 无官方机器可读配额 API，依赖用户设置页真实快照 |
| 单位配额产出指标 | implemented | 每具名窗口的 accepted 加权任务点 / Claude 消耗；排除 fixture/test | 长周期趋势需真实窗口积累 |
| 配额触线自动转 Codex | implemented | 同一 attempt 记录 node.routed，不调用 Claude 后再重启 | A8 真实配额窗口证据 external-pending |
| MacBook 完整驾驶舱 | implemented | 任务/DAG/契约/Evidence/配额/告警、暂停恢复、优先级、steering、approval | 原文“接管异常 Agent”当前是控制与下一 attempt 指令，不是交互式终端 attach |
| 手机精简驾驶舱 | implemented | 响应式任务阶段、时间、下一步、审批和控制；真实渲染写 A2 receipt | A2 仍需真机回执 |
| 手机后台通知 | partial | 页面打开时使用浏览器 Notification 展示完成/阻塞/审批/配额/协调器事件 | 页面关闭后的 Web Push 尚未实现 |
| GitHub 主同步与增量传输 | implemented | clean fast-forward；SSH/Tailscale Git bundle 导入独立 refs；MacBook 自动以 `tailscale nc` 绕过错误 CGNAT 系统路由 | 无 |
| 授权后 PR/CI/merge/release | implemented | accepted + external-write contract + durable delivery receipt；不确定时 indeterminate | 真实仓库权限仍由调用任务决定 |
| 认证与费用保护 | implemented | Codex/Claude native-subscription；API key 不转发；认证失败不循环重登 | A9 真实认证过期旅程 external-pending |
| 无人值守 readiness | implemented | doctor 检查 FileVault、authrestart、launchd、Tailscale、自动开机和睡眠 | UPS/现场断电能力属于外部基础设施 |
| A1–A12 验收账本 | implemented | 所有检查由持久真实 Evidence 计算；fixture/test 不能冒充 | A1/A2/A3/A4/A5/A6/A7/A8/A9/A10/A12 需对应真实旅程或时间窗口 |
| Claude Web PPT 保留池证明 | external-pending | 工件签名、内容地址、export receipt、session、配额窗口和 ≥20% 联合校验 | 需用户完成一次真实 Claude Web 导出 |
| 与 DSH 解耦 | implemented | 独立仓库、包、SQLite、进程、发布节奏；无 DSH 运行时依赖 | 无 |

## 当前忠实度结论

1. 控制面、执行面、配额治理、持久状态、工作树隔离和证据验收已按原设计实现。
2. 后续用户要求的 Codex-first Luna/Terra 路由是唯一有意的模型分工调整；它不改变 Sol 规划/验收、Claude 可选高阶 Worker 和配额保护边界。
3. 1.0.1 在完成真实 A1–A12 外部旅程前应称为“设计兼容实现”，不能称为“全部验收完成”。
4. 尚未忠实覆盖的产品能力只有页面关闭后的手机 Web Push，以及交互式终端式 Agent attach；二者均在 UI 中不冒充已实现。
