# Codex Workbench Backlog

## 页面关闭后的手机 Web Push（deferred）

1.7.0 已恢复手机接入：Codex 原生 Remote 管理入口、响应式手机 cockpit、认证控制和 A2 `client.observed` 回执都在当前实现与验收范围内。真实手机配对、查看和发任务仍是 external-pending Evidence，不属于 backlog。

唯一继续后置的是页面关闭后的 Web Push：需要单独决定推送提供方、设备 token 生命周期、权限提示与撤销策略。在此之前，页面打开期间的浏览器通知可以使用，但不能冒充后台 Push。

## v1.7.0 生产证据积累（evidence backlog）

本版本的性能闭环已经实现，但以下项目需要真实、获准的长期运行数据，不能用 fixture、静态健康检查或公开 benchmark 代替：

- 让真实模型/Agent/verifier receipt 按精确版本、harness、effort 和任务领域持续积累，用于校准当前 advisory posterior；当前接口只报告 `cold-start`/`ok`，尚无 `baseline`/`shadow`/`calibrated` 晋级阈值。
- 观察 Spark P0 的依赖就绪 `queue_depth`、`dependency_blocked`、`inflight`、`started`、`accepted`、`failed`、`blocked`、`indeterminate`、`retry`、`rework`、`busy_seconds`、`utilization` 和 `accepted_per_hour`；first-pass/final acceptance 与 duration 在 performance ledger 中另行观察，没有样本时显示 `N/A`。
- 按 Claude 五小时/周窗口验证 Keychain 登录态、`/usage` 采集和至少 20% 保留目标；Codex/Spark 剩余配额仍保持 `N/A`，不补造余额。
- 重新评估公开基准来源与迁移折扣；Terminal-Bench、SWE-Bench Pro、HLE 等不同领域不得合并为单一排行榜。

这些是生产观察与验收证据，不是为填充数据而触发模型调用的任务；每次校准仍须经过质量门禁和独立 verifier。
