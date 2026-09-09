# 任务控制与失败恢复

Codex Workbench 以 Authority 的 SQLite 数据库作为任务状态、节点 attempt、工作树 allocation、指导和 Evidence 的唯一权威。失败恢复复用已有的 DirtyWorktreeRecovery、ArtifactStore 和 allocation 账本；它不会建立第二份恢复数据库，也不会从生成的运行目录反向覆盖源码仓库。

MCP 将工作树恢复拒绝作为当前请求的 `isError` 返回，连接继续处理后续请求；拒绝不代表节点已恢复或启动。

原 attempt 已提交 checkpoint 时，MCP `resume` 可显式提供 `expected_checkpoint_sha`，CLI `task resume-blocked-worktree` 对应 `--expected-checkpoint-sha`。必须使用核实过的完整 SHA；未提供时仍要求 HEAD 等于合同 base。恢复核对同一 Git 仓库、分配分支、base 祖先关系、原 attempt 已报告的文件列表和写入范围，将 checkpoint SHA 与补丁哈希封存在现有恢复回执中。新 attempt 从原 base／依赖输入恢复完整差异，不重写旧提交。HEAD 或补丁在封存后变化均拒绝恢复；其他分支的新功能不会自动合入旧任务。

## 调用方 revision 与原子控制

外部 CLI、HTTP 和 MCP 的 queue、resume、pause、cancel、steer 必须传入刚读取的 expected_revision。服务端比较该 revision；陈旧、缺失、布尔值或字符串值都不能启动节点。内部创建任务后立即排队的可信路径仍可调用底层 store 接口，但不得用于外部控制。

带 instruction 的 queue/resume 使用一个 SQLite 事务完成以下操作：

1. 校验调用方 revision。
2. 校验 instruction 是去除首尾空白后 1–500 字符的字符串。
3. 保存 instruction 并递增 revision。
4. 建立失败 attempt 的恢复绑定。
5. 将任务切换到 queued。

任何一步失败都会回滚整个事务。因而不会再出现“指导被拒绝，但任务已经启动”的状态。

    codex-workbench task get <task-id>
    codex-workbench task resume <task-id> --expected-revision <revision> \
      --instruction "继续现有修改，只修复当前失败"
    codex-workbench task steer <task-id> "下一 attempt 使用这条指导" \
      --expected-revision <revision>

## 指导回执

执行器在节点 claim 时取得不可变指导快照。保存一条指导不等于已把它注入正在运行的进程：

- status=not_delivered：已保存，但还没有 attempt 取得它。
- scheduled_for=next_attempt：任务已可调度，下一次 claim 会取得它。
- scheduled_for=future_attempt：已有 attempt 正在运行；该进程不会收到后来追加的指导。
- scheduled_for=after_resume 或 after_queue：任务尚不可 claim，需先恢复或排队。

task.steering_delivered 事件绑定 steering_id、node_id 和 attempt。它证明该 attempt 的执行请求取得了指导快照，不代表对已启动进程进行了交互式终端 attach。

## 失败 attempt 续修

普通 Worker 失败时，协调器从实际工作树计算相对于该节点固定 input tree 的 changed paths；它不信任模型自行声明的文件列表。若仍在重试额度内，结算事务建立 capture_pending 绑定，自动重试和人工 queue 使用同一恢复路径。

下一 attempt 的准备顺序是：

1. 验证 source allocation、attempt、branch、base 与物理工作树一致。
2. 验证已记录的 dependency-input 正好等于当前已验收祖先闭包。
3. 比较 source 的真实 tracked/untracked 文件集与失败 receipt。
4. 检查每个路径同时满足任务 allowed/forbidden scope 与节点 write scope。
5. 内容寻址保存 binary patch，并固定 SHA-256。
6. 无依赖根 Worker 以合同 `base_sha` 的 tree 复原自己的 patch；有依赖 Worker 则先复原其已记录的 dependency-input，再复原自己的 patch。
7. 再次核对 source 未漂移、target patch 哈希一致，然后以 lease CAS 写入 allocation。
8. 只有 assignment 成功后才调用原 Worker 执行器。

恢复验收仍以无 shell 的 argv 方式执行。合同命令可以在可执行文件之前声明 `PYTHONPATH`、`PYTHONPYCACHEPREFIX` 或 `PYTHONDONTWRITEBYTECODE`；恢复器把这些前缀写入该子进程环境，同时在 Evidence 中保留完整原始命令。`PATH`、`HOME`、加载器变量及其他环境覆盖会在执行前被拒绝，不能借环境前缀绕过 argv 治理。

已验收祖先保持原 attempt 和 accepted 状态，不重新执行。tracked 与合法普通 untracked 文件均可恢复。`__pycache__/*.pyc` 是唯一可自动分类的 ignored 残留：它必须已出现在失败回执中，且物理对象必须是工作树内的普通非符号链接文件；恢复器会先把每个文件的字节、SHA-256 和路径写入 ArtifactStore，再删除缓存并只重放业务补丁。缓存已在捕获前被清理时，收据显式记录 missing path，不再把它误判为业务补丁漂移。其他 ignored 文件、符号链接、越权路径、来源漂移、丢失 allocation、错误 branch/base 或哈希不一致仍会 fail closed，原失败 attempt 仍是权威状态。

`blocked` 并非一律自动重试。只有由可信执行边界产生且显式带 `retryable=true` 的 Worker 结果，才可在 `retry_limit` 内进入与失败 attempt 相同的内容捕获和新工作树恢复路径。模型自报阻断、凭据、权限、配额、需求歧义、verifier 阻断和 `indeterminate` 结果均保持显式终态，防止无界循环或重复副作用。

MCP 控制面可以安全续跑已经进入 `blocked` 的历史 attempt，而不是把通用 `resume` 直接交给只接受 `paused`/`needs_fix` 的 queue 路径。调用方必须同时提交最新 `expected_revision`、精确 `node_id`、`expected_attempt` 和持久化 `reason`。若 receipt 含业务修改，调用方还必须声明 `confirm_recovery=true`；Workbench 在 SQLite 写事务之外捕获内容寻址补丁，再用 revision/attempt CAS 授权一次新 attempt。若 receipt 明确没有修改，只能以 `confirm_no_side_effects=true` 授权原路重试。无依赖根 Worker 与有依赖 Worker 的合法 untracked 文件都需显式 `preserve_untracked=true`；根 Worker 收据只绑定合同 base tree，绝不伪造 dependency-input。有 `depends_on` 的节点若缺少已记录 dependency-input 一律拒绝恢复。未知副作用或不确定捕获继续 fail closed。`instruction` 仍是模型指导而不是恢复理由；需要新指导时先 `steer`，再以更新后的 revision 执行 `resume`。

## 崩溃与重复请求

- assignment 前重启：recover_interrupted() 将 capture_pending 原子回滚到原失败 attempt 和 needs_fix，并在同一事务写入 orphan cleanup receipt。协调器在 SQLite 事务外把可能残留的 target 移入私有 recovery archive；归档失败或进程再次重启时，未 resolved 的 receipt 会继续重放。下一次合法 queue 再重建恢复绑定。
- assignment 后重启或执行器崩溃：target allocation 已是 attempt 的物理状态，节点进入 indeterminate。自动 retry 被明确拒绝，直到操作人完成显式恢复裁决；不会把同一 target 再派发给新 attempt。
- 重复 queue：旧 revision 失败，不会创建第二份恢复授权。
- 旧 attempt 晚到的 settlement：coordinator epoch、attempt 和 lease epoch fencing 拒绝写入。
- 暂停或取消与 settlement 竞态：节点的已租约结果可保存，但任务的 paused/cancelled 控制状态优先，不会被晚到结果推进到 verifying 或 accepted。

## Indeterminate 节点的显式本地恢复

`indeterminate` 节点若在 target attempt 已被 assign 后崩溃，会同时拥有一个物理 worktree 但没有待处理的 `recovery_json` 绑定（结算时已清空）。裸的 `resolve_indeterminate`/`decide_approval(retry)` 对这种节点始终 fail closed（`_assert_indeterminate_retry_is_safe`），因为自动重试无法证明旧执行器已经退出、也无法证明该 worktree 上的改动仍局限于节点自己的写入范围。

MCP `resolve_indeterminate_locally` 动作（CLI `task resolve-indeterminate-locally`）为这一具体场景提供显式、操作员确认的本地恢复路径，复用现有的 `failed-attempt-worktree-recovery` 绑定、捕获与预派发恢复机制，而不是重新实现一套并行系统：

1. `WorkbenchStore.indeterminate_local_recovery_candidate` 只读校验：任务必须仍处于 `needs_approval`，节点必须是 `indeterminate`、没有待处理的 `recovery_json`、拥有一个仍然 `active` 的物理 worktree allocation，且该 allocation 与任务合同的 repository/base_sha 一致。该合同还必须明确禁止 external write 与 destructive action；本恢复路径只适用于可由本地 worktree 证据覆盖的执行。
2. `observed_indeterminate_recovery_paths`（`dirty_worktree_recovery.py`）在 SQLite 事务之外检查该 worktree：拒绝任何越权 ignored 路径、越出任务 `allowed_scope`/`forbidden_scope` 或节点 `write_scopes` 的路径、以及符号链接；只返回可信的 tracked/untracked changed_paths 与 generated-residue 路径。
3. 调用方必须显式声明 `confirm_old_executor_ended=true`（旧执行器进程已退出的证据，例如 `ps`/`pgrep` 核实）与 `confirm_effects_restricted_to_owned_files=true`（第 2 步已核实的范围结论）；这是记录在案的操作员断言，不是自动验证。
4. `WorkbenchStore.queue_indeterminate_local_recovery` 用观测到的 changed_paths 构造与失败 attempt 完全相同形状的 `capture_pending` 恢复绑定（`_failed_attempt_recovery_authorization`），把节点原子地转回 `pending` 并清空其 `worktree`，同时决定任何待处理的 `indeterminate_resolution` approval。
5. 该节点重新排队后，协调器沿用既有的失败 attempt 续修流程：在新的干净 attempt 上捕获补丁、复原已记录的 dependency-input、核对范围与哈希，只有装配成功后才派发执行器；indeterminate 源 worktree 本身永不被复用为派发目标，assign 成功后其 allocation 转为 superseded。
6. 若节点 `depends_on` 其他节点，调用方必须显式提供该节点原 attempt 记录的 `dependency-input` artifact ref（结算前已写入 ArtifactStore，即使进程随后崩溃也仍然存在），否则拒绝恢复，防止用未核实的祖先输入静默替换已验收的依赖。

与 `resume-blocked-worktree`/`retry-blocked` 一样，`confirm_*` 字段是留痕断言而非自动核验；未知副作用、越权路径、哈希漂移或过期 revision/attempt 一律 fail closed。

### 提供方响应故障后的重新准入

Claude 执行器明确记录的 CLI 响应解析故障（`claude-executor-failed`，原因以 `Claude structured result rejected: CLI ` 开头）只决定失败源 attempt 的 Codex fallback，不再永久覆盖后续 attempt 的冻结 Claude 候选。已授权的新 attempt 会重新经过原有能力、合同、认证、配额和容量检查；`node.started.provider_readmission` 记录源 attempt 与重新准入意图，不代表 Claude 已实际执行。运行中的 attempt 不变，恢复绑定及源工作树内容不变。配额/认证拒绝、业务失败及未知原因仍沿用原 fallback 行为；这不是自动解禁 `claude_allowed=false` 或自动重试未知副作用。

## SQLite 事务边界

Git repository identity、工作树检查、dependency-input 复原、patch 捕获、Artifact 读取与 SHA-256 校验均在 SQLite 写事务之外运行。写事务只重新核对持久字段、revision、attempt、coordinator/lease epoch、预验证签名和 allocation 状态，再提交短时状态变更。并发期间出现预取快照中没有的新仓库时，本轮 claim 放弃并由下一轮重新读取，不会在写锁内执行 Git。

这些保证由 fixture 仓库和临时数据库测试验证；它们不需要 Codex/Claude 登录、真实套餐调用或生产 SQLite 写入。
