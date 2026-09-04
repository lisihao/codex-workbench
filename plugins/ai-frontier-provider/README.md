# AI Frontier Provider 0.1.0

AI Frontier Provider 是一个可被 Codex Workbench 和未来 DSH 宿主独立使用的、离线优先
数据桥接器。它把 Martian AI Frontier 的公开 JSON 观测保存为本地 SQLite 快照，供上层把
质量、成本和稳定性作为外部先验；Provider 自身不路由任务，也不把外部 Quality 伪装成本地
任务成功率。

## 数据与合规边界

- 默认没有 consent receipt 时状态为 `disabled_by_policy`，且绝不联网。
- `consent --personal-use` 只记录本机操作者的自用选择。receipt 永远写入
  `not_official_authorization: true`，不代表 Martian 官方授权、许可或服务条款例外。
- 使用前请审阅 [Martian Terms of Service](https://withmartian.com/terms-of-service)。该站点
  的条款对自动化访问、下载及 benchmark/竞争分析用途有限制；操作者必须自行确认其使用
  方式适用并承担相应责任。
- Provider 仅请求两个聚合 JSON：`/api/reliability/leaderboard` 与
  `/api/cost-comparison`。不会抓 HTML、Cookie、登录态、examples、Plotly frontier，亦不会
  调用模型。
- 指定 `--model` 时才会读取 `/api/single-model/benchmarks`，最多八个且必须出现在当次
  leaderboard；缺失模型会记录为 `skipped_source_ids`，不会阻止两张聚合表落库。默认不会
  产生额外的模型级请求。
- 默认 refresh 间隔是 72 小时，内部硬下限是 24 小时；没有重试循环。断网或字段漂移时，
  保留 SQLite 的 last-known-good 快照并明确标记结果失败。

## 本地合同

`<state-root>/ai-frontier.sqlite3` 是唯一权威存储，目录为 `0700`，数据库与 receipt 为
`0600`。单笔 SQLite 事务同时写入：snapshot、raw payload、normalized models、categories
和 active pointer。快照包含本地 `fetched_at`，并明确声明来源没有公开远端时间戳/版本号；
其 `routing_boundary` 将 frontier/oracle 观测排除在直接调度之外。

模型观测的固定语义：

```text
Quality       = 跨 benchmark 外部质量观测，不是本地成功率
Consistency   = 稳定性，不是成功率
Cost          = 发布方定义的相对成本，不默认换算为美元
Cost Surprise = 可正可负的发布方差异观测
```

## 使用

宿主安装本仓库 Python 包后，可执行 CLI（或等价的
`python -m ai_frontier_provider.cli`）：

```text
ai-frontier-provider --state-root <DIR> status
ai-frontier-provider --state-root <DIR> consent --personal-use
ai-frontier-provider --state-root <DIR> show [--snapshot-id <ID>]
ai-frontier-provider --state-root <DIR> refresh \
  --authorization-file <DIR>/authorization.json \
  [--model openai/gpt-5.6-luna] [--model anthropic/claude-opus-4-6]
```

`status` 和 `show` 都只读本地 SQLite，绝不会发出网络请求。`refresh` 会先校验 secret-free
personal-use receipt；无 receipt、格式错误或低于 24 小时的请求间隔都在本地失败。

消费方应固定 `snapshot_id`/`digest` 到自身任务或证据合同；刷新只影响新任务。不要让外部
观测绕过本地 capability、质量门槛、配额或最终验证门禁。
