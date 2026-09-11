# 受限验证执行

本页是操作者控制的确定性验证入口，不改变 Worker 的默认权限，不创建任务 attempt，也不把验证通过当作任务 accepted。Workbench 继续使用项目原有命令；仅当证据已定位为宿主沙箱拒绝 Unix IPC 或具体 Git 快照写入时，由拥有该任务授权的操作者执行。不得直接在被封存的恢复 source 上写入；选择已获准的验证工作树，并重新获取后续恢复需要的源码摘要。

## 执行器与权限

使用 Authority 实际固定的 Codex binary，先核对 `--version` 和 `sandbox --help`。受支持的调用是 `codex sandbox -P <profile> -c <complete-inline-profile> -C <worktree> --allow-unix-socket <private-temp> -- <exact-argv>`，不是 `codex exec`，不调用模型、登录或订阅。当前验证的 CLI 提供 `--allow-unix-socket`，只允许该路径下的 Unix bind/connect；旧 CLI 不支持时明确停止，不能退化为全网络或全权限执行。

为每次验证用 `mktemp -d /private/tmp/wb-verify.XXXXXX` 创建短、私有的临时目录。把该子进程的 `TMPDIR`、`TMP`、`TEMP` 指向它；DSH 的 IPC 测试还必须把 `DSH_HOME` 指向其子目录，避免落入真实用户的 `~/.dsh`。临时 SQLite、artifact 和 daemon socket 属于 fixture 数据，不是生产 Authority 的数据库。验证完成后只清理这个明确生成的临时目录。

传入完整 profile 内联表，所有路径使用规范化绝对路径；不要修改全局 Codex 配置，不要混用 `--sandbox` 和 named permission profile：

```text
permissions.wb-controlled-validation={filesystem={":root"="read",":tmpdir"="read",":slash_tmp"="read","<private-temp>"="write","<explicit-generated-output>"="write"},network={enabled=false}}
```

省略不需要的 `explicit-generated-output`；需要多个目录时逐个列出。Unix socket allowlist 只给私有临时目录，不给 `/tmp`、用户 home、生产 `.dsh` 或 Authority socket。测试仍执行原项目 argv，不关闭失败测试，也不使用 `danger-full-access`、全网络或 sandbox bypass。使用固定 Node/pnpm launcher；不能靠改全局 PATH/Node 来使测试偶然通过。若具体测试仍向其他目录写入，先确认该目录确实是该测试的产物，再补精确授权；错误本身不授权扩大范围。

## 精确翻译快照

DSH 的 pairing `--write` 会保存两侧 Markdown 的 Git blob、写 `refs/dsh/translation-pairing/snapshots/<blob>` 并更新对应 `.i18n.yaml`，不是只读检查。先读当前英文/中文精确字节，用 `git hash-object -- <file>` 只读求出对象 ID，并用 `git rev-parse --path-format=absolute --git-common-dir` 取得真实共享 Git 目录，不能把 linked worktree 的 `.git` 文件当目录。

每个已核对的 blob 只给这些写路径：

- `<git-common-dir>/objects/<blob前两位>`：Git 为内容寻址对象使用同目录临时文件，故必须授予这个 fanout 目录，而不是仅最终对象文件；这是目录级写权限，不声称能限制该目录内的所有字节。
- `<git-common-dir>/refs/dsh/translation-pairing/snapshots/<blob>` 和同名 `.lock`：精确 ref 与锁文件。
- 工作树中明确列出的 `.i18n.yaml` 文件。

不授予整个 `.git`、`refs/heads`、`refs/tags`、Git config、index 或其他 pairing 文件的写权限。若 snapshot 父目录不存在，需操作者显式准备该固定命名空间；不要把所有 refs 改为可写。应先审阅所执行的项目脚本；沙箱目录权限不是脚本语义校验或完整内容证明。

仅对需要更新的成对路径运行项目命令，随后使用同一组路径运行不带 `--write` 的 pairing gate。不得用 `--write --all` 扩大到整个文档库，不能把重记录当作翻译质量审查。记录命令、源版本、两侧 blob ID、退出码与 gate 输出；继续保留独立代码/文档审阅责任。

```sh
pnpm run verify-translation-pairing --write <README-1.md> <README-2.md>
pnpm run verify-translation-pairing <README-1.md> <README-2.md>
```

上述 argv 在选定的受限 sandbox 内执行。外层有自己的沙箱时，只能使用平台提供的、对该条受限命令的审批升级；不能尝试逃离外层沙箱。

## 可执行证据

`tests/test_controlled_validation_sandbox.py` 在一次性 macOS fixture 中执行真实 Codex sandbox：证明允许目录内的 Unix socket 往返通信，同时拒绝其他 Unix bind/connect、TCP bind、无关源码和 `.codex` 写入；证明指定 Git blob/ref 可写，同时拒绝其他 snapshot ref、分支 ref 与 Git config。它不调用真实模型，不连接或改写生产任务。

```sh
WB_SANDBOX_CODEX=<authority-pinned-codex> scripts/python-runtime \
  -m unittest tests.test_controlled_validation_sandbox
```

Linux CI 没有 macOS Seatbelt 时明确 skip；macOS 发布证据须使用固定 CLI 实际执行，不能用 skip 代替通过。该隔离回归证明权限行为，不证明 DSH B 的具体测试或 pairing 已通过；后者必须在授权目标与实际源码上另行记录。

权限配置参考 [OpenAI Docs: Permissions](https://learn.chatgpt.com/docs/permissions)。本地 CLI 的实际参数与受限 fixture 是版本能力的直接证据，不能从最新文档推断旧 binary 一定支持。
