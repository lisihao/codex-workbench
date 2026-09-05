# Codex Workbench Backlog

## 页面关闭后的手机 Web Push（deferred）

1.10.0 已恢复手机接入：Codex 原生 Remote 管理入口、响应式手机 cockpit、认证控制和 A2 `client.observed` 回执都在当前实现与验收范围内。真实手机配对、查看和发任务仍是 external-pending Evidence，不属于 backlog。

唯一继续后置的是页面关闭后的 Web Push：需要单独决定推送提供方、设备 token 生命周期、权限提示与撤销策略。在此之前，页面打开期间的浏览器通知可以使用，但不能冒充后台 Push。

## v1.12.0 生产证据积累（evidence backlog）

本版本的性能闭环已经实现，但以下项目需要真实、获准的长期运行数据，不能用 fixture、静态健康检查或公开 benchmark 代替：

- Astra 已加入精确控制面型号目录；质量 benchmark 仍缺失。生产实际使用后的型号/Agent 回执与同类型任务对照才可用于校准，不继承 Sol 的测评分数。
- performance evaluate 已提供目录基线、去除 AI Frontier、完整输入的离线路由比较；当前选择变化只能证明数据参与了决策。正常任务的策略分组、质量非退步与真实时间/消耗改善仍需后续样本证明。

- 让真实模型/Agent/verifier receipt 按精确版本、harness、effort 和任务领域持续积累，用于校准当前 advisory posterior；当前接口只报告 `cold-start`/`ok`，尚无 `baseline`/`shadow`/`calibrated` 晋级阈值。
- 观察 Spark P0 的依赖就绪 `queue_depth`、`dependency_blocked`、`inflight`、`started`、`accepted`、`failed`、`blocked`、`indeterminate`、`retry`、`rework`、`busy_seconds`、`utilization` 和 `accepted_per_hour`；first-pass/final acceptance 与 duration 在 performance ledger 中另行观察，没有样本时显示 `N/A`。
- 按 Claude 五小时/周窗口验证 Keychain 登录态、`/usage` 采集和至少 20% 保留目标；Codex/Spark 剩余配额仍保持 `N/A`，不补造余额。
- 重新评估公开基准来源与迁移折扣；Terminal-Bench、SWE-Bench Pro、HLE 等不同领域不得合并为单一排行榜。
- Mac mini 生产首采已经完成：build `v1.10.0` / commit `5f99ef4cffe74687e23363b79a017e497175c3c3` 生成 snapshot `codex-radar-v1-f121a13f8301c655`，SQLite 五表 row counts 为 `1/4/58/1/1`，并导入 17 条精确 Codex model/effort 先验。后续 backlog 只是在真实、非 fixture 的 Workbench attempt 中持续校准 first-pass、返工、时延与验收结果。个人自用 receipt 仅表示本地操作者承担责任的公共 JSON 使用决定，不是站方授权。
- AI Frontier 正式部署需单独记录 build/commit、个人自用 receipt、snapshot/digest、五表 row counts、selected/skipped exact IDs、第二次刷新零网络和 performance provenance；当前公开榜单没有 GPT-5.6 Sol/Terra/Luna exact ID 时，正确行为是保留聚合观测并跳过这些模型，不做别名猜测。
- DSH 如需复用，另开独立任务只通过 `codex_radar_provider` 与 `ai_frontier_provider` 的稳定 CLI/JSON 合同只读消费并固定 snapshot ID/digest；不得复制 Provider SQLite、Workbench task SQLite、路由器或把外部榜单写成 DSH 本机 Evidence。本次版本未修改 DSH。

这些是生产观察与验收证据，不是为填充数据而触发模型调用的任务；每次校准仍须经过质量门禁和独立 verifier。
