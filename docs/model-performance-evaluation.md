# 型号、性能清单与路由效果验收

v1.12.0 增加精确型号与可观察的效果评估。功能测试、数据参与路由、真实交付收益分别验收。

## GPT-6 Astra

精确型号 `gpt-6-astra` 可用于显式选择的 planner、verifier 和跨模块 control；原有默认仍为
Sol，Spark/Luna/Terra/Claude 的日常分工继续保留。Astra 未提供 benchmark 观测时，score
保持缺失，不能复制 Sol 分数或将 API 价格当作订阅剩余配额。默认控制面 effort 为 max；
Codex CLI 的实际 supported efforts 单独记录，不把 CLI 的 ultra 能力冒充 API 合同。

来源：[OpenAI Astra 模型页](https://developers.openai.com/api/docs/models/gpt-6-astra)、
[模型使用说明](https://developers.openai.com/api/docs/guides/latest-model)。API 工具特性与本机
Codex 执行路径的可用性分别记录；未被本机元数据观察到的功能不会仅凭网页自动开启。

```bash
codex-workbench request --help
# 在正常 request 的既有参数后显式选择：
# --planner-model gpt-6-astra --verifier-model gpt-6-astra
```

## Claude 别名绑定

`opus`、`sonnet`、`fable` 是选择器。只有同一个正常 Workbench 原生结果回执包含请求选择器、
精确 actual_model、当前 CLI 版本、真实观察时间和成功进程退出，才形成绑定。跨 provider
fallback、fixture、verifier、Evidence reuse、缺字段、跨家族、过期或矛盾证据不能形成绑定。
绑定以七天有效期约束新快照；已创建任务使用自己的固定快照，刷新不重写旧任务。

绑定和来源 provenance 写入既有 performance snapshot，沿用既有内容寻址。外部先验按
canonical model 匹配；候选仍保留其原始选择器，运行指标按 actual_model/Agent version/effort/
task/complexity 查询。外部观测不通过家族回退转移给其他版本；没有证据时报告 unresolved。

```bash
codex-workbench performance identities
codex-workbench ai-frontier status
```

## 所有模型性能清单

```bash
codex-workbench performance list --format json
codex-workbench performance list --format csv
codex-workbench performance list --format html
```

三个格式来自同一份数据，读取本地 LKG，不触发网络、登录或模型调用。包含已采集的所有
模型，即使该型号不在当前可路由目录中；按来源、型号、档位、benchmark 与时间保留独立
观察，不将 Terminal-Bench、SWE、Radar IQ 与 AI Frontier Quality 合并成统一排行榜。

| 来源 | 保留字段 | 解释 |
|---|---|---|
| Radar | IQ、通过率、样本数、美元成本、时延、档位 | 来源自身测试，不是 Workbench 交付率 |
| AI Frontier | Quality、Consistency、标准差、相对成本、分类 | Consistency 不代表正确率；成本不推定美元 |
| bundled baseline | 论文/官方 benchmark、版本、分数、迁移权重 | 小权重冷启动假设 |
| runtime ledger | 首轮/最终验收、返工、时延、样本量、实际版本 | 版本或结果未证明时质量数据保持缺失 |
| catalog-only | 角色、型号、已观测能力，性能 N/A | 包括尚无数据的 Astra |

JSON 可由未来 DSH 消费者只读使用；本版本不修改 DSH，不共享 Workbench 任务库。

## 效果评估

```bash
codex-workbench performance evaluate --requests routing-requests.json
```

输入为 route_capability_snapshot 的请求对象数组。每条请求保留 task_type、complexity、
验收风险、权限、作用域、配额认证与容量。输出使用相同请求分别计算：

1. `declared_baseline`：目录声明，不使用性能校准；它不是旧版本生产路由器的精确重放。
2. `without_ai_frontier`：内置基线、Radar 与本地账本。
3. `current`：加入 AI Frontier 后的当前策略。

输出包含每组可路由覆盖率、选择变化率及其分母、候选排除原因、快照身份和来源贡献。
对照只在内存中构建快照，不改变活动 performance generation。合成请求须标记为 scenario；
历史单路由结果不能推断另一模型的反事实交付效果。命令始终保持
`delivery_improvement_proven=false`，实际节省值缺失。

真实收益必须在正常开发中按类型/难度匹配任务，固定验收标准并记录策略分组；比较首轮与
最终通过率、返工、提交到验收的 p50/p90、累计执行时间和可观测配额。先确定质量非退步
标准及最小有价值改善，再报告样本量与不确定范围。当前还没有自动随机分流和统计晋级
机制；没有对照样本时不能声明已提速。质量等价带和 1/p 的预期尝试值是工程启发式，
不是经过本机校准的概率保证。
