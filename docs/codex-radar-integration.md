# Codex Radar 通用 Provider 与 Workbench 集成

状态：`v1.10.0 / codex-radar-provider 0.2.0 已实现并部署；Mac mini 真实生产首采 Evidence 已闭环`

本集成固定使用 [WineChord/codex-radar](https://github.com/WineChord/codex-radar)
`v0.1.69` / commit `4c83973df6b17e6b18b0b56e8735168580fea12b` 的公开 JSON
契约与同步方法。上游软件是 macOS 菜单栏应用，并没有可被 Workbench 直接调用的 CLI、
IPC 或 server；因此本仓库提供一个独立、标准库-only 的 `codex_radar_provider` 包，而不是
复制上游 GUI 或重写一套 benchmark 采集系统。

软件 MIT 许可、公开 JSON 阅读以及站方衍生集成许可是不同事项。上游 `current.json` 仍
声明完整 API 与衍生集成需要站方授权。本版本的个人自用路径只记录操作者对公共 JSON 的
本地使用决定，不是站方许可，也绝不把它标成 `authorized`。固定归因是：`数据来自 Codex
雷达 codexradar.com`；没有有效的本地 consent 或官方 receipt 时，网络请求前 fail closed。

## 1. 组件与数据流

```text
Codex Radar public JSON endpoints
          │  仅在本地 personal-use consent 或官方 receipt 有效后；默认每 86400 秒
          ▼
codex_radar_provider 0.2.0               通用、无 Workbench/DSH 依赖
  ├── radar.sqlite3                      权威真源（snapshot/raw/models/insights/active）
  └── JSON projections                   raw/generations/active，仅兼容投影
          │
          ├──────────────► 任意消费者 / 未来 DSH adapter
          │                读取同一 schema，自行保留硬门禁
          ▼
WorkbenchRadar                            Workbench 专用薄适配层
  ├── fresh / stale / expired
  ├── exact model + reasoning effort
  └── bounded weak prior
          ▼
performance snapshot ──► routing-v3
          │                  │
          │                  └── 能力、角色、作用域、配额、质量门禁先执行
          └── snapshot ID/digest 固定到新任务；活动任务不被刷新重路由
```

Provider 不读取 Codex 或 Claude 凭据，不触发模型调用，不保存 API key，也不判断任务应该
路由到哪个模型。`<state_root>/radar.sqlite3` 是 Provider 的权威状态；每次 ingest 在一笔
事务中写入 snapshot、raw payload、models、insights 和 active。`raw/`、`generations/`、
`active.json` 只是兼容投影。Workbench adapter 不拥有第二份任务账本；它只把有效 Provider
快照转换为 performance snapshot 中可追溯的外部弱先验。

## 2. 通用 Provider 契约

安装本仓库 Python 包后，可用以下命令：

```bash
codex-radar-provider --state-root <RADAR_STATE> consent --personal-use \
  [--authorization-file <RECEIPT>]
codex-radar-provider --state-root <RADAR_STATE> status
codex-radar-provider --state-root <RADAR_STATE> show [--snapshot-id <ID>]
codex-radar-provider --state-root <RADAR_STATE> refresh \
  --authorization-file <AUTHORIZATION_RECEIPT>
codex-radar-provider --state-root <RADAR_STATE> import \
  --authorization-file <AUTHORIZATION_RECEIPT> \
  --payload-dir <EXPORTED_JSON_DIRECTORY>
```

`status` 与 `show` 只访问本地 SQLite（必要时自动把有效旧 JSON 投影迁入数据库），不联网。
`refresh` 只访问上游 Skill 记录的四个 JSON endpoint，不做 HTML 抓取；默认 86400 秒内
再次调用会被节流。`import` 允许另一个采集端把四份 JSON 交给离线机器，仍会执行相同
schema、时间戳和 consent/授权检查。`consent --personal-use` 只写入本地、无秘密的 receipt，
不联网。

规范化快照 schema version 为 `1`：

```text
schema_version
snapshot_id / digest
upstream { name, repository, version, commit, json_contract }
source_urls { current, intelligence_efficiency, model_ratings, radar_insights }
attribution
authorization { schema, version, provider, status, scope, basis?, accepted_at? }
ingest_mode
fetched_at / source_updated_at / source_timestamps
models[]
insights
raw_payload_digest
cache { state, stale_after_seconds }
```

Provider 数据库位于 `<state_root>/radar.sqlite3`，schema version 为 `1`，包含
`radar_snapshots`、`radar_raw_payloads`、`radar_models`、`radar_insights` 和
`radar_active` 五张表。`status` 会暴露 `backend=sqlite`、schema、绝对路径和各表 row
counts。数据库先提交；JSON 文件只作为兼容投影，投影失败不会回滚已提交的权威数据库。
旧版本若只有有效的 `raw/`、`generations/`、`active.json`，首次读取会自动迁入 SQLite；
迁移后仍保留 JSON 以兼容旧消费者。

每个 `models[]` 记录包含：

```text
provider / model / reasoning_effort / routing_eligible
pass_rate / iq / sample_count
avg_cost_usd / avg_runtime_seconds
community_rating / metric_sources
```

`iq` 是独立指标，绝不能转换成 `pass_rate`。只有显式 `pass_rate ∈ [0,1]`、正
`sample_count`、精确模型与推理档位可识别且 `routing_eligible=true` 的记录，才有资格被
消费者考虑。未知模型会保留用于观察，但不会自动获得路由权限。

## 3. 授权、缓存与断网行为

receipt 不能包含 token、cookie、密码或 API key。个人自用由以下四项同时表示：
`status=consented`、`basis=local_operator_consent`、`scope` 包含 `public-json`、以及有效的
`accepted_at`。它只是本地操作者承担责任的个人使用 consent，不是站方授权，不得写成
`status=authorized`。上游 `current.json` 对完整 API/衍生集成仍要求站方授权；若以后获得站方
明确授权，才可另行使用站方提供的 `status=authorized` receipt。

```json
{
  "schema": "codex-radar-provider-authorization",
  "version": 1,
  "provider": "codex-radar",
  "status": "consented",
  "basis": "local_operator_consent",
  "scope": ["public-json"],
  "accepted_at": "2026-09-04T00:00:00Z",
  "attribution": "数据来自 Codex 雷达 codexradar.com"
}
```

Provider 的失败语义：

| 状态 | 本地快照 | 网络行为 | 消费方式 |
| --- | --- | --- | --- |
| `unauthorized` | 无有效本地 consent/官方 receipt | 0 请求 | 使用宿主内置 baseline，不使用 Radar |
| `unavailable` | 无 | 刷新失败或尚未采集 | 使用宿主内置 baseline |
| `fresh` | 有 | `status/show` 为 0 请求 | 可作为受限外部先验 |
| `stale` | 有 | 刷新失败时保留 last-known-good | 消费者必须降权并显示原时间 |
| `expired` | 有 | Workbench 的消费状态 | 保留用于审计，但不影响新路由 |

写入先在 SQLite 一事务中提交 raw payload、normalized snapshot、models、insights 与
active，再原子刷新 JSON 兼容投影；schema 错误、时间戳倒退或网络失败不会覆盖数据库中的
last-known-good。Workbench 默认 7 天内为 fresh，7–31 天为 stale，超过 31 天为 expired；
这些是消费策略，不改变 Provider 的稳定 JSON schema。断网时直接读取数据库 LKG，JSON
投影缺失也不影响数据库读取。

## 4. Workbench 的保守校准

Mac mini authority 安装一个独立 LaunchAgent，默认每 86400 秒运行：

```bash
codex-workbench --home <WB_STATE_ROOT> radar refresh
```

该命令先刷新通用 Provider；有可用 last-known-good 时，再重建 Workbench performance
snapshot。MacBook client 不安装第二个 Radar writer，只从 authority 的 API/cockpit 读取
状态。

外部记录进入 Workbench 的强度为：

```text
base_strength = min(2.0, sample_count × 0.05)
fresh_strength = base_strength × 1.00
stale_strength = base_strength × 0.25
expired_strength = 0
```

上限 2.0 小于当前 Luna coding 精确公开基线的合计 strength 4.0，保证单条社区观察仍是
次要信号。这不是把社区分数当成本机成功率。外部先验保存在 baseline records 与
`source_provenance.external_priors.codex_radar`；本机真实 attempt 的 first-pass、最终验收、
返工与时延继续作为后验事实，并按 provider/model/Agent version/reasoning effort/task/
complexity 精确分桶。routing-v3 必须先通过 capability、role、task type、scope、
tool、Claude quota/concurrency 和质量门禁，Radar 只在合法候选之间提供 advisory 信息。

常用入口：

```bash
codex-workbench --home <WB_STATE_ROOT> radar consent-personal-use
codex-workbench --home <WB_STATE_ROOT> radar status
codex-workbench --home <WB_STATE_ROOT> radar show
codex-workbench --home <WB_STATE_ROOT> radar refresh
curl http://127.0.0.1:8766/api/radar
```

`/health`、`/api/snapshot` 和 `/api/radar` 的 Radar 部分都不会联网。没有本地 consent 时
显示 `unauthorized` 是正确运行状态，不是服务健康失败；撤销/删除 receipt 会立即把 Radar
先验从下一份 performance snapshot 移除，数据库中的历史快照仍保留审计。`/api/radar` 和
provider `status` 会显示 SQLite backend、schema、path 与 row counts。Provider 与 Workbench
的 freshness 阈值取更严格者。

## 5. 未来 DSH 接入合同（本次不修改 DSH）

DSH 后续通过安装本仓库发布的 provider Python 包、调用 `codex-radar-provider ... show`，
或读取稳定 JSON 合同来消费；推荐使用包内 `validate_radar_snapshot()`，避免绑定 Provider
内部表。Provider 的 `radar.sqlite3` 与 Workbench 的任务/事件 SQLite 是两套独立数据库，
DSH 不应复制或写入 Workbench SQLite。

DSH adapter 的最小责任：

1. 读取并验证 schema version、snapshot ID/digest、consent/授权字段和来源归因；个人自用
   必须同时满足 `consented`、`local_operator_consent`、`public-json`、`accepted_at`；
2. 将 Provider 快照 ID/digest 固定进新 Run/TaskGraph 的输入闭包；
3. 只接受 exact provider/model/effort 和正样本 `pass_rate`；未知模型保持 observed-only；
4. 自行定义 fresh/stale/expired 阈值，并在 stale 时保留原 `fetched_at`；
5. 把 Radar 视为外部 prior，不能写成本地 runner 成功率或 Evidence；
6. 继续执行 DSH 自己的 capability、scope、lease、quota 与 verifier 门禁；
7. 永远不从 Radar 推断 Codex/Claude 剩余配额或订阅资格；
8. Provider 不可用时回到 DSH 自身基线，不能生成猜测数据。

这一合同使 Provider 可在 Workbench 与 DSH 之间复用，但两者不会共享 SQLite、调度器或
任务状态。本版本只交付接口和 Workbench adapter；没有读取、写入、构建或部署任何 DSH
源码。

## 6. 当前可证明边界

- 已实现：通用 package/CLI、personal-use consent、授权/consent 前零网络、四端点 JSON、
  SQLite 权威数据库与五表一事务、旧 JSON 自动迁移、兼容投影、脱敏 raw、规范化 generation、
  原子 active、last-known-good、时间戳回退保护、Workbench 状态/API/性能快照接入、
  authority-only 每日定时任务、插件 Skill 与上游 source lock。
- 已验证：无网络 fixture 测试、installer rollback、plugin validation、Workbench 受影响
  测试与完整仓库 gate；Mac mini authority 已部署 build `v1.10.0` / commit
  `5f99ef4cffe74687e23363b79a017e497175c3c3`。个人自用 receipt 后的生产首采生成 snapshot
  `codex-radar-v1-f121a13f8301c655`，SQLite 五表 row counts 为 `1/4/58/1/1`，并把 17 条
  精确 Codex model/effort 记录导入 performance snapshot。紧接着的第二次 refresh 被 86400 秒
  门禁阻止联网，两个过程均为 `model_calls=0`。
- 未宣称：个人 consent 等同站方授权；Radar 能证明某模型在本机项目上的真实成功率；DSH
  已经完成接入。

当前问题：生产首采已经完成；仍需靠后续真实 Workbench attempt 长期校准公开数据先验，且
个人自用 consent 不等于站方对完整 API/衍生集成的授权。

下一步：Mac mini 按 86400 秒低频计划持续刷新并复用 SQLite last-known-good；只有输入闭包
变化时才生成新的 performance snapshot。DSH 接入另开任务，仅实现上述 CLI/JSON 只读
adapter，不修改 DSH 或复制 Workbench 调度逻辑。
