# Codex Workbench Backlog

## 页面关闭后的手机 Web Push（deferred）

1.6.1 已恢复手机接入：Codex 原生 Remote 管理入口、响应式手机 cockpit、认证控制和 A2 `client.observed` 回执都在当前实现与验收范围内。真实手机配对、查看和发任务仍是 external-pending Evidence，不属于 backlog。

唯一继续后置的是页面关闭后的 Web Push：需要单独决定推送提供方、设备 token 生命周期、权限提示与撤销策略。在此之前，页面打开期间的浏览器通知可以使用，但不能冒充后台 Push。
