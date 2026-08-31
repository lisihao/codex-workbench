# Codex Workbench Backlog

## 手机接入与真机验收（deferred）

用户已明确将手机接入从当前交付范围和验收门禁中移除，等回加拿大后再恢复。

保留但本轮不继续推进的实现：

- tailnet-only HTTPS 控制面；
- 响应式手机任务摘要、审批与短指令；
- authenticated `client.observed` 回执与 A2 判定；
- 页面打开期间的浏览器通知。

恢复条件：

1. 用户回到加拿大并确认手机 Tailscale 身份可用；
2. 重新确认目标 Mac mini Tailscale identity 与 MagicDNS；
3. 手机真机登录、渲染同一 Authority 快照并生成服务端回执；
4. 再决定是否建设页面关闭后的 Web Push。

在恢复前，A2 以 `deferred` 出现在验收报告的 `backlog` 数组中，不参与 `complete`。
