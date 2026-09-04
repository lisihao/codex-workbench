# Workbench 性能路由：基线、账本与 Spark P0

状态：`v1.8.2 已实现 / 生产校准 Evidence 待积累`
适用范围：Codex Workbench 的新任务路由、长期运行指标和 Spark 执行队列。本文不改变任务验收的权威性：Worker 结果永远不是验收结果，只有独立 verifier 的通过转换才能使任务 `accepted`。

本文把“模型擅长什么”和“在 Workbench 里实际交付得怎样”分开建模。公开基准用于冷启动先验；本机的持久事件用于长期校准；二者都不能绕过能力、权限、证据和质量门禁。

## 1. 目标与状态语义

Workbench 当前把公开 benchmark 先验与本地运行账本合成一个可重建的 advisory performance snapshot，但不能把外部排行榜伪装成本机实测。当前代码对校准上下文只报告以下两种状态：

| 状态 | 含义 | 是否改变新任务路由 |
|---|---|---|
| `cold-start` | 没有活动 performance snapshot；`calibrate` 使用随包的公开 benchmark 先验 | 可以作为 advisory 候选信息，但不能降低质量门禁 |
| `ok` | 存在有效的 content-addressed performance snapshot；候选同时带公开先验和已采集的运行统计（若有） | 只能在硬能力/角色/配额/作用域门禁之后作为 advisory 排序输入 |

当前没有 `baseline`、`shadow`、`calibrated` 或 `stale` 的晋级、过期和回滚生命周期，也没有按有效样本量或稳定性自动晋级的阈值。它们是后续设计可采用的状态，不应当写成 v1.8.2 已实现能力。当前 posterior/lower-bound 仍只是 advisory 估计，不是模型质量已被证明。

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

## 3. 分层先验与迁移降权

当前运行账本实际用于聚合的 key 是 provider、实际模型、Agent 版本、task type 和 complexity；metric 内还记录 Agent 名称。它没有把 harness profile、reasoning effort、execution lane 或 repository/toolchain class 纳入聚合 key：

```text
(provider,
 exact_model_id,
 agent_version,
 task_type,
 complexity)
```

当前先验选择只有两级：先按 provider + exact model + task type 匹配公开记录；没有 exact model 时按同 family 的迁移记录，并应用固定 family transfer multiplier；没有公开质量分数时使用声明式或 generic conservative prior。

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

`identity_match`、`domain_applicability`、`freshness` 和按 harness/effort 的分层折扣尚未作为完整字段和策略实现；当前文档中的公式是未来设计，不是 v1.8.2 的运行时证明。现有 baseline 仍保留来源、provenance、match kind 和 transfer weight，使 exact-model 与 family-transfer 可追溯。若公开来源没有 exact pairing，Workbench 将其标为迁移先验，不能当作本机 exact evidence。

运行数据加入同一聚合时，真实 receipt 的质量成功/失败计数与公开 prior 合并；新 Agent 版本会形成不同 key，不覆盖旧版本。当前没有将 harness/effort/lane/toolchain 全部隔离成独立桶的实现，也没有自动 promotion 或 drift lifecycle；这些保留为未来设计。

## 4. 长期事实账本与派生性能快照

### 4.1 事实来源

Workbench 已有的 append-only `events` 与 `tasks` 是长期运行事实账本。性能层读取全量事件和任务合同，重建任务、节点、attempt、开始/结束和 verifier 转换，不另造一份会与 SQLite 状态漂移的“真相库”。当前 quota 只从最新可信 snapshot 形成池视图；带时间的配额/容量历史和完整 reservation 账本仍是未来扩展。

v1.8.2 的性能层生成 content-addressed `performance snapshot` 作为派生缓存，实际包括：

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

当前没有 `failure_origin` 字段或完整的 `infrastructure_failure` / `model_or_task_failure` 分类器。因此 process crash、主机断开、网络失败、harness 启动失败或超时不能被文档宣称为已完全从模型质量失败中分离；若 receipt 无法确认归因，应保留为 unresolved/unknown，不能强行归因给模型。未来可增加显式 failure origin 和可靠性报表，但那不是 v1.8.2 当前实现。

没有真实模型 receipt 的 fixture/测试/演示只能验证管线，不能校准生产模型质量。当前纳入质量 posterior 的 terminal attempt 至少要求实际 `actual_model`、已证明的 Agent version、Codex/Claude provider、零进程退出和非 fixture/deterministic/verifier 路径；harness profile、effort 和完整 `failure_origin` 尚未作为独立校准维度。`baseline`/`shadow`/`calibrated` 集合与 promotion 规则是未来设计。

## 5. 校准、保守下界与质量门禁

对于每个领域/上下文桶，以 Beta-Binomial 保存 `success` 与 `failure`（基础设施失败和 unknown 不进这两个计数）。后验可以写成：

```text
prior       = Beta(alpha_prior, beta_prior)
posterior   = Beta(alpha_prior + success,
                   beta_prior + failure)
quality_lcb = lower_quantile(posterior, policy.alpha)
```

`quality_lcb` 是设计上的保守质量下界，而不是均值。v1.8.2 当前实现保存 prior/posterior 的 alpha、beta、mean，并以固定 `z=1.96` 的方差近似计算 `lower_bound_95`；它是 advisory 值，不是已通过独立质量认证的分数，也没有 promotion threshold。

v1.8.2 的实际 routing-v3 顺序是：先完成能力/角色/任务类型/复杂度/工具/权限/结构化输出/作用域/Claude 认证配额与并发等硬门禁，再以质量分数或可用的 posterior lower bound 为主排序，first-pass、rework、时延、显式偏好、成本、吞吐、容量利用率和 provider/model 作为后续 tie-breaker：

```text
硬门禁 → 质量/下界 → first-pass → rework → latency → preference/cost/throughput/utilization → 确定性 tie-break
```

硬门禁包括 role、task type、complexity、工具和权限、结构化输出、harness/Agent 版本、证据能力、Claude quota freshness/admission、并发容量和任务作用域。任何候选低于 role 的 `quality_floor` 都不可用；成本和速度不能把它重新买回来。当前没有 quota reservation/cost-unit 预留账本；只有通过硬门禁的候选，才比较保守质量下界、已观测返工、延迟、成本、吞吐和容量利用率。

未来可在版本化 policy 中加入低样本、分布漂移、近期失败聚集和稳定候选比较，并定义 `baseline`/`shadow`/`calibrated`/`stale` promotion、降级与回滚；v1.8.2 尚未实现这些生命周期，不会把一次 posterior 变化宣称为长期可靠性提升。

定期重建只读取本地事实账本和可信 quota snapshot，不触发 Claude 登录、模型调用、付费 API 或人工配额探测。v1.8.2 的 `performance refresh` 会生成/复用 content-addressed snapshot；最小有效样本、衰减、漂移检测和 promotion 容忍范围尚未实现为运行时 policy。

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

当前实现不做 pool/cost unit reservation、release 或 reservation-failure admission ledger；它只在 Claude quota gate 和 runtime lane capacity gate 上做判断，并把 Codex/Spark remaining 保持为 `N/A`。按 provider/family/lane 分离的 reservation、成本扣账和释放事件是未来扩展，不能当作 v1.8.2 已有功能。

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

当前 v1.8.2 对 Spark 的可观察行为是：

1. terminal 结果、retry 和 rework 会进入 append-only event ledger；v3 节点保持其已选 capability，不在 claim 时静默升级；
2. Claude 的认证/配额阻断可在同一 attempt 走 Codex fallback，但这不是 Spark 的自动升级指标；
3. Sol 仍负责跨模块决策和最终 verifier/acceptance，不作为普通 Spark repair worker。

未来可以增加有界 Spark→Luna→Terra repair policy、升级原因和 escalation receipt，但当前没有 generic Spark escalation contract，也不能声称已经按该链路自动升级或统计。

## 8. 验收边界与持续更新

v1.8.2 当前的最小可证明闭环如下；实现状态与真实生产 Evidence 分开记录：

| 验收项 | 证据要求 | 当前设计状态 |
|---|---|---|
| 外部基线可追溯 | 随包 baseline JSON 保留来源、benchmark 版本、模型/家族、task types、分数（若有）、provenance、transfer weight | `implemented`；原始评测配置、样本量、置信区间等并非全部具备 |
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
- 本机 Workbench 的真正目标是“在用户仓库、指定权限和实际验收命令下成功交付”。因此长期观察必须最终以真实 receipt、first-pass/final acceptance、返工、duration、lane 利用率和明确可归因的结果为准；当前没有 `failure_origin` 分类器，未知 process failure 不能被强行归因给模型。

作为新鲜度与污染讨论的补充，可参阅 [SWE-rebench](https://arxiv.org/abs/2505.20411)；它不替代上述四个主来源，也不直接提供 Workbench 当前模型的本机质量证明。

当前问题：v1.8.2 已实现公开 benchmark baseline、append-only runtime ledger、advisory posterior、content-addressed snapshot pinning 和 Spark logical lane；`performance calibrate` 当前只返回 `cold-start`/`ok`。尚无 `baseline`/`shadow`/`calibrated`/`stale` 晋级生命周期、quota reservation、failure_origin、eligible/routed/escalation 或 queue-wait 指标，也没有足够真实生产 receipt 可宣称动态质量校准完成。

下一步：在不触发无意义模型调用的前提下，持续收集获准真实任务的 actual model/Agent version、first-pass/final acceptance、rework、duration 与 Spark lane 指标；达到真实 Evidence 后再单独设计并验收 promotion、failure-origin、reservation 或更丰富的 queue/escalation 指标。
