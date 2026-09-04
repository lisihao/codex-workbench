# Codex Radar 通用 Provider 与 Workbench 集成

状态：`v1.9.0 已实现 / 真实数据采集待 Codex Radar 授权`

本集成固定使用 [WineChord/codex-radar](https://github.com/WineChord/codex-radar)
`v0.1.69` / commit `4c83973df6b17e6b18b0b56e8735168580fea12b` 的公开 JSON
契约与同步方法。上游软件是 macOS 菜单栏应用，并没有可被 Workbench 直接调用的 CLI、
IPC 或 server；因此本仓库提供一个独立、标准库-only 的 `codex_radar_provider` 包，而不是
复制上游 GUI 或重写一套 benchmark 采集系统。

软件 MIT 许可与数据使用授权是两件事。当前 Codex Radar 公共状态要求衍生集成先获得
授权并保留归因；本实现因此在没有本地授权 receipt 时 fail closed，而且在发出任何网络
请求之前返回 `unauthorized`。固定归因是：`数据来自 Codex 雷达 codexradar.com`。

## 1. 组件与数据流

```text
Codex Radar JSON endpoints
          │  仅在本地授权 receipt 有效后；默认每 6 小时
          ▼
codex_radar_provider                     通用、无 Workbench/DSH 依赖
  ├── raw/<snapshot-id>.json             原始响应，敏感字段脱敏
  ├── generations/<snapshot-id>.json     规范化、内容寻址快照
  └── active.json                        原子 last-known-good 指针
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
路由到哪个模型。Workbench adapter 不拥有第二份任务账本；它只把有效 Provider 快照转换
为 performance snapshot 中可追溯的外部弱先验。

## 2. 通用 Provider 契约

安装本仓库 Python 包后，可用四个命令：

```bash
codex-radar-provider --state-root <RADAR_STATE> status
codex-radar-provider --state-root <RADAR_STATE> show [--snapshot-id <ID>]
codex-radar-provider --state-root <RADAR_STATE> refresh \
  --authorization-file <AUTHORIZATION_RECEIPT>
codex-radar-provider --state-root <RADAR_STATE> import \
  --authorization-file <AUTHORIZATION_RECEIPT> \
  --payload-dir <EXPORTED_JSON_DIRECTORY>
```

`status` 与 `show` 只读本地文件。`refresh` 只访问上游 Skill 记录的四个 JSON endpoint；
不做 HTML 抓取。`import` 允许另一个已获授权的采集端把四份 JSON 交给离线机器，仍会执行
相同 schema、时间戳和授权检查。

规范化快照 schema version 为 `1`：

```text
schema_version
snapshot_id / digest
upstream { name, repository, version, commit, json_contract }
source_urls { current, intelligence_efficiency, model_ratings, radar_insights }
attribution
authorization { schema, version, provider, status, scope }
ingest_mode
fetched_at / source_updated_at / source_timestamps
models[]
insights
raw_payload_digest
cache { state, stale_after_seconds }
```

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

授权 receipt 只记录“授权已由外部事实授予”的元数据，不能包含 token、cookie、密码或
API key。不要根据本说明自行伪造 receipt；只有拿到 Codex Radar 明确授权后，才把授权
范围与归因写入 authority 的 `radar/authorization.json`。

```json
{
  "schema": "codex-radar-provider-authorization",
  "version": 1,
  "provider": "codex-radar",
  "status": "authorized",
  "scope": ["<exact-scope-granted-by-provider>"],
  "attribution": "数据来自 Codex 雷达 codexradar.com"
}
```

Provider 的失败语义：

| 状态 | 本地快照 | 网络行为 | 消费方式 |
| --- | --- | --- | --- |
| `unauthorized` | 无 | 0 请求 | 使用宿主内置 baseline，不使用 Radar |
| `unavailable` | 无 | 刷新失败或尚未采集 | 使用宿主内置 baseline |
| `fresh` | 有 | `status/show` 为 0 请求 | 可作为受限外部先验 |
| `stale` | 有 | 刷新失败时保留 last-known-good | 消费者必须降权并显示原时间 |
| `expired` | 有 | Workbench 的消费状态 | 保留用于审计，但不影响新路由 |

写入顺序是 raw generation、normalized generation、最后原子替换 `active.json`。schema
错误、时间戳倒退或网络失败不会覆盖 last-known-good。Workbench 默认 7 天内为 fresh，
7–31 天为 stale，超过 31 天为 expired；这些是 Workbench 消费策略，不改变 Provider
文件的通用 schema。

## 4. Workbench 的保守校准

Mac mini authority 安装一个独立 LaunchAgent，默认每 21600 秒运行：

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
codex-workbench --home <WB_STATE_ROOT> radar status
codex-workbench --home <WB_STATE_ROOT> radar show
codex-workbench --home <WB_STATE_ROOT> radar refresh
curl http://127.0.0.1:8766/api/radar
```

`/health`、`/api/snapshot` 和 `/api/radar` 的 Radar 部分都是只读的，不会因为查看状态而
联网。没有授权时显示 `unauthorized` 是正确运行状态，不是服务健康失败；即使磁盘仍有
旧缓存，撤销/删除本地授权 receipt 也会立即把 Radar 先验从下一份 performance snapshot
移除。Provider 与 Workbench 的 freshness 阈值取更严格者。

## 5. 未来 DSH 接入合同（本次不修改 DSH）

DSH 后续有三种等价消费方式：安装本仓库发布的 provider Python 包、调用
`codex-radar-provider ... show`，或只读 `generations/<snapshot-id>.json`。推荐使用包内
`validate_radar_snapshot()`，避免自行复制 digest 与 schema 校验。

DSH adapter 的最小责任：

1. 读取并验证 schema version、snapshot ID/digest、授权和来源归因；
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

- 已实现：通用 package/CLI、授权前零网络、四端点 JSON、脱敏 raw、规范化 generation、
  原子 active、last-known-good、时间戳回退保护、Workbench 状态/API/性能快照接入、
  authority-only 六小时定时任务、插件 Skill 与上游 source lock。
- 已验证：无网络 fixture 测试、installer rollback、plugin validation、Workbench 受影响
  测试与完整仓库 gate。
- 未宣称：当前机器已经获得 Codex Radar 数据授权；当前缓存已有真实 Radar 记录；Radar
  能证明某模型在本机项目上的真实成功率；DSH 已经完成接入。

当前问题：真实 Radar 采集仍缺 Codex Radar 官方数据授权 receipt；在此之前 authority 会
稳定显示 `unauthorized`，并继续使用内置公开 benchmark 与本地运行账本。

下一步：取得明确数据授权后，仅在 Mac mini 写入不含秘密的 receipt，执行一次
`radar refresh`，核对归因、snapshot ID、model/effort 映射和 performance provenance；DSH
接入另开任务，仅实现上述只读 adapter，不复制 Workbench 调度逻辑。
