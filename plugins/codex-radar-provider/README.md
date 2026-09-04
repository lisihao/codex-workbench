# Codex Radar Provider 0.2.0

Codex Workbench 的离线优先 Codex Radar 数据桥接插件。它把上游
[WineChord/codex-radar](https://github.com/WineChord/codex-radar) 的同步方法投影到一个
可供 Workbench 和 DSH 消费的通用 Skill/Provider 边界，便于后续接入模型能力先验、质量
信号和调度校准。

## 边界

- `skills/codex-radar-sync/` 是上游 Skill 的原样副本，用于跟踪端点、字段和兼容性变化；它
  不在本插件中改写，也不等于内置上游菜单栏 GUI。
- `skills/codex-radar-provider/` 是本插件自己的桥接 Skill，规定 CLI、缓存、consent 和消费
  合同；本仓库随 Python 包提供实际 `codex-radar-provider` 命令，插件 Skill 本身不启动
  后台进程。
- 本插件不把 Codex Radar 当作实时配额源。实时配额必须继续由宿主自己的 quota collector
  获取；Radar 只能提供带时间戳、样本量和来源的质量/成本/速度先验。
- `<state_root>/radar.sqlite3` 是 Provider 的权威真源，按一笔事务保存 snapshots、raw
  payloads、models、insights 和 active；`raw/`、`generations/`、`active.json` 只是兼容投影。
  旧 JSON 投影首次读取时自动迁入 SQLite；网络不可用时直接返回数据库中的 last-known-good，
  并明确标记 `stale`，不生成猜测数据。
- 个人自用通过 `consented`、`local_operator_consent`、`public-json`、`accepted_at` 四项
  receipt 字段表示；这不是站方 `authorized` 许可。上游 `current.json` 仍声明完整 API/
  衍生集成需要站方授权，软件 MIT 许可也不改变该边界。

## 通用消费方式

Workbench 和 DSH 可直接使用同一个 `codex_radar_provider` Python 包、CLI 或读取其
标准化 JSON，不需要互相依赖。Provider 的稳定快照包含（数据库是存储实现，JSON 是消费
合同）：

`schema_version`、`snapshot_id`、`digest`、`upstream`、`source_urls`、`fetched_at`、
`source_updated_at`、`authorization`、`cache`、`models[]` 和 `insights`。模型记录使用
`provider`、`model`、`reasoning_effort`、`routing_eligible`、`pass_rate`、`iq`、
`sample_count`、`avg_cost_usd`、`avg_runtime_seconds` 与 `metric_sources`。消费者应把
快照 ID/digest 固定到自己的任务或证据合同；刷新只影响新任务。

## 安装

在包含本仓库 team marketplace 的 Codex 环境中安装 `codex-radar-provider`。宿主安装了
本仓库 Python 包后，可执行：

```text
codex-radar-provider --state-root <DIR> status
codex-radar-provider --state-root <DIR> show [--snapshot-id <ID>]
codex-radar-provider --state-root <DIR> consent --personal-use \
  [--authorization-file <RECEIPT>]
codex-radar-provider --state-root <DIR> refresh --authorization-file <RECEIPT>
codex-radar-provider --state-root <DIR> import --authorization-file <RECEIPT> --payload-dir <DIR>
```

`status`/`show` 永不联网，并报告 `backend=sqlite`、schema、数据库路径和五张表的 row
counts。`consent --personal-use` 只写入本地 receipt，不联网。`refresh` 会先验证不含秘密
的 consent/授权 receipt；没有有效 receipt 时返回 `unauthorized` 且网络请求数为零。独立
CLI 的默认值与 Workbench 定时生产合同都将 refresh 节流设为 86400 秒；CLI 参数只用于操作者
明确覆盖。API key 如以后由授权方要求，只能通过显式环境
变量引用传入，不能写进 receipt、快照或插件配置。

最小个人自用 receipt 形状如下；它记录的是本地操作者的决定，不是站方授权：

```json
{
  "schema": "codex-radar-provider-authorization",
  "version": 1,
  "provider": "codex-radar",
  "status": "consented",
  "basis": "local_operator_consent",
  "scope": ["public-json"],
  "accepted_at": "<UTC_TIMESTAMP>",
  "attribution": "数据来自 Codex 雷达 codexradar.com"
}
```

DSH 后续只需把 provider 包或 CLI 安装到自己的宿主，并通过稳定 CLI/JSON 合同只读消费上述
schema；不要绑定 Provider SQLite 内部表，也不要复制/写入 Workbench task SQLite。本次集成
没有修改 DSH 代码。

上游软件与 Skill 版权和许可见 `LICENSE-WineChord-Codex-Radar`。
