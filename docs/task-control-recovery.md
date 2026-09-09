# 任务控制与失败恢复

Codex Workbench 以 Authority 的 SQLite 数据库作为任务状态、节点 attempt、工作树 allocation、指导和 Evidence 的唯一权威。失败恢复复用已有的 DirtyWorktreeRecovery、ArtifactStore 和 allocation 账本；它不会建立第二份恢复数据库，也不会从生成的运行目录反向覆盖源码仓库。

MCP 将工作树恢复拒绝作为当前请求的 `isError` 返回，连接继续处理后续请求；拒绝不代表节点已恢复或启动。

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
6. 在新的干净 attempt 工作树复原原 dependency-input 和该 Worker 自己的 patch。
7. 再次核对 source 未漂移、target patch 哈希一致，然后以 lease CAS 写入 allocation。
8. 只有 assignment 成功后才调用原 Worker 执行器。

恢复验收仍以无 shell 的 argv 方式执行。合同命令可以在可执行文件之前声明 `PYTHONPATH`、`PYTHONPYCACHEPREFIX` 或 `PYTHONDONTWRITEBYTECODE`；恢复器把这些前缀写入该子进程环境，同时在 Evidence 中保留完整原始命令。`PATH`、`HOME`、加载器变量及其他环境覆盖会在执行前被拒绝，不能借环境前缀绕过 argv 治理。

已验收祖先保持原 attempt 和 accepted 状态，不重新执行。tracked 与合法普通 untracked 文件均可恢复。`__pycache__/*.pyc` 是唯一可自动分类的 ignored 残留：它必须已出现在失败回执中，且物理对象必须是工作树内的普通非符号链接文件；恢复器会先把每个文件的字节、SHA-256 和路径写入 ArtifactStore，再删除缓存并只重放业务补丁。缓存已在捕获前被清理时，收据显式记录 missing path，不再把它误判为业务补丁漂移。其他 ignored 文件、符号链接、越权路径、来源漂移、丢失 allocation、错误 branch/base 或哈希不一致仍会 fail closed，原失败 attempt 仍是权威状态。

`blocked` 并非一律自动重试。只有由可信执行边界产生且显式带 `retryable=true` 的 Worker 结果，才可在 `retry_limit` 内进入与失败 attempt 相同的内容捕获和新工作树恢复路径。模型自报阻断、凭据、权限、配额、需求歧义、verifier 阻断和 `indeterminate` 结果均保持显式终态，防止无界循环或重复副作用。

MCP 控制面可以安全续跑已经进入 `blocked` 的历史 attempt，而不是把通用 `resume` 直接交给只接受 `paused`/`needs_fix` 的 queue 路径。调用方必须同时提交最新 `expected_revision`、精确 `node_id`、`expected_attempt` 和持久化 `reason`。若 receipt 含业务修改，调用方还必须声明 `confirm_recovery=true`；Workbench 在 SQLite 写事务之外捕获内容寻址补丁，再用 revision/attempt CAS 授权一次新 attempt。若 receipt 明确没有修改，只能以 `confirm_no_side_effects=true` 授权原路重试。合法 untracked 文件仍需显式 `preserve_untracked=true`，未知副作用或不确定捕获继续 fail closed。`instruction` 仍是模型指导而不是恢复理由；需要新指导时先 `steer`，再以更新后的 revision 执行 `resume`。

## 崩溃与重复请求

- assignment 前重启：recover_interrupted() 将 capture_pending 原子回滚到原失败 attempt 和 needs_fix，并在同一事务写入 orphan cleanup receipt。协调器在 SQLite 事务外把可能残留的 target 移入私有 recovery archive；归档失败或进程再次重启时，未 resolved 的 receipt 会继续重放。下一次合法 queue 再重建恢复绑定。
- assignment 后重启或执行器崩溃：target allocation 已是 attempt 的物理状态，节点进入 indeterminate。自动 retry 被明确拒绝，直到操作人完成显式恢复裁决；不会把同一 target 再派发给新 attempt。
- 重复 queue：旧 revision 失败，不会创建第二份恢复授权。
- 旧 attempt 晚到的 settlement：coordinator epoch、attempt 和 lease epoch fencing 拒绝写入。
- 暂停或取消与 settlement 竞态：节点的已租约结果可保存，但任务的 paused/cancelled 控制状态优先，不会被晚到结果推进到 verifying 或 accepted。

## SQLite 事务边界

Git repository identity、工作树检查、dependency-input 复原、patch 捕获、Artifact 读取与 SHA-256 校验均在 SQLite 写事务之外运行。写事务只重新核对持久字段、revision、attempt、coordinator/lease epoch、预验证签名和 allocation 状态，再提交短时状态变更。并发期间出现预取快照中没有的新仓库时，本轮 claim 放弃并由下一轮重新读取，不会在写锁内执行 Git。

这些保证由 fixture 仓库和临时数据库测试验证；它们不需要 Codex/Claude 登录、真实套餐调用或生产 SQLite 写入。
