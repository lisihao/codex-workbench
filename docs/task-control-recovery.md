# 任务控制与失败恢复

Codex Workbench 以 Authority 的 SQLite 数据库作为任务状态、节点 attempt、工作树 allocation、指导和 Evidence 的唯一权威。失败恢复复用已有的 DirtyWorktreeRecovery、ArtifactStore 和 allocation 账本；它不会建立第二份恢复数据库，也不会从生成的运行目录反向覆盖源码仓库。

MCP 将工作树恢复拒绝作为当前请求的 `isError` 返回，连接继续处理后续请求；拒绝不代表节点已恢复或启动。

原 attempt 已提交 checkpoint 时，MCP `resume` 可显式提供 `expected_checkpoint_sha`，CLI `task resume-blocked-worktree` 对应 `--expected-checkpoint-sha`。必须使用核实过的完整 SHA；未提供时仍要求 HEAD 等于合同 base。恢复核对同一 Git 仓库、分配分支、base 祖先关系、原 attempt 已报告的文件列表和写入范围，将 checkpoint SHA 与补丁哈希封存在现有恢复回执中。新 attempt 从原 base／依赖输入恢复完整差异，不重写旧提交。HEAD 或补丁在封存后变化均拒绝恢复；其他分支的新功能不会自动合入旧任务。

## 规划错误保留和公开摘要

完整的规划错误保留在私有 `planning_requests.error`；`error_ref` 是内容关联的 SHA 摘要，不是可读取的 ArtifactStore 地址。公共响应只给摘要，重规划时的 `planning_feedback` 仍最多传入前 1000 个字符。旧版本已截断的错误无法追补。

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

已验收祖先保持原 attempt 和 accepted 状态，不重新执行。tracked 与合法普通 untracked 文件均可恢复。`__pycache__/*.pyc` 是唯一可自动分类的 ignored 残留：它必须已出现在失败回执中，且物理对象必须是工作树内的普通非符号链接文件；恢复器会先把每个文件的字节、SHA-256 和路径写入 ArtifactStore，再删除缓存并只重放业务补丁。缓存已在捕获前被清理时，收据显式记录 missing path，不再把它误判为业务补丁漂移。`node_modules`、`.pnpm-store` 和编译 `lib` 目前没有同时绑定 source allocation、attempt、worktree 与生成输入（依赖还需要 lockfile）的持久平台证据，因此仍作为未知 ignored 内容拒绝；不得按目录名放行或删除依赖。其他 ignored 文件、符号链接、越权路径、来源漂移、丢失 allocation、错误 branch/base 或哈希不一致同样 fail closed，原失败 attempt 仍是权威状态。涉及路径的恢复错误只报告总数、最多 8 条转义样本和省略标记，避免把大型依赖树回传到控制面。

`blocked` 并非一律自动重试。只有由可信执行边界产生且显式带 `retryable=true` 的 Worker 结果，才可在 `retry_limit` 内进入与失败 attempt 相同的内容捕获和新工作树恢复路径。模型自报阻断、凭据、权限、配额、需求歧义、verifier 阻断和 `indeterminate` 结果均保持显式终态，防止无界循环或重复副作用。

MCP 控制面可以安全续跑已经进入 `blocked` 的历史 attempt，而不是把通用 `resume` 直接交给只接受 `paused`/`needs_fix` 的 queue 路径。调用方必须同时提交最新 `expected_revision`、精确 `node_id`、`expected_attempt` 和持久化 `reason`。若 receipt 含业务修改，调用方还必须声明 `confirm_recovery=true`；Workbench 在 SQLite 写事务之外捕获内容寻址补丁，再用 revision/attempt CAS 授权一次新 attempt。若 receipt 明确没有修改，只能以 `confirm_no_side_effects=true` 授权原路重试。无依赖根 Worker 与有依赖 Worker 的合法 untracked 文件都需显式 `preserve_untracked=true`；根 Worker 收据只绑定合同 base tree，绝不伪造 dependency-input。有 `depends_on` 的节点若缺少已记录 dependency-input 一律拒绝恢复。未知副作用或不确定捕获继续 fail closed。`instruction` 仍是模型指导而不是恢复理由；需要新指导时先 `steer`，再以更新后的 revision 执行 `resume`。

### Blocked 历史回执的 source-only 恢复

旧版失败路径观察可能把 Git ignored 依赖和构建文件并入 `changed_paths`。严格恢复仍逐项比对原回执，不会猜测哪些历史路径可以丢弃。MCP `workbench_control_task` 的 blocked `resume` 和 CLI `task resume-blocked-worktree` 另提供显式 source-only 分支：它只提取当前真实的非 ignored 源码差异，不改写旧回执。

请求必须绑定最新 `expected_revision`、节点 `expected_attempt`、`reason`，并提供 `source_only=true`、`confirm_source_only_extraction=true`、`confirm_preserve_unknown_ignored=true`。有合法 untracked 文件时还需 `preserve_untracked=true`。先以 `dry_run=true` 获取 `source_delta_sha256`，再以相同 revision/attempt 和 `expected_source_delta_sha256` 应用。此分支不要求旧的 `confirm_recovery` 或“历史无副作用”断言；旧严格分支继续要求其原确认。

```sh
codex-workbench task resume-blocked-worktree <task-id> <node-id> \
  --expected-revision <revision> --expected-attempt <attempt> \
  --reason "提取当前经过核实的源码，保留旧回执与 ignored 内容" \
  --source-only --confirm-source-only-extraction \
  --confirm-preserve-unknown-ignored --dry-run
```

正式应用去掉 `--dry-run` 并加 `--expected-source-delta-sha256 <preview-digest>`，不是重复提交旧 recovery file。预检不创建正式工件、事件或新 attempt；正式应用会 CAS 授权 deterministic clean-target 恢复。源分支/base、物理 allocation、已验收祖先输入、Task 与 Node scope、文件类型、路径/模式/字节摘要必须同时匹配。源码变化或持久状态漂移需要重新预检，不能只复用相同路径名。

旧 result 原文保留在恢复授权中，`historical_effects=unknown`、`retrospective_compliance_claimed=false`、`external_replay_authorized=false` 保持明确。ignored 留在被 hold 的 source allocation，不删除、复制或重放；本地恢复合同必须仍禁止 external write 与 destructive action。新 target 沿用现有 deterministic `prepare` 和合同冻结的 acceptance commands，不重新调用模型；Worker 成功也不替代独立 verifier 的 accepted 转移。验证命令需要的局部 IPC／Git 快照权限见 [受限验证执行](controlled-validation.md)，它们不是 source-only 参数隐含授予的权限。

新的失败路径观察只把真实源码差异和既有可识别 Python bytecode 残留写入 `changed_paths`；其他 ignored 路径以独立、有界摘要留证，并继续阻断无条件自动重试。是否忽略由 Git 判定，不能按 `lib` 或 `node_modules` 的目录名排除合法 tracked 源码。

## 崩溃与重复请求

MCP 的 blocked `resume` 支持布尔值 `dry_run=true`：无修改分支只返回重试预览，有修改分支在临时 ArtifactStore 中验证恢复补丁；两者均不写入任务、节点、事件或正式工件，不启动下一 attempt，也不修改源工作树。重复预检保持相同持久状态。字符串或数字形式的 `dry_run` 一律拒绝。除该路径、`resolve_indeterminate_locally` 与 `normalize_indeterminate_scope` 外，其他控制动作不支持预检并明确报错；HTTP control 端点同样拒绝 `dry_run=true`，不会将预检静默执行为真实操作。

- assignment 前重启：recover_interrupted() 将 capture_pending 原子回滚到原失败 attempt 和 needs_fix，并在同一事务写入 orphan cleanup receipt。协调器在 SQLite 事务外把可能残留的 target 移入私有 recovery archive；归档失败或进程再次重启时，未 resolved 的 receipt 会继续重放。下一次合法 queue 再重建恢复绑定。
- assignment 后重启或执行器崩溃：target allocation 已是 attempt 的物理状态，节点进入 indeterminate。自动 retry 被明确拒绝，直到操作人完成显式恢复裁决；不会把同一 target 再派发给新 attempt。
- 重复 queue：旧 revision 失败，不会创建第二份恢复授权。
- 旧 attempt 晚到的 settlement：coordinator epoch、attempt 和 lease epoch fencing 拒绝写入。
- 暂停或取消与 settlement 竞态：节点的已租约结果可保存，但任务的 paused/cancelled 控制状态优先，不会被晚到结果推进到 verifying 或 accepted。

## Indeterminate 节点的显式本地恢复

`indeterminate` 节点若在 target attempt 已被 assign 后崩溃，会同时拥有一个物理 worktree 但没有待处理的 `recovery_json` 绑定（结算时已清空）。裸的 `resolve_indeterminate`/`decide_approval(retry)` 对这种节点始终 fail closed（`_assert_indeterminate_retry_is_safe`），因为自动重试无法证明旧执行器已经退出、也无法证明该 worktree 上的改动仍局限于节点自己的写入范围。

MCP `resolve_indeterminate_locally` 动作（CLI `task resolve-indeterminate-locally`）为这一具体场景提供显式、操作员确认的本地恢复路径，复用现有的 `failed-attempt-worktree-recovery` 绑定、捕获与预派发恢复机制，而不是重新实现一套并行系统：

1. `WorkbenchStore.indeterminate_local_recovery_candidate` 只读校验：任务必须仍处于 `needs_approval`，节点必须是 `indeterminate`、没有待处理的 `recovery_json`、拥有一个仍然 `active` 的物理 worktree allocation，且该 allocation 与任务合同的 repository/base_sha 一致。该合同还必须明确禁止 external write 与 destructive action；本恢复路径只适用于可由本地 worktree 证据覆盖的执行。
2. `observed_indeterminate_recovery_paths`（`dirty_worktree_recovery.py`）在 SQLite 事务之外检查该 worktree：默认拒绝任何未知 ignored 路径、越出任务 `allowed_scope`/`forbidden_scope` 或节点 `write_scopes` 的路径、以及源码符号链接、父链逃逸或非常规文件；只返回可信的 tracked/untracked changed_paths 与 generated-residue 路径。
3. 所有模式都要求 `confirm_old_executor_ended=true`。严格模式还要求 `confirm_effects_restricted_to_owned_files=true`；当前文件差异或进程结束本身不是完整历史副作用证明。source-only 模式使用下述源码提取授权，不要求或记录这个历史副作用断言。
4. `WorkbenchStore.queue_indeterminate_local_recovery` 用观测到的 changed_paths 构造与失败 attempt 完全相同形状的 `capture_pending` 恢复绑定（`_failed_attempt_recovery_authorization`），把节点原子地转回 `pending` 并清空其 `worktree`，同时决定任何待处理的 `indeterminate_resolution` approval。
5. 该节点重新排队后，协调器沿用既有的失败 attempt 续修流程：在新的干净 attempt 上捕获补丁、复原已记录的 dependency-input、核对范围与哈希，只有装配成功后才派发执行器；indeterminate 源 worktree 本身永不被复用为派发目标，assign 成功后其 allocation 转为 superseded。
6. 若节点 `depends_on` 其他节点，调用方必须显式提供该节点原 attempt 记录的 `dependency-input` artifact ref（结算前已写入 ArtifactStore，即使进程随后崩溃也仍然存在），否则拒绝恢复，防止用未核实的祖先输入静默替换已验收的依赖。

`confirm_*` 字段是留痕授权或断言，而非全历史自动核验。严格模式的历史副作用限制不变；source-only 只授权当前已验证源码的提取和原 local-only 合同内的续修，不授权未知外部操作重放。越权路径、哈希漂移或过期 revision/attempt 一律拒绝。

### 历史 glob 范围的显式规范化

新规划的 read/write scopes 只接受精确文件、目录和根范围（独立 `*` 规范化为 `.`），拒绝嵌入式 `*`、`?`、`[`、`]`。运行时和调度器仍采用相同的路径/目录前缀匹配，不增加通用 glob 权限。

历史 `indeterminate` 节点可以通过 MCP `workbench_control_task` 的 `normalize_indeterminate_scope` 动作，将已存储的单星号文件名范围转换为唯一现存的精确文件。必填 `task_id`、`node_id`、`expected_revision`、`expected_attempt`、`scope_pattern`、`exact_path` 和非空 `reason`。星号只能出现在最终文件名中；不接受目录 glob、多个匹配、目录或符号链接目标、任务权限外路径或并发访问冲突。HTTP control 不提供此动作，且明确拒绝其专用字段。

先以布尔值 `dry_run=true` 获取预览和 `file_sha256`，不会修改任务、节点、事件、工件或源码。正式操作需携带同一 revision/attempt、预览返回的 `expected_file_sha256`，并显式指定 `confirm_scope_normalization=true`。服务核对 allocation、repository/base/branch、源路径和文件内容，在短事务内重新核对持久状态后更新范围和任务 revision。并发修改或文件哈希变化时必须重新预检。

回执包含旧/新 read/write scopes、revision_before/revision_after、attempt、allocation_id、base_sha、branch、worktree、文件哈希、理由及 event_cursor。仅规范化已声明的范围，不扩展到整个父目录，不恢复执行，也不改写历史 result、节点状态或 attempt；审计事件不能作为过去副作用合规的追认证据。之后的恢复仍需单独通过 `resolve_indeterminate_locally` 的检查和操作员确认。

### Source-only ignored 留存模式

MCP `resolve_indeterminate_locally` 或 CLI `task resolve-indeterminate-locally` 的 source-only 请求须显式提供 `source_only=true`、`confirm_source_only_extraction=true`、`confirm_preserve_unknown_ignored=true` 和 `confirm_old_executor_ended=true`（CLI 对应连字符选项）。这授权提取当前经过验证的 tracked/untracked delta 并在原合同内继续本地工作，不是宣称过去没有其他副作用。此模式拒绝 `confirm_effects_restricted_to_owned_files=true`；严格模式拒绝 source-only 专用授权和摘要字段。

先用 `dry_run=true` 获取 `source_delta_sha256`；预检不修改任务、节点、事件、正式工件或源码。正式应用必须提交该摘要作为 `expected_source_delta_sha256`，并重新通过 revision/attempt、base/allocation、任务及节点范围、源空闲和普通文件/父链检查。摘要只覆盖已验证 delta 的路径、删除状态、文件模式及内容，不哈希 ignored 内容或整个仓库。排队后到捕获期间发生内容或路径漂移也拒绝恢复。正式应用会排队新的本地 attempt，不是单纯下载文件；合同必须仍明确禁止 external write 和 destructive action。

回执及审计保留 `historical_effects=unknown`、`retrospective_compliance_claimed=false`、`external_replay_authorized=false`。原 indeterminate result 不被追认为成功或合规；恢复用 synthetic blocked 凭据只描述提取输入，不是旧执行结果。已知外部操作的状态和权限必须单独核对，不能用此字段允许重复执行。

未知 ignored 内容原地留在 source allocation，包括 `node_modules`、`.pnpm-store`、`lib` 和 `__pycache__`；均不删除、不写入 ArtifactStore、不进入 patch，也不复制到新 target。这不把它们认定为合法产物。原 allocation 的授权事件持久保留 hold，取消、重启或 recovery binding 清理后仍不能自动回收或 quarantine。新的干净 Node target 只能克隆已验证的本地 pnpm linker 模板；模板缺失、失效或不匹配时明确拒绝，绝不 seed 或执行安装，也绝不从 source 复制依赖树。source cwd 的同 UID 进程检查只是本地观察，不能证明已 chdir 的旧子进程退出或外部副作用不存在。HTTP control 拒绝这些专用字段，不会将其静默当作 queue。

### 提供方响应故障后的重新准入

Claude 执行器明确记录的 CLI 响应解析故障（`claude-executor-failed`，原因以 `Claude structured result rejected: CLI ` 开头）只决定失败源 attempt 的 Codex fallback，不再永久覆盖后续 attempt 的冻结 Claude 候选。已授权的新 attempt 会重新经过原有能力、合同、认证、配额和容量检查；`node.started.provider_readmission` 记录源 attempt 与重新准入意图，不代表 Claude 已实际执行。运行中的 attempt 不变，恢复绑定及源工作树内容不变。配额/认证拒绝、业务失败及未知原因仍沿用原 fallback 行为；这不是自动解禁 `claude_allowed=false` 或自动重试未知副作用。

## SQLite 事务边界

Git repository identity、工作树检查、dependency-input 复原、patch 捕获、Artifact 读取与 SHA-256 校验均在 SQLite 写事务之外运行。写事务只重新核对持久字段、revision、attempt、coordinator/lease epoch、预验证签名和 allocation 状态，再提交短时状态变更。并发期间出现预取快照中没有的新仓库时，本轮 claim 放弃并由下一轮重新读取，不会在写锁内执行 Git。

这些保证由 fixture 仓库和临时数据库测试验证；它们不需要 Codex/Claude 登录、真实套餐调用或生产 SQLite 写入。
