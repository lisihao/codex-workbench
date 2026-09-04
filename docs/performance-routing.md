# Workbench 性能路由：基线、账本与 Spark P0

状态：`v1.11.0 已实现 / 长期生产校准 Evidence 持续积累`
适用范围：Codex Workbench 的新任务路由、长期运行指标和 Spark 执行队列。本文不改变任务验收的权威性：Worker 结果永远不是验收结果，只有独立 verifier 的通过转换才能使任务 `accepted`。

本文把“模型擅长什么”和“在 Workbench 里实际交付得怎样”分开建模。随包公开基准、本机
personal-use consent 取得的 Codex Radar 公共 JSON 快照，以及 AI Frontier 公共 JSON 快照
都只用于冷启动弱先验；本机持久事件用于长期校准；这些来源都不能绕过能力、权限、证据和
质量门禁。

## 1. 目标与状态语义

Workbench 当前把公开 benchmark 先验与本地运行账本合成一个可重建的 advisory performance snapshot，但不能把外部排行榜伪装成本机实测。当前代码对校准上下文只报告以下两种状态：

| 状态 | 含义 | 是否改变新任务路由 |
|---|---|---|
| `cold-start` | 没有活动 performance snapshot；`calibrate` 使用随包的公开 benchmark 先验 | 可以作为 advisory 候选信息，但不能降低质量门禁 |
| `ok` | 存在有效的 content-addressed performance snapshot；候选同时带公开先验和已采集的运行统计（若有） | 只能在硬能力/角色/配额/作用域门禁之后作为 advisory 排序输入 |

当前没有 `baseline`、`shadow`、`calibrated` 或 `stale` 的晋级、过期和回滚生命周期，也没有按有效样本量或稳定性自动晋级的阈值。它们是后续设计可采用的状态，不应当写成 v1.11.0 已实现能力。当前 posterior/lower-bound 仍只是 advisory 估计，不是模型质量已被证明。

非目标：把 Terminal-Bench、SWE-Bench Pro、HLE 的分数做一个全局平均；用成本或速度换取低于质量下界的候选；用一次成功回合宣称模型长期可靠；在没有真实配额证据时编造剩余百分比。

## 2. 冷启动基线：按领域使用公开证据

随包的 v1 baseline 保存来源 URL、benchmark 版本、模型/家族、适用 task types、公开分数（若有）、provenance、transfer weight 和 effective sample strength。它不声称已保存所有原始评测配置、样本量、置信区间或精确 Agent/harness 版本；这些字段是后续扩展方向。Workbench 只把与任务领域相关的源放入对应先验，不跨领域平均。

| 主来源 | 主要测量对象 | Workbench 领域映射 | 使用边界 |
|---|---|---|---|
| [OpenAI GPT-5.6 官方评测表](https://openai.com/index/gpt-5-6/) | 同一官方页面中的 professional、coding 和 agentic coding 评测表 | `implementation`、`debugging`、`tests`、`research` 的弱领域先验 | 厂商报告的配置、采样和 harness 与本机不同；不把官方表直接当作本机成功率 |
| [Terminal-Bench 2.1](https://www.tbench.ai/news/terminal-bench-2-1) | 真实命令行环境中的长流程工具使用、终端操作与任务完成 | `terminal_execution`、`implementation`、`debugging`、低风险拆分 | 任务、依赖和资源会漂移；必须记录 benchmark 版本和 agent/model pairing |
| [SWE-Bench Pro 官方页面与 leaderboard](https://scaleapi.github.io/SWE-bench_Pro-os/) | 长时程、跨文件、真实软件工程问题的端到端解决 | `implementation`、`debugging`、`integration`、返工风险 | 不同 harness、补丁策略和测试环境不可直接横比；公开集不等于 Workbench 项目分布 |
| [SWE-Bench Pro 论文](https://arxiv.org/abs/2509.16941) | SWE-Bench Pro 的任务设计、复杂度和污染控制说明 | 解释领域适用性和迁移折扣 | 论文结果仍是外部实验；不替代本机 receipt 和 verifier 证据 |
| [Humanity's Last Exam leaderboard](https://labs.scale.com/leaderboard/humanitys_last_exam) 与 [论文](https://arxiv.org/abs/2501.14249) | 跨学科高难度知识、推理和校准；含公开与保留集 | `research`、`architecture`、`review`、需求歧义识别的弱先验 | 它不是代码交付或终端执行基准；不得把 HLE 分数直接转成实现成功率 |

### 2.1 分域而不是全局平均

初始先验按至少以下领域存储：

- `terminal_execution`：命令编排、工具调用、环境诊断和可复现执行。
- `implementation`：有明确范围的代码切片、测试和文档实现。
- `debugging`：定位根因、修复并通过已有验收命令。
- `architecture_research`：架构、需求拆解、上游/论文研究和跨模块决策。
- `review_acceptance`：独立审核、证据检查和最终验收。

当前 baseline 通过 `task_types`、`transfer_weight` 和 `effective_sample_strength` 表达有限的领域适用性与迁移强度；`applicability`、`transfer_discount` 等更细粒度字段仍是未来扩展。Terminal-Bench 可影响 coding task prior；HLE 只影响研究/推理相关 task，不能为 Spark 的代码实现质量背书。没有匹配领域时，代码使用 family 或 generic conservative prior，不从无关领域借分。

### 2.2 Codex Radar：可选的动态外部先验

1.10.0 加入通用 `codex_radar_provider` 0.2.0，固定使用 WineChord/codex-radar v0.1.69 的
JSON 契约。Provider 自有 `<state_root>/radar.sqlite3` 是 snapshots、raw payloads、models、
insights、active 的权威真源；每次 ingest 一事务提交，`raw/`、`generations/`、`active.json`
仅作为兼容投影，旧 JSON 会自动迁入数据库。断网、schema 失败或源时间倒退时保留数据库
last-known-good。上游 `current.json` 仍声明完整 API/衍生集成需站方授权；本地 personal-use
consent 只表示操作者承担责任、读取公共 JSON，不是站方许可，也不标成 `authorized`。

Workbench 只把以下 Radar 记录加入 coding prior：本机 capability catalog 中存在且 routable 的精确 provider/model、推理档位受支持、上游 `routing_eligible=true`、显式 pass rate 合法且 sample count 为正。`iq` 只作为元数据。外部有效样本强度上限为 `min(2.0, sample_count × 0.05)`；7 天内乘 1，7–31 天乘 0.25，超过 31 天不参与新路由。Radar snapshot ID/digest 与来源/归因固定进 performance snapshot provenance。

这组阈值是保守迁移权重，不是模型质量认证。Workbench 本机的真实 verifier 结果与返工记录仍是后验事实；Radar 也永远不是 Codex/Claude 的配额、订阅资格或并发容量证据。完整 provider、离线状态与未来 DSH 消费合同见 [Codex Radar 集成文档](codex-radar-integration.md)。
### 2.3 AI Frontier：多源外部弱先验

AI Frontier Provider 是独立的 `ai_frontier_provider` SQLite/LKG 组件。Authority 默认每
72 小时刷新一次，硬下限为 24 小时；没有本地 personal-use consent 时网络请求必须为零，
安装器只安装组件、配置和 Authority-only LaunchAgent，不写 consent receipt。刷新只允许
两个聚合 JSON endpoint：

```text
/api/reliability/leaderboard
/api/cost-comparison
```

并且最多为当前 capability catalog 中八个 `routable=true` 的精确模型请求
`/api/single-model/benchmarks?llm_name=...`。不抓 HTML、examples、Plotly/frontier/oracle
图表或视频，不读取凭据；MacBook 只读 Authority 的快照/API。完整网络、receipt、schema、
LKG 与未来 DSH consumer 合同见 [AI Frontier 集成文档](ai-frontier-integration.md)。

AI Frontier 的 `Quality` 是外部 benchmark accuracy/quality 弱先验，不是本机 first-pass 或
`accepted` 率；`Consistency` 是稳定性/离散程度，100% 也可能是稳定地错，只能调节不确定性
与风险；`Real Cost` 是来源发布口径下的 API 成本观察，不是 Codex/Claude 订阅 quota。
frontier/oracle 是多模型选择后的研究上界，不绑定单模型，也不直接参与单模型路由。
个人 consent 不是 Martian 官方授权；使用前应阅读 [Martian Terms of Service](https://withmartian.com/terms-of-service)。

## 3. 分层先验与迁移降权

当前运行账本实际用于聚合的 key 是 provider、实际模型、Agent 版本、task type 和 complexity；metric 内还记录 Agent 名称。它没有把 harness profile、reasoning effort、execution lane 或 repository/toolchain class 纳入聚合 key：

```text
(provider,
 exact_model_id,
 agent_version,
 task_type,
 complexity)
```

随包静态先验选择先按 provider + exact model + task type 匹配；没有 exact model 时按同 family 迁移并应用固定 family multiplier；没有公开质量分数时使用声明式或 generic conservative prior。Radar 动态记录不做 family 迁移，只在 exact model + supported reasoning effort 匹配时进入先验。

未来扩展的分层目标（当前未完整实现）是：

1. exact model + exact Agent/CLI version + exact harness/profile + exact effort + 相同任务上下文；
2. exact model + 相同 Agent/harness，但任务上下文相邻；
3. 同一 model family，Agent/harness 或 effort 不同；
4. 同领域的公开 benchmark family prior；
5. 全领域安全下界，仅用于 fail-safe，不用于证明能力。

当前实现为公开记录累加有限的 effective sample strength，并与运行成功/失败计数形成 advisory posterior，而不是把外部百分比当成确定值。更完整的迁移权重模型可概念上使用：

```text
effective_count = source_sample_count
                  × source_reliability
                  × identity_match
                  × domain_applicability
                  × transfer_discount
                  × freshness
```

`identity_match`、`domain_applicability` 和按 harness 的完整分层折扣尚未实现；当前公式仍是未来设计。1.10.0 已对 Radar 单独实现 exact effort、fresh/stale/expired 和有界样本强度，但这不能反向证明静态 benchmark 的 harness/effort 完全匹配。baseline 继续保留来源、provenance、match kind 和 transfer weight，使 exact-model 与 family-transfer 可追溯。

运行数据加入同一聚合时，真实 receipt 的质量成功/失败计数与公开 prior 合并；新 Agent 版本会形成不同 key，不覆盖旧版本。当前没有将 harness/effort/lane/toolchain 全部隔离成独立桶的实现，也没有自动 promotion 或 drift lifecycle；这些保留为未来设计。

## 4. 长期事实账本与派生性能快照

### 4.1 事实来源

Workbench 已有的 append-only `events` 与 `tasks` 是长期运行事实账本。性能层读取全量事件和任务合同，重建任务、节点、attempt、开始/结束和 verifier 转换，不另造一份会与 Workbench task SQLite 状态漂移的“真相库”。Codex Radar Provider 的 `radar.sqlite3` 是另一套仅负责外部快照的数据库；它不替代、不复制 Workbench task/event SQLite，消费者通过 provider CLI/JSON 合同读取。当前 quota 只从最新可信 snapshot 形成池视图；带时间的配额/容量历史和完整 reservation 账本仍是未来扩展。

v1.11.0 的性能层生成 content-addressed `performance snapshot` 作为派生缓存，实际包括：

```text
schema_version
snapshot_id / content_digest
event_cursor
catalog { catalog_id, catalog_digest }
baseline { baseline_id, digest, records }
ledger { calibration_event_count, eligible_terminal_attempts,
         excluded_terminal_attempts, logical_nodes }
metrics[] { runtime rates, outcomes, rework, posterior, duration }
pools
source_provenance
advisory_policy
```

snapshot 可以删除后由事实账本、能力目录和基线重建；它不是任务状态权威。创建任务时固定 performance `snapshot_id`/digest、catalog/capability digest 和 policy string，并写入 TaskContract/NodeSpec。运行中的任务继续使用原快照；刷新不会重路由已 claim 的节点。`source_cutoff`、独立 `policy_version` 和更细的 calibration window 是未来 schema 扩展，不应当当作当前字段。

### 4.2 计量口径

每个可用于模型校准的节点至少记录：

| 指标 | 口径 | 是否进入模型质量分母 |
|---|---|---|
| first-pass acceptance | 第一次 attempt 直接通过独立验收 | 是，单独统计 |
| final acceptance | 允许有界返工/重试后，任务最终进入 `accepted` | 是，单独统计 |
| rework | 需要 repair、第二次及以上 attempt，或 verifier 明确要求修改 | 作为负向信号；不能与 first-pass 混为一次成功 |
| blocked / indeterminate | 因等待审批、配额/认证阻断、证据不确定或系统无法判定而未形成结果 | 单独计数；不进入已确认质量成功/失败 |
| duration | 当前按 node started 到 terminal event 计算 mean/p50；没有排队、执行、验证阶段的独立分解 | 进入 advisory 速度统计，不直接改变质量 |
| quota/pool utilization | scheduler metrics 另行回放 lane 的 queue depth、inflight、busy seconds、utilization 和 accepted/hour；quota remaining 只有可信 provider snapshot 才记录 | 进入观测，不等同 reservation/cost ledger，也不直接改变质量 |

performance snapshot 的每个 runtime metric 会同时保存 `first_pass` 和 `final_acceptance`，并保存 rework、retry、outcomes 与 duration summary。`indeterminate` 不能被当作成功或失败；scheduler API 不提供 first-pass/final 字段，它们只在 performance ledger 中可见。

### 4.3 基础设施失败剔除规则

当前代码的可确认规则较窄：fixture、deterministic、verifier、Evidence reuse、缺少 result、缺少 `actual_model` 或 provider 不支持的 terminal attempt 会进入 exclusion ledger，不参与质量 posterior；`agent_version=unattested`、非零或未知进程退出、认证/配额阻断形成的 `blocked`，以及 `indeterminate` 会保留运行指标但记作 unresolved，不进入已确认的质量成功/失败。只有进程成功、Agent 版本已证明且 receipt 形成可判定的模型结果时，最终 `accepted` 或 `needs_fix` 才进入质量分母。

当前没有 `failure_origin` 字段或完整的 `infrastructure_failure` / `model_or_task_failure` 分类器。因此 process crash、主机断开、网络失败、harness 启动失败或超时不能被文档宣称为已完全从模型质量失败中分离；若 receipt 无法确认归因，应保留为 unresolved/unknown，不能强行归因给模型。未来可增加显式 failure origin 和可靠性报表，但那不是 v1.11.0 当前实现。

没有真实模型 receipt 的 fixture/测试/演示只能验证管线，不能校准生产模型质量。当前纳入质量 posterior 的 terminal attempt 至少要求实际 `actual_model`、已证明的 Agent version、Codex/Claude provider、零进程退出和非 fixture/deterministic/verifier 路径；harness profile、effort 和完整 `failure_origin` 尚未作为独立校准维度。`baseline`/`shadow`/`calibrated` 集合与 promotion 规则是未来设计。

## 5. 校准、保守下界与质量门禁

对于每个领域/上下文桶，以 Beta-Binomial 保存 `success` 与 `failure`（基础设施失败和 unknown 不进这两个计数）。后验可以写成：

```text
prior       = Beta(alpha_prior, beta_prior)
posterior   = Beta(alpha_prior + success,
                   beta_prior + failure)
quality_lcb = lower_quantile(posterior, policy.alpha)
```

`quality_lcb` 是设计上的保守质量下界，而不是均值。v1.11.0 当前实现保存 prior/posterior 的 alpha、beta、mean，并以固定 `z=1.96` 的正态近似计算 `lower_bound_95`；它不是 Beta 分布的精确 5% 分位数，也不是已通过独立质量认证的分数，当前仍没有 promotion threshold。

v1.11.0 的实际 routing-v3 顺序是：先完成能力、角色、任务类型、工具权限、结构化输出、作用域、Claude 认证配额与并发等硬门禁，再计算保守质量；质量差距超过验收风险带时质量优先，只有带内候选才按显式偏好与交付效率排序：

```text
硬门禁 → 保守质量 → 验收风险等价带
       → 用户/角色偏好 → attempts/rework/latency/consistency
       → relative cost/catalog cost/throughput/utilization → 确定性 tie-break
```

硬门禁包括 role、task type、complexity、工具和权限、结构化输出、harness/Agent 版本、证据能力、Claude quota freshness/admission、并发容量和任务作用域。任何候选低于 role 的 `quality_floor` 都不可用；成本和速度不能把它重新买回来。当前没有 quota reservation/cost-unit 预留账本；只有通过硬门禁的候选，才比较保守质量下界、已观测返工、延迟、成本、吞吐和容量利用率。

### 5.1 AI Frontier 与本地账本的融合算法

外部资料不应被压缩成一个全局排行榜。当前本地运行账本建立的精确上下文桶为：

```text
(provider, exact_model_id, agent_version, reasoning_effort,
 task_type, complexity)
```

`harness_profile` 和 tool/permission contract 作为路由硬门禁存在，但 v1.11.0 尚未把它们加入
运行校准桶键；不能把硬门禁误写成已经实现的统计隔离维度。

只有 identity、任务领域和来源 benchmark family 可以证明匹配时，才把外部记录加入该桶。
AI Frontier、Codex Radar 和随包 benchmark 的相关观测按 source cluster 去重并设置总强度上限；
Quality 先转换成 accuracy 弱先验，不能当作本机 first-pass。 `Consistency` 只调节不确定性
或风险；`Consistency=100%` 也可能代表稳定地答错。`Real Cost` 保留为 publisher-defined
API cost observation，不是订阅 quota，也不是 Claude 五小时/周剩余量。

对每个桶用外部弱先验加本机真实结果构造 Beta 后验：

```text
alpha = alpha_external + local_confirmed_successes
beta  = beta_external  + local_confirmed_failures
quality_lcb95 = max(0, posterior_mean - 1.96 * posterior_stddev)
```

其中 local confirmed success/failure 只来自非 fixture、非 deterministic、非 verifier、
具有 actual model/Agent version、零进程退出且由独立 verifier 明确判定的 terminal attempt。
网络、主机、harness、认证/配额阻断和 indeterminate 只记为 unresolved，不进入 Beta 分母。
外部 prior 按 freshness、domain、identity 和 benchmark cluster cap 降权；本地长期数据不断
累积后自然压过外部 prior，不会因一次刷新覆盖历史。新 Agent/CLI 版本必须开新桶。

### 5.2 质量等价带内的效率排序

先执行全部硬门禁，再取候选中最高的 `quality_lcb95`。只有质量下界落在该最高值的等价带
内，才允许比较效率：

| complexity | 质量下界允许差距 |
| --- | ---: |
| `critical` | 0 个百分点 |
| `high` | 0.5 个百分点 |
| `standard` | 2 个百分点 |
| `low` | 4 个百分点 |

等价带内按以下顺序选择，保证“质量优先，成本/速度优化”：

```text
用户/角色偏好优先
expected_attempts 越低
rework_rate 越低
执行时延越短
consistency risk 越低
发布者相对成本越低
catalog cost units 越低
accepted throughput 越高
lane/quota utilization 拥塞惩罚越低
provider/model 的稳定确定性 tie-break
```

可解释的效率指标可写为：

```text
useful_rate = quality_lcb95 × (1 - rework_rate)
expected_time = queue_wait + execution_latency + verifier_latency
efficiency = useful_rate / max(expected_time, epsilon)
```

该指标只能在质量等价带内使用，不能让更快、更便宜但低于质量门禁的候选重新入选。
并行启动还要通过 scope 冲突、独立验收和 marginal useful-rate/cost 门禁；相关性过高的
候选不重复启动。所有输入来自 pinned catalog、performance snapshot 和 policy，故同一输入
闭包应产生相同排序。


`performance refresh` 只读取本地事实账本、可信 quota snapshot、Radar cache 与 AI Frontier cache，不触发 Claude 登录、模型调用、付费 API 或人工配额探测。独立的 `radar refresh` 与 `ai-frontier refresh` 只有在本地 personal-use consent 有效后才访问各自固定 JSON endpoints；AI Frontier 默认 72 小时、硬性不低于 24 小时，失败保留数据库 last-known-good。performance snapshot 会生成或复用 content-addressed generation；本机 runtime 样本的衰减、漂移检测和 promotion 容忍范围尚未实现。

关于保守在线学习的设计依据可参阅 [Conservative Contextual Linear Bandits](https://arxiv.org/abs/1611.06426) 与 [Conservative Contextual Bandits（ICLR 2025）](https://proceedings.iclr.cc/paper_files/paper/2025/hash/dbca58f35bddc6e4003b2dd80e42f838-Abstract-Conference.html)。模型/Agent 变化的检测可采用版本隔离和显式漂移事件；不应因为一个短窗口的偶然提升就覆盖长期基线。

## 6. 配额池模型

配额和容量是不同概念，必须分开记录：

```text
performance_pools = {
  codex: { remaining: null, remaining_display: "N/A" },
  spark: { remaining: null, remaining_display: "N/A", separate_rate_limit: true },
  claude: { remaining: { five_hour, weekly_all, weekly_sonnet, weekly_fable } }
}
runtime_lane = {
  lane, capacity, queue_depth, dependency_blocked, inflight, started, accepted,
  failed, blocked, indeterminate, retry, rework,
  busy_seconds, utilization, accepted_per_hour
}
```

Claude 的五小时/周窗口只接受可信的被动 quota snapshot；至少保留用户要求的 20% 可用目标，并在 policy 的停线阈值前停止 admission。Codex 订阅若没有官方剩余百分比，不得伪造 `remaining`；可以记录本地并发、429/限流和等待时间作为 capacity evidence，但它们不是配额余额。

当前实现不做 pool/cost unit reservation、release 或 reservation-failure admission ledger；它只在 Claude quota gate 和 runtime lane capacity gate 上做判断，并把 Codex/Spark remaining 保持为 `N/A`。按 provider/family/lane 分离的 reservation、成本扣账和释放事件是未来扩展，不能当作 v1.11.0 已有功能。

## 7. Spark P0：专属逻辑队列、主动拆分与质量保护

### 7.1 专属逻辑队列

Spark P0 是 Workbench 的优先生产能力，不等于“所有低风险任务都强行交给 Spark”。当前 Coordinator 在单一全局 `ThreadPoolExecutor` 内为 Spark 维护逻辑 lane capacity，并在 claim 时优先尝试 Spark，再让 general/control 借用未使用的全局槽位；持久节点状态提供依赖就绪 `queue_depth`、`dependency_blocked` 与 `inflight` 观测，历史 lane/pool 以 NodeSpec 而非可伪造事件标签为准。它没有独立的 ready-queue 数据结构、queue-wait 计时或 escalation 指标；这些是未来扩展。

Spark candidate 必须同时满足：

- 低复杂度、范围明确、短时且可独立完成；
- 单一受控写作用域，或纯只读/生成物作用域；没有与其他 ready node 的写冲突；
- 有确定的 acceptance command 或机械可验证的 receipt；
- 已固定所需工具、权限、harness 和 model/Agent 能力；
- 不承担架构、跨模块决策、需求歧义、研究判断、秘密/认证操作或最终验收。

依赖数量本身不是复杂度证明：只要依赖已经完成、作用域不冲突且其余条件满足，节点仍可进入 Spark queue。反之，只有一个依赖也不意味着可以进入 Spark。

### 7.2 主动任务拆分

Sol planner 的受控 prompt 会要求主动寻找可并行的独立切片，例如互不写冲突的单文件实现、独立测试、文档或机械校验；归一化器/validator 会检查 Spark 所需的低复杂度、低风险、短任务、机械验收和 scope 条件。每个切片仍需有输入、输出、作用域、依赖和 acceptance command；当前实现不提供独立的“拆分质量分数”或自动拆分结果 Evidence，planner 不得为了填满 Spark lane 而制造伪节点。

确定性 normalizer/validator 再检查切片是否满足 Spark 条件；planner 的文字建议不能绕过 validator。不能安全拆分时，保留较大的 Luna/Terra worker，必要时由 Sol 做跨模块决策和最终验收。

### 7.3 利用率与质量指标

在观察窗口 `W` 内，当前实现按配置的 Spark lane capacity 计算利用率：

```text
spark_utilization(W)
  = busy_seconds(W)
    / (spark_workers × window_seconds)
```

`/api/scheduler` 当前实际报告以下 lane 指标，避免把“占满”误当成“高产出”：

```text
queue_depth, inflight, started, accepted, failed,
blocked, indeterminate, retry, rework,
busy_seconds, utilization, accepted_per_hour
```

分母为零时利用率输出 `null`（界面可显示 `N/A`），不应造成误读。first-pass/final acceptance、duration 和 posterior 在 performance snapshot 的 model/version/task/complexity metric 中另行记录；`eligible_share`、`routed_share`、`escalation_rate`、queue wait 分位数不是当前 scheduler API 字段。按更细的 harness/effort/task domain 分组与这些衍生指标属于未来扩展。

### 7.4 失败升级

当前 v1.11.0 对 Spark 的可观察行为是：

1. terminal 结果、retry 和 rework 会进入 append-only event ledger；v3 节点保持其已选 capability，不在 claim 时静默升级；
2. Claude 的认证/配额阻断可在同一 attempt 走 Codex fallback，但这不是 Spark 的自动升级指标；
3. Sol 仍负责跨模块决策和最终 verifier/acceptance，不作为普通 Spark repair worker。

未来可以增加有界 Spark→Luna→Terra repair policy、升级原因和 escalation receipt，但当前没有 generic Spark escalation contract，也不能声称已经按该链路自动升级或统计。

## 8. 验收边界与持续更新

v1.11.0 当前的最小可证明闭环如下；实现状态与真实生产 Evidence 分开记录：

| 验收项 | 证据要求 | 当前设计状态 |
|---|---|---|
| 外部基线可追溯 | 随包 baseline JSON 保留来源、benchmark 版本、模型/家族、task types、分数（若有）、provenance、transfer weight；Radar 与 AI Frontier 各自固定 snapshot/digest、freshness、exact identity 与归因；Provider SQLite status 暴露 backend/schema/path/row counts | `implemented`；运行快照仍按每次部署单独验收 |
| 长期账本不丢 attempt | 从 append-only events/tasks 读取并重建 runtime metrics，记录 exclusion ledger | `implemented`；真实生产样本仍需积累 |
| snapshot 可重建且可 pin | content-addressed 派生快照；新任务 pin performance/catalog identity，运行中不重路由 | `implemented` |
| 质量优先 | 硬能力/角色/作用域/Claude quota gate 先于 advisory quality posterior、成本和速度；不能绕过验收 | `implemented`；没有 promotion lifecycle |
| Spark P0 | 单一全局 executor 内 Spark-first logical lane、主动拆分 prompt/validator、lane utilization API | `implemented`；没有独立 ready queue、reservation、queue wait 或自动 escalation receipt |
| 真实校准 | 真实模型/Agent/verifier receipt 长期进入 exact runtime buckets，并观察 posterior 与 acceptance/rework/duration | `external-pending`；当前 `calibrate` 只报告 `cold-start`/`ok`，不执行晋级阈值 |

当前实现会在 baseline/catalog/runtime 内容变化时生成新的 content-addressed snapshot；任务合同固定其 performance/catalog identity，历史任务不因刷新而重路由。公开基准新鲜度、模型/Agent 版本隔离、漂移检测以及 `baseline`/`shadow`/`calibrated`/`stale` 生命周期仍是未来设计，不能当作当前报告字段。

## 9. 公开基准的局限

- benchmark 分数是特定数据、prompt、工具、预算、重试策略和 harness 下的结果；模型名相同不代表 Agent 运行路径相同。
- Terminal-Bench、SWE-Bench Pro 和 HLE 的目标不同，不能用一个加权平均替代领域模型。
- leaderboard 的公开集可能存在污染、过拟合、测试覆盖不足、环境漂移或任务规格问题；保留集、fresh benchmark 和本机 verifier 都只能降低风险，不能消除风险。
- 供应商官方表适合提供冷启动方向，也带有自报配置和不可完全复现的限制；官方来源的权重仍须经过 identity/domain/recency 折扣。
- 本机 Workbench 的真正目标是“在用户仓库、指定权限和实际验收命令下成功交付”。因此长期观察必须最终以真实 receipt、first-pass/final acceptance、返工、duration、lane 利用率和明确可归因的结果为准；质量后验按 provider/model/Agent version/reasoning effort/task/complexity 精确隔离。当前没有 `failure_origin` 分类器，未知 process failure 不能被强行归因给模型。

作为新鲜度与污染讨论的补充，可参阅 [SWE-rebench](https://arxiv.org/abs/2505.20411)；它不替代上述四个主来源，也不直接提供 Workbench 当前模型的本机质量证明。

当前问题：v1.11.0 已实现公开 benchmark baseline、Radar 与 AI Frontier 两个独立 SQLite/LKG 外部先验源、append-only runtime ledger、advisory posterior、quality-equivalence efficiency 路由、content-addressed snapshot pinning 和 Spark logical lane；`performance calibrate` 仍只返回 `cold-start`/`ok`，没有 promotion lifecycle、quota reservation、failure_origin 或足够长期生产 receipt 可宣称动态质量校准完成。

下一步：在不触发模型调用的前提下，逐次部署核对两个 Provider 的数据库路径、row counts、snapshot/digest 和 performance provenance；随后持续收集真实任务的 actual model/Agent version、first-pass/final acceptance、rework、duration 与 Spark lane 指标。DSH 未来仅通过稳定 provider CLI/JSON 合同只读接入，不修改 DSH 或复制 Workbench task SQLite。
