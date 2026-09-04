# Codex Radar Provider

Codex Workbench 的离线优先 Codex Radar 数据桥接插件。它把上游
[WineChord/codex-radar](https://github.com/WineChord/codex-radar) 的同步方法投影到一个
可供 Workbench 和 DSH 消费的通用 Skill/Provider 边界，便于后续接入模型能力先验、质量
信号和调度校准。

## 边界

- `skills/codex-radar-sync/` 是上游 Skill 的原样副本，用于跟踪端点、字段和兼容性变化；它
  不在本插件中改写，也不等于内置上游菜单栏 GUI。
- `skills/codex-radar-provider/` 是本插件自己的桥接 Skill，规定 CLI、缓存、授权和消费
  合同；本仓库随 Python 包提供实际 `codex-radar-provider` 命令，插件 Skill 本身不启动
  后台进程。
- 本插件不把 Codex Radar 当作实时配额源。实时配额必须继续由宿主自己的 quota collector
  获取；Radar 只能提供带时间戳、样本量和来源的质量/成本/速度先验。
- 网络不可用时，Provider 返回最近一次完整且有效的 last-known-good 快照，并明确标记
  `stale`；没有有效快照时失败，不生成猜测数据。
- 需要授权的数据端点在授权状态无法证明时 fail-closed。软件的 MIT 许可不自动授予
  CodexRadar 数据 API 的衍生集成权限，详情见 `upstream-lock.json`。

## 通用消费方式

Workbench 和 DSH 可直接使用同一个 `codex_radar_provider` Python 包、CLI 或读取其
标准化 JSON，不需要互相依赖。Provider 的稳定快照包含：

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
codex-radar-provider --state-root <DIR> refresh --authorization-file <RECEIPT>
codex-radar-provider --state-root <DIR> import --authorization-file <RECEIPT> --payload-dir <DIR>
```

`status`/`show` 永不联网。`refresh` 会先验证不含秘密的授权 receipt；无有效 receipt 时
返回 `unauthorized` 且网络请求数为零。API key 如以后由授权方要求，只能通过显式环境
变量引用传入，不能写进 receipt、快照或插件配置。

DSH 后续只需把 provider 包或 CLI 安装到自己的宿主，并消费上述 schema；本次集成没有
修改 DSH 代码。

上游软件与 Skill 版权和许可见 `LICENSE-WineChord-Codex-Radar`。
