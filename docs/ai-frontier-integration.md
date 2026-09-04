# AI Frontier Provider 与 Workbench 多源调度

状态：`v1.11.0 已实现；正式测试与每次部署的运行 Evidence 分开验收`

本文定义 Workbench 对 [Martian AI Frontier](https://aifrontier.withmartian.com/) 公共数据的
自动采集、离线保存与调度使用边界。它是一个外部弱先验来源，不是 Workbench 的任务状态、
配额账本或验收系统。Workbench 仍以本地真实任务的 verifier receipt 为准，也不会修改 DSH。

## 1. 为什么接入

AI Frontier 研究 [Capability Frontier 论文](https://arxiv.org/abs/2606.26836) 讨论了多模型、
多次生成和选择策略带来的能力上界。该研究可帮助 Workbench 了解跨模型的质量、成本、稳定性
方向，但论文中的 frontier/oracle 结果是选择多个候选后的上界，不是某一个模型在本地仓库
上的通过率。因此本接入只使用可归因到具体 Executor 的聚合观测和最多八个精确模型的
benchmark 分类数据；不把 frontier 曲线绑定给单模型，也不把公开分数直接写成本机成功率。

数据的用途是冷启动和候选排序：

```text
AI Frontier public observations ──┐
Codex Radar / bundled benchmarks ─┼─► external weak priors
Workbench accepted/rework ledger ─┘              │
                                                 ▼
                         hard gates → calibrated quality band
                                   → time/cost/throughput/utilization
                                   → deterministic route
```

## 2. Provider 与采集合同

`ai_frontier_provider` 是与 Workbench、DSH 解耦的标准库-only Provider。其唯一持久真源为
`<state_root>/ai-frontier/ai-frontier.sqlite3`，表为：

```text
ai_frontier_snapshots       normalized content-addressed snapshots
ai_frontier_raw_payloads    accepted public JSON payloads
ai_frontier_models          Executor-level observations
ai_frontier_categories      cost and per-model benchmark categories
ai_frontier_active          one active snapshot pointer
```

每次 ingest 在同一 SQLite 事务中写入 snapshot、raw、models、categories 和 active pointer；
失败不会覆盖 last-known-good（LKG）。数据库权限由 Provider 收紧为用户私有目录；JSON
投影若存在也只是兼容读取，不是第二份状态真相。

### 2.1 允许的网络请求

Authority 的 refresh 只允许以下 HTTPS JSON 请求：

| 请求 | 用途 | 选择规则 |
| --- | --- | --- |
| `GET https://aifrontier.withmartian.com/api/reliability/leaderboard` | 两个聚合入口之一；Executor 的 `Quality`、`Cost`、`Consistency`、`Consistency Std` | 必须完整采集并保存原始 JSON 与抓取时间 |
| `GET https://aifrontier.withmartian.com/api/cost-comparison` | 两个聚合入口之二；`Quoted Cost`、`Real Cost`、`Cost Surprise` | 必须完整采集并保存原始 JSON 与抓取时间 |
| `GET https://aifrontier.withmartian.com/api/single-model/benchmarks?llm_name=...` | 精确模型的分类 benchmark 观测 | 只对当前 capability catalog 中可路由的 exact model 采集，最多 8 个；参数必须 URL 编码 |

以下内容永远不抓取：主页或 `/single-model-insights` HTML、examples、Plotly 图形或二进制
payload、frontier/oracle 图表、视频、Cookie、登录页、凭据和 API key。尤其不调用
`/api/aggregated-frontier`、`/api/frontier-shift-leaderboard` 或 model examples；它们不是本
Provider 的路由输入。若上游新增字段，Provider 只接受已声明的 JSON 合同，未识别或非法
结构进入失败路径并保留 LKG。

刷新策略固定为：默认 72 小时一次，且任何配置都不能低于 24 小时。Provider 不重试单次
请求；由 Authority 的低频 `launchd` 任务在下一次窗口再尝试。选择的八个模型来自当时
active capability catalog 的 `routable=true` 精确 ID；不把 `claude`/`gpt` family alias
扩展成猜测的服务端版本。没有有效 catalog 时仍可保存两个聚合观测，但不会产生可路由
model benchmark 请求。

### 2.2 Consent、安装与双机所有权

安装器只安装 Provider、配置、LaunchAgent 和目录，不创建 consent receipt，也不联网。
网络请求必须满足无秘密的本地 receipt：

```json
{
  "schema": "ai-frontier-provider-authorization",
  "version": 1,
  "provider": "ai-frontier",
  "status": "consented",
  "basis": "local_operator_consent",
  "scope": ["public-json"],
  "accepted_at": "<UTC_TIMESTAMP>",
  "attribution": "数据来自 Martian AI Frontier aifrontier.withmartian.com",
  "terms_url": "https://withmartian.com/terms-of-service",
  "not_official_authorization": true
}
```

`status=consented` 表示本地操作者决定以个人用途读取公共 JSON，绝不等于 Martian 官方授
权、API 许可或对其数据条款的豁免。使用前应阅读 [Martian Terms of Service](https://withmartian.com/terms-of-service)。
receipt 不能含 token、Cookie、密码、secret、API key 或任何登录材料；缺 receipt、receipt
不完整或被撤销时，网络请求数必须为零。 `status=unauthorized` 是正确的 fail-closed 状态，
不是触发自动登录或循环重试的理由。

Mac mini 是唯一 Authority writer：它运行 refresh、写入 SQLite/LKG、物化 performance
snapshot，并向 HTTP API 提供只读视图。MacBook 仅通过 SSH/Tailscale/MCP 读取
`/api/ai-frontier`、`/api/performance` 和状态；不会安装第二个 writer、复制 SQLite 或在
离线端刷新。断网时读取 Mac mini 上的 LKG；若 Mac mini 不可达，调度沿用已 pin 的 snapshot
或随包基线，不伪造新数据。

## 3. 数据语义与不可混淆的字段

AI Frontier 的字段是发布者的外部观察，不是 Workbench 的本地结果：

| 字段 | 在 Provider 中的语义 | 调度使用 | 明确禁止 |
| --- | --- | --- | --- |
| `Executor` / `LLMs` | 来源侧模型标识，归一化为 provider + exact `model_id` | 仅 exact identity 对齐 capability catalog | 把 family alias 或未知 ID 自动变成可路由模型 |
| `Quality` | 跨 benchmark 的 accuracy/quality 观察，属于弱先验 | 进入外部 prior，必须经过迁移折扣与质量等价带 | 当作 Workbench `first_pass`、`accepted` 或 verifier 通过率 |
| `Consistency` / `Consistency Std` | 多次观察的稳定性或离散程度信号 | 降低不确定性或风险排序 | 把 `Consistency=100%` 当作“总是正确”；一个模型可以稳定地错 |
| `Real Cost` | 来源发布者定义的 API/相对成本观察；单位和口径随来源 | 在质量等价带内作为成本排序信号 | 当作 Codex/Claude 订阅剩余额度、五小时/周 quota 或可用并发 |
| `Quoted Cost` / `Cost Surprise` | 报价与真实 API 成本的比较观察 | 成本模型的解释字段、异常提醒 | 用它绕过 Claude 20% 保留和 25% 停线 |
| per-model benchmark category | 某 exact Executor 的分类 benchmark 观测 | 仅按匹配 domain 形成弱 prior | 把 category score 直接当本机 acceptance rate |
| frontier/oracle aggregate | 多模型选择后的研究上界或曲线 | 仅用于研究注释与候选多样性讨论 | 绑定给单模型、生成单模型质量分或直接路由 |

AI Frontier 当前响应没有可证明的 Workbench sample count、本地 harness、Agent CLI 版本、
仓库分布或验收协议；Provider 只保存 `fetched_at`、来源 URL、原始 payload digest 和归一化
快照。Quality、Consistency、Cost 的来源定义若发生变化，先停止接纳并等待合同更新，不能
静默解释为新的本机指标。

## 4. 快照、LKG 与可观测入口

Provider snapshot 固定以下身份与边界：`schema_version`、`snapshot_id`、content `digest`、
source URLs、attribution、authorization、`ingest_mode`、`fetched_at`、models、categories、
raw payload digest、cache state，以及：

```text
routing_boundary.frontier_oracle_collected        = false
routing_boundary.frontier_oracle_used_for_routing = false
routing_boundary.model_observations_are_not_success_rates = true
```

消费状态建议使用 `fresh`、`stale`、`expired`、`unavailable`：默认抓取后七天内为 fresh，
七天到 31 天为 stale 并降权，超过 31 天保留审计但不影响新任务路由。刷新失败、网络断开、
时间戳倒退或 schema 不兼容时，不覆盖 LKG；过期或 unavailable 时回到随包 benchmark
baseline。Provider 的 freshness 不改变已创建任务的 pinned snapshot。

预期的 CLI facade 使用 `ai-frontier ...` 命令组（Workbench 安装版以
`codex-workbench ... ai-frontier ...` 暴露）：

```bash
# 以下命令名是 Provider/Workbench 的稳定合同；具体发行版先以 --help 核对
codex-workbench --home "$WB_STATE_ROOT" ai-frontier status
codex-workbench --home "$WB_STATE_ROOT" ai-frontier show
codex-workbench --home "$WB_STATE_ROOT" ai-frontier consent-personal-use
codex-workbench --home "$WB_STATE_ROOT" ai-frontier refresh

# 只读 Authority HTTP 视图
curl -H "Authorization: Bearer $WB_TOKEN" \
  http://127.0.0.1:8766/api/ai-frontier
curl -H "Authorization: Bearer $WB_TOKEN" \
  http://127.0.0.1:8766/api/performance
```

`status`、`show` 和上述 GET 不联网；`consent-personal-use` 只写本地 receipt；`refresh` 仅
Authority 可运行，并在 consent 有效时请求固定 JSON。所有命令应输出 `model_calls=0`；
安装与 consent 也不触发 Claude/Codex 登录或模型调用。

## 5. 综合调度算法

调度器不能用一个“综合总分”吞掉质量、成本和速度。候选先过硬门禁，再把外部弱先验与
本地长期账本合成为同一 exact context 的质量后验，先选质量等价带，最后才比较效率。

### 5.1 输入与隔离键

当前本地运行账本按如下精确上下文键聚合；若字段无法证明，则不做跨桶迁移：

```text
(provider, exact_model_id, agent_version, reasoning_effort,
 task_type, complexity)
```

`harness_profile` 与 tool/permission contract 仍在路由前执行硬门禁，但 v1.11.0 尚未把它们
加入本地校准桶键，不能宣称已按这两个维度隔离历史样本。

外部来源按 `source + benchmark family + fetched_at` 分组；同一 benchmark family 的多个表不
重复计权。AI Frontier、Codex Radar 和随包基线不直接平均：它们只提供先验参数与来源
可靠性，且各 source cluster 有总强度上限。Workbench 本机 eligible terminal attempts
才贡献已判定的 `success/failure`；fixture、deterministic、verifier、Evidence reuse、缺
少 actual model/version、认证/配额阻断、网络/主机/harness 未归因的 indeterminate 不进入
Beta 成功/失败分母。

### 5.2 外部先验与本地 Beta 校准

对每个 exact context 建立 Beta 后验：

```text
external_prior = source_weight × identity_match × domain_match
                 × freshness × benchmark_cluster_cap

posterior = Beta(alpha_external + local_successes,
                 beta_external  + local_failures)
quality_lcb95 = max(0, posterior_mean - 1.96 * posterior_stddev)
```

Quality 的外部百分比先归一化为 accuracy prior，不能直塞为 first-pass。Consistency 只调节
不确定性或风险权重；它不增加 successes。Real Cost 进入成本观测，不进入质量后验；quota
remaining 只来自现有可信 Claude quota snapshot，不能由 AI Frontier 推导。

外部先验应保持很小的等效样本上限，并随新鲜度递减；本地每一次有可判定的真实
first-pass/final acceptance 都会累计。因而长期运行后，local evidence 的 effective count
自然超过并压过外部先验；刷新 AI Frontier 不会抹掉本地历史。新 Agent/CLI 版本形成新桶，
不会把旧版本数据冒充新版本。

### 5.3 质量等价带与效率排序

先在所有硬门禁通过的候选中取得最高 `quality_lcb95`，再按任务风险建立质量等价带：

| 验收风险 | 允许的质量下界差距 |
| --- | ---: |
| `critical` | 0 个百分点 |
| `high` | 0.5 个百分点 |
| `standard` | 2 个百分点 |
| `low` | 4 个百分点 |

只有落入最高质量候选等价带的模型才进入效率比较。带内按以下顺序减少预期交付代价：

```text
用户/角色偏好优先
expected_attempts 越低
rework_rate 越低
执行时延越低
consistency risk 越低
发布者相对成本越低
catalog cost units 越低
accepted throughput 越高
lane/quota utilization 越低
source/model/provider 的稳定 deterministic tie-break
```

可实现的解释性 utility 形式为：

```text
expected_useful_rate = quality_lcb95 × (1 - rework_rate)
expected_time = queue_wait + execution_latency + verifier_latency
efficiency = expected_useful_rate / max(expected_time, epsilon)
```

它只能在质量等价带内排序，不能用更快或更便宜买回硬质量差距。并行执行时，只有切片
scope 不冲突、独立验收且 marginal expected useful rate 足以覆盖额外 cost/slot 的候选才
能同时启动；相关性过高的候选不会重复启动。确定性 tie-break 确保同一 pinned catalog、
performance snapshot 和 policy 得到相同路由结果。

### 5.4 硬门禁与配额不变式

算法顺序必须是：

```text
capability/role/task/scope/tool/permission/agent-version gates
        → Claude auth + fresh quota + 20% reserve / 25% stop-line
        → concurrency / Spark lane capacity
        → external prior + local Beta calibration
        → quality equivalence band
        → time/cost/throughput/utilization
        → deterministic tie-break
```

Sol 规划、跨模块决策和最终验收职责不被外部数据重新分配；Spark 仍只处理边界清晰、可
机械验收的低风险切片。Claude 的 20% 保留目标、30% admission guard、25% stop line 和
认证 fail-closed 不会因为 AI Frontier 的 Quality 或 Real Cost 改变。Codex/Spark 没有可证
明的订阅剩余额度时仍显示 `N/A`。

## 6. 安装验收与失败处理

安装器的预期验收命令（命令组统一称为 `ai-frontier ...`）如下：

```bash
# 1. 安装前：只读检查，不写 receipt、不联网
codex-workbench --home "$WB_STATE_ROOT" ai-frontier --help
codex-workbench --home "$WB_STATE_ROOT" ai-frontier status

# 2. 安装后：确认配置和 Authority-only collector
codex-workbench --home "$WB_STATE_ROOT" doctor
codex-workbench --home "$WB_STATE_ROOT" ai-frontier status
launchctl print "gui/$(id -u)/com.lisihao.codex-workbench-ai-frontier"

# 3. 明确决定启用个人公共 JSON 后，才创建本地 receipt
codex-workbench --home "$WB_STATE_ROOT" ai-frontier consent-personal-use
codex-workbench --home "$WB_STATE_ROOT" ai-frontier status

# 4. 在有效 consent 下由 Mac mini 主动刷新；不触发模型调用
codex-workbench --home "$WB_STATE_ROOT" ai-frontier refresh
curl -H "Authorization: Bearer $WB_TOKEN" \
  http://127.0.0.1:8766/api/ai-frontier
curl -H "Authorization: Bearer $WB_TOKEN" \
  http://127.0.0.1:8766/api/performance
```

验收应确认：状态输出 provider/schema/path/row counts、`snapshot_id`/digest、fetched_at、
freshness、来源归因和 `routing_boundary`；refresh 的请求列表最多两个聚合 + 八个当前
exact model benchmark；无 consent 时 `network_requested=0`；安装前后都没有 model call、
登录、Cookie 或 credential；MacBook 只能读；断网后 status/show 仍能读 LKG，刷新失败不破坏
旧 snapshot。HTTP `/api/ai-frontier` 是观察视图，不授权客户端写 Provider SQLite。

以下情况必须 fail closed 并报告，不得静默扩大抓取范围：endpoint 改成 HTML/Plotly、字段
语义不明、时间戳倒退、模型不在当前 catalog、超过八个模型、低于 24 小时刷新间隔、receipt
含秘密、SQLite 事务失败或 MacBook 试图执行 writer 操作。没有 AI Frontier 数据时，Workbench
仍可用 bundled baseline + 本地账本调度；没有本地 receipt 时不把“无数据”报告成网络故障。

## 7. 未来 DSH 消费者

DSH 后续如需要复用，只读取 Provider 稳定 CLI/JSON snapshot 合同，校验 schema、digest、
authorization、freshness 和 `routing_boundary`，并将 snapshot ID/digest 固定到自己的 Run。
DSH 不应复制 `ai-frontier.sqlite3`、Workbench task SQLite 或调度器；不应把 AI Frontier
Quality 写成本机 Evidence、first-pass 或 quota。DSH 接入是后续独立任务，本次文档和实现不
修改 DSH 源码。

当前问题：AI Frontier 的公开聚合数据可作为按 exact model/domain 的冷启动弱先验，但它
没有本地 harness、Agent 版本、真实验收样本或可证明 quota；真实生产采集和长期校准仍需
Mac mini 上的 consent、LKG 与运行 Evidence。

下一步：完成 Provider/Workbench 代码与 focused tests 后，在 Mac mini 仅由操作者决定是否
写入 personal-use receipt，执行一次 `ai-frontier refresh`，核对 SQLite 快照、最多八个
精确模型请求、`/api/ai-frontier` 和 performance provenance；随后以真实 first-pass、返工、
时延、吞吐和利用率长期校准外部先验。
