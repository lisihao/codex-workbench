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

## Claude 配额保护

Claude 没有稳定的官方 CLI 用量接口，因此系统不会猜测余额。只有本地资格审查和三个配额池都已写入时才允许派发：

```bash
codex-workbench quota set \
  --auth-ok --auth-method native-subscription \
  --five-hour 80 --weekly-all 70 --weekly-sonnet 65 \
  --source settings-usage
```

任何池未知或剩余不高于 25% 时禁止新 Claude 任务；系统不会读取或转发 `ANTHROPIC_API_KEY`。20% 永久作为 PPT 保留池，25% 是吸收在途任务的停线。

## 远程面板

Mac mini 正式服务只绑定 loopback。通过 Tailscale Serve 将其映射成 tailnet-only HTTPS 地址：

```bash
tailscale serve --bg --https=10443 http://127.0.0.1:8766
```

MacBook 和手机读取 `/api/snapshot` 与 `/api/events?after=<cursor>`；断线只产生 stale 投影，不会启动第二个协调器。控制操作需要 `codex-workbench token` 返回的本地令牌。

## 状态语义

任务状态为：

```text
inbox → planning → ready → queued → running → verifying → accepted
                                           ├→ needs_fix
                                           ├→ needs_approval
                                           └→ blocked
```

Worker 进程成功只会结算节点。最终任务必须由标记为 verifier 的独立节点返回 `accepted`。协调器重启时仍在运行且无法证明终态的节点进入 `indeterminate`，只能通过显式 `task resolve` 选择 retry、fail 或 cancel。
