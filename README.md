# Codex Workbench

独立于 DSH 的个人开发基础设施。Mac mini 持有唯一任务账本、后台执行器、Git worktree 和验收证据；MacBook 与手机通过 Tailscale 私网查看同一份状态。

核心约束：

- Codex 是唯一用户入口，Claude Code 只是可选后台 Worker。
- 任务、节点、事件、配额快照和审批持久化到 SQLite。
- DAG 节点只有依赖完成且写作用域不冲突时才能并行执行。
- Worker 完成不等于任务完成，必须经过独立 verifier。
- Claude 配额未知、认证未知或剩余不高于 25% 时禁止启动新任务，至少保留 20%。
- 不读取或转发 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`，只允许产品订阅登录态。

```bash
PYTHONPATH=src python3 -m codex_workbench init
PYTHONPATH=src python3 -m codex_workbench serve
PYTHONPATH=src python3 -m codex_workbench doctor
```

默认数据目录为 `~/Library/Application Support/Codex Workbench`，默认只监听 `127.0.0.1:8766`。远程访问由 Tailscale Serve 代理，服务自身不开放公网端口。

