# Codex Workbench：交给 AI 执行的安装与配置指南

本指南面向一名有终端权限、但**不会猜测环境或越权登录**的 AI 操作员。它把安装分成两个明确角色：Mac mini 是唯一的 **Authority**（持有 SQLite 任务账本、执行器与验收 Evidence），MacBook 是 **Cockpit**（Codex 入口、MCP 客户端和本地回退点）。二者都必须运行 macOS。

```text
MacBook Codex + WB plugin
        │  stdio MCP over SSH / Tailscale
        ▼
Mac mini Authority ── SQLite / worktrees / workers / verifier
        │
        └── 只监听本机回环地址；不复制账本到 MacBook
```

安装目标是让 Codex 能通过 MCP 使用 Authority，并让 `wb` 将一个新会话或已有会话绑定到该 Authority。安装、Health 检查和会话绑定本身不应启动模型回合、触发 Claude 登录或消耗 Claude 配额。

## 0. AI 的执行边界

在开始前，AI 必须遵守以下规则：

- 仅在 macOS 上执行；不要尝试在其他平台安装。
- 先执行所有只读预检；任何预检失败都停止，不以删除、覆盖、重置或创建替代配置的方式绕过。
- 不读取、打印、复制、提交或传输 `~/.codex/auth.json`、SSH 私钥、环境凭据或其他机密。检查文件是否存在即可。
- 不执行 `codex login`、Claude 登录、模型提示、付费 API 调用，或任何绕过 Hook 信任的命令。
- 不覆盖已有 `~/.ssh/config`、`~/.codex`、`~/.claude` 内容。安装器遇到非 Workbench 管理的同名 Skill 或策略块时会拒绝写入；这是正确的停止条件。
- 只有在用户确认 Hook 内容后，才可在 Codex 中信任 Hook；不得使用绕过信任的启动参数。
- `WB_REF` 应是已审阅的发布 tag 或完整提交，而不是浮动分支；若只能使用分支，AI 必须把实际提交写入安装记录。

下面的尖括号值都是占位符。AI 必须先替换，不能把原样占位符拿去执行。

## 1. 两台机器通用：准备来源与变量

在 **Mac mini** 和 **MacBook** 各自的终端中设置变量。值不写入仓库，也不提交到 Git。

```bash
set -euo pipefail

export WB_REPOSITORY_URL="https://github.com/<GITHUB_OWNER>/codex-workbench.git"
export WB_MARKETPLACE_SOURCE="<GITHUB_OWNER>/codex-workbench"
export WB_REF="<RELEASE_TAG_OR_FULL_COMMIT>"
export WB_ROOT="$HOME/Projects/codex-workbench"
export WB_STATE_ROOT="$HOME/Library/Application Support/Codex Workbench"
export WB_MARKETPLACE="codex-workbench"
export WB_AUTHORITY_ALIAS="workbench-authority"
export WB_AUTHORITY_HOST="<TAILSCALE_DNS_NAME>"
export WB_AUTHORITY_LAN_HOST="<HOME_LAN_HOST_OR_IP>"
export WB_AUTHORITY_LAN_PORT="22"
export WB_AUTHORITY_TAILNET_HOST="<TAILNET_DNS_NAME>"
export WB_CLIENT_TAILSCALE_SOCKET="<OPTIONAL_MACBOOK_USERSPACE_TAILSCALED_SOCKET>"
export WB_AUTHORITY_USER="<MACOS_ACCOUNT_NAME>"
export WB_NAS_ARCHIVE_ROOT="<MOUNTED_NAS_DIRECTORY_FOR_WORKTREE_ARCHIVES>"
```

先确认平台与基础工具。以下仅检查，不会修改系统。

```bash
test "$(uname -s)" = "Darwin"
command -v git
command -v ssh
command -v codex
codex --version
codex plugin --help
codex mcp --help
codex app-server daemon --help
codex remote-control --help
```

如果 `codex plugin marketplace add`、`codex plugin add` 或 `codex mcp` 不在本机 CLI 帮助中，停止并升级或更换到兼容的 Codex CLI；不要手工编辑 Codex 配置来模拟这些能力。

在一个尚不存在的受控目录克隆并固定来源。若 `$WB_ROOT` 已存在，停止并先由操作者确认它是否为干净的 Workbench checkout；不要覆盖它。

```bash
test ! -e "$WB_ROOT"
git clone "$WB_REPOSITORY_URL" "$WB_ROOT"
git -C "$WB_ROOT" fetch --tags --prune origin
git -C "$WB_ROOT" checkout --detach "$WB_REF"
git -C "$WB_ROOT" rev-parse HEAD
test -z "$(git -C "$WB_ROOT" status --porcelain)"
test -x "$WB_ROOT/scripts/python-runtime"
"$WB_ROOT/scripts/python-runtime" --version
```

记录 `git rev-parse HEAD` 的输出到安装工单或本地变更记录；不要把它替换成“最新”。

### 1.1 两台机器的 Codex 500K 长上下文

MacBook 与 Mac mini 的 Codex App 和用户 CLI 读取各自用户的 `~/.codex/config.toml`。保留
文件中所有现有配置，只确保以下两个键位于 TOML 顶层：

```toml
model_context_window = 500000
model_auto_compact_token_limit = 450000
```

不要为此删除其他模型、MCP、sandbox、plugin 或 profile 配置。修改后先运行当前 CLI 的
只读解析命令（例如 `codex features list`）；若桌面 App 内含独立 Codex binary，也用该
binary 运行同一解析检查。配置只在新进程/新任务建立上下文时生效，不能声称正在运行的
旧任务被追溯扩容；保存工作后重启 App 并创建新任务。

Workbench 的 planner/worker 使用 `--ignore-user-config`，所以不能依赖上述用户配置。
1.12.0 会对受管 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna` 与 `gpt-6-astra` 进程额外显式传入同一
`500000/450000`；Spark 保持其自身模型合同。

## 2. Mac mini Authority

### 2.1 Authority 先决条件与只读预检

Authority 安装器需要：可执行的 Codex CLI 及其同目录的 `codex-code-mode-host`、可读的本机 Codex 订阅认证文件、Python 3.11+、完整的 Research Skill 来源，以及已就绪的 Tailscale 本地 API socket。它不会把用户的全局 Codex 配置、会话或模型缓存复制到 Authority 运行时。

在 Mac mini 上补充以下变量。`WB_TAILSCALE_SOCKET` 必须是当前 Tailscale 进程实际使用的 Unix socket；不要猜测路径。

```bash
export WB_CODEX_BINARY="$HOME/.codex/packages/standalone/current/codex"
export WB_RESEARCH_SKILL_SOURCE="<ABSOLUTE_PATH_TO_COMPLETE_RESEARCH_SKILL>"
export WB_TAILSCALE_SOCKET="<ABSOLUTE_PATH_TO_ACTIVE_TAILSCALED_LOCAL_API_SOCKET>"
```

运行以下只读检查。Research Skill 必须至少包含安装器要求的四个文件；缺任一文件即停止。

```bash
test -x "$WB_CODEX_BINARY"
test -x "$(dirname "$WB_CODEX_BINARY")/codex-code-mode-host"
test -r "$HOME/.codex/auth.json"

test -f "$WB_RESEARCH_SKILL_SOURCE/SKILL.md"
test -f "$WB_RESEARCH_SKILL_SOURCE/UrlVerificationProtocol.md"
test -f "$WB_RESEARCH_SKILL_SOURCE/Workflows/StandardResearch.md"
test -f "$WB_RESEARCH_SKILL_SOURCE/Workflows/DeepInvestigation.md"

command -v tailscale
command -v zstd
test -S "$WB_TAILSCALE_SOCKET"
tailscale --socket="$WB_TAILSCALE_SOCKET" status --json >/dev/null
test -d "$WB_NAS_ARCHIVE_ROOT"
test -w "$WB_NAS_ARCHIVE_ROOT"
smbutil statshares -m "$WB_NAS_ARCHIVE_ROOT"

"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-code-as-harness.py" \
  --source "$WB_ROOT" --check --adopt-compatible

"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-archify.py" \
  --source "$WB_ROOT" --dry-run

"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-macos.py" \
  --source "$WB_ROOT" \
  --state-root "$WB_STATE_ROOT" \
  --codex-binary "$WB_CODEX_BINARY" \
  --research-skill-source "$WB_RESEARCH_SKILL_SOURCE" \
  --nas-archive-root "$WB_NAS_ARCHIVE_ROOT" \
  --tailscale-socket "$WB_TAILSCALE_SOCKET" \
  --dry-run
```

`--dry-run` 不写文件、不启动 LaunchAgent、不启用 Tailscale Serve、不发起 SSH/MCP 连接。它通过只证明当前安装器能接受本机的来源和目标，不证明模型登录、Claude 配额或远端客户端已经可用。

### 2.2 可选：接入 Claude Code，但保留配额

Claude Code 完全可选。没有它时，Workbench 保持可安装并将 Claude 工作路由为 Codex；不要因缺少 Claude 而阻塞基础部署。

如要接入，AI 只能检查 CLI 是否存在及版本，不能发起登录或模型回合：

```bash
command -v claude
claude --version
export WB_CLAUDE_BINARY="$(command -v claude)"
```

仅当上面的命令都成功，才在下一步的 Authority 安装命令中附加：

```text
--claude-binary "$WB_CLAUDE_BINARY"
```

Authority 的被动配额采集器只接受兼容的本地 Claude CLI 使用量显示；认证未知、来源不兼容、快照过期或任一受保护池不高于 25% 时，系统不得启动新的 Claude 节点，而是回落到 Codex。20% 是保留目标，30% 是 admission guard，25% 是硬停线。安装成功不等于 Claude 已登录，也不等于已验证任何真实余额或单回合绝不跨线。

### 2.3 安装 Authority

选择**一个**命令：不接入 Claude 时执行第一个；已通过可选预检时执行第二个。安装器会以事务方式投影 Workbench 管理的 Code-as-Harness 与 Archify 内容，建立独立运行时、服务和本地回环监听，并为 Tailscale 配置 HTTPS 与原生 SSH TCP Serve。

```bash
"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-macos.py" \
  --source "$WB_ROOT" \
  --state-root "$WB_STATE_ROOT" \
  --codex-binary "$WB_CODEX_BINARY" \
  --research-skill-source "$WB_RESEARCH_SKILL_SOURCE" \
  --nas-archive-root "$WB_NAS_ARCHIVE_ROOT" \
  --tailscale-socket "$WB_TAILSCALE_SOCKET"
```

```bash
"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-macos.py" \
  --source "$WB_ROOT" \
  --state-root "$WB_STATE_ROOT" \
  --codex-binary "$WB_CODEX_BINARY" \
  --research-skill-source "$WB_RESEARCH_SKILL_SOURCE" \
  --nas-archive-root "$WB_NAS_ARCHIVE_ROOT" \
  --tailscale-socket "$WB_TAILSCALE_SOCKET" \
  --claude-binary "$WB_CLAUDE_BINARY"
```

Authority 安装后进行无模型验证：

```bash
export WB_AUTHORITY_BIN="$WB_STATE_ROOT/app/bin/codex-workbench"
test -x "$WB_AUTHORITY_BIN"
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" doctor
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" harness health
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" capabilities status
curl --ipv4 --fail --silent --show-error http://localhost:8766/health
```

Authority 安装器会在主服务启动前，以已安装的 Codex runtime 生成一份 bundled capability catalog；只有存在明确的 Sol planner/verifier 和至少一个 Codex Worker 时才激活。随后 `com.lisihao.codex-workbench-capabilities` LaunchAgent 默认每 6 小时执行一次 live metadata refresh。刷新只调用 CLI 版本、帮助与模型目录命令，不登录、不发模型提示；未变化结果不会制造新 generation，失败会继续使用上一份完整目录。

Authority 还会安装 `com.lisihao.codex-workbench-radar`，默认每 86400 秒执行一次
`codex-workbench ... radar refresh`。首次使用个人自用模式时，在 Authority 执行
`radar consent-personal-use`；它只写入不含秘密的本地 receipt，不联网，也不表示获得站方
许可。个人 receipt 必须同时包含 `consented`、`local_operator_consent`、`public-json` 和
`accepted_at`。上游 `current.json` 仍声明完整 API/衍生集成需要站方授权；本地个人使用不能
写成 `authorized`。通过 consent 后才采集固定公共 JSON endpoints；断网继续从
`<WB_STATE_ROOT>/radar.sqlite3` 读取 last-known-good，7–31 天降权，31 天后回落随包 baseline。
旧 JSON 投影会自动迁入 SQLite；JSON 仅为兼容投影。

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" radar consent-personal-use
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" radar status
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" radar show
```

provider 插件 0.2.0 也提供等价的本地命令：

```bash
codex-radar-provider --state-root "$WB_STATE_ROOT/radar" consent --personal-use
```

`radar status` 和 `codex-radar-provider status` 会报告 SQLite backend、schema、path 与
`radar_snapshots`、`radar_raw_payloads`、`radar_models`、`radar_insights`、`radar_active`
的 row counts。Provider SQLite 与 Workbench task SQLite 是两个独立账本。

Authority 还会安装 `com.lisihao.codex-workbench-ai-frontier`，默认每 259200 秒（72 小时）
运行一次，Provider 自身再强制 24 小时最短间隔。安装不会创建 consent receipt，也不会联网；
因此首次 `status` 显示 `disabled_by_policy` 是正确状态。只有操作者决定采用个人自用模式后，
才依次写入本地 receipt、执行一次首采并检查性能快照：

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" ai-frontier status
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" ai-frontier consent-personal-use
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" ai-frontier refresh
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" ai-frontier show
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" performance status
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" performance identities
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" performance list --format json
```

该 receipt 固定包含 `not_official_authorization=true` 和 Martian Terms URL；它只记录本地操作者
选择，不代表 Martian 官方授权或条款豁免。刷新只读取 reliability、cost 两个聚合 JSON，及
最多八个同时出现在 capability catalog 与 AI Frontier leaderboard 的精确模型分类数据；
不抓 HTML、examples、frontier/oracle、Cookie 或凭据，也不调用 Codex/Claude 模型。SQLite
位于 `$WB_STATE_ROOT/ai-frontier/ai-frontier.sqlite3`；失败保留 LKG，7--31 天降权，31 天后
退出新任务先验。详细合同见 [AI Frontier 集成](ai-frontier-integration.md)。

如果接入了 Claude，也只能查看被动快照；`unknown`、`unavailable` 或未登录都是正确的 fail-closed 结果，而不是让 AI 重试登录的理由。

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" quota show
```

## 3. MacBook Cockpit

### 3.1 配置 SSH 与 Tailscale

在 MacBook 上，先确保 Tailscale 已由操作者完成设备登录并能看到 Authority。不要修改全局代理规则。为 Authority 添加一个**新的、经人工审阅的** SSH stanza，保留既有 `~/.ssh/config` 内容。下面是需要替换占位符的配置形状，不是可以直接原样执行的 shell 命令：

```sshconfig
Host workbench-authority
  HostName <TAILSCALE_DNS_NAME>
  User <MACOS_ACCOUNT_NAME>
  IdentityFile ~/.ssh/<PRIVATE_KEY_FILE>
  IdentitiesOnly yes
```

`Host` 必须等于 `$WB_AUTHORITY_ALIAS`。写入后，仅检查解析结果：

```bash
ssh -G "$WB_AUTHORITY_ALIAS" | grep -E '^(hostname|user|identityfile|proxycommand) '
tailscale status --json >/dev/null
```

不要把密钥内容粘贴进本指南、终端日志或 Git。实际远端连通性由下一节的非 dry-run 安装器预检验证；它会使用 `BatchMode=yes`，不会互动索要密码。

该传输策略按显式网络 CIDR 判断，而不是按国家/地区、Wi‑Fi 名称或 IP 段的语义猜测。它不改任何系统路由和 Tailscale 身份策略；也不会自动触发 Claude/Codex 登录。

### 3.2 Cockpit 只读预检

MacBook 也需要同一已固定的 Workbench checkout。完成第 1 节的 clone 后运行：

```bash
command -v codex
test -f "$WB_ROOT/.agents/plugins/marketplace.json"
test -f "$WB_ROOT/plugins/codex-workbench/.codex-plugin/plugin.json"
test -f "$WB_ROOT/plugins/codex-radar-provider/.codex-plugin/plugin.json"

"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-code-as-harness.py" \
  --source "$WB_ROOT" --check --adopt-compatible

"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-archify.py" \
  --source "$WB_ROOT" --dry-run

"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-macbook-client.py" \
  --source "$WB_ROOT" \
  --authority-state-root "$WB_STATE_ROOT" \
  --authority-ssh-alias "$WB_AUTHORITY_ALIAS" \
  --authority-lan-host "$WB_AUTHORITY_LAN_HOST" \
  --authority-lan-port "$WB_AUTHORITY_LAN_PORT" \
  --authority-tailnet-host "$WB_AUTHORITY_TAILNET_HOST" \
  --tailscale-socket "$WB_CLIENT_TAILSCALE_SOCKET" \
  --home-network "<HOME_CIDR_1>" \
  --home-network "<HOME_CIDR_2>" \
  --ssh-transport location-aware \
  --dry-run
```

缺少 marketplace manifest 或插件 manifest 时停止。这说明所选发布版本不含完整的 WB 插件发行物，不能用手工复制本地文件替代。

### 3.3 安装 Cockpit 与 MCP

以下命令会先通过 SSH 测试 Authority 的 MCP 可执行文件；成功后才注册 `codex-workbench` stdio MCP，并安装自动恢复的 Cockpit tunnel 与 heartbeat LaunchAgent。它不复制 Authority 的 SQLite。

```bash
"$WB_ROOT/scripts/python-runtime" \
  "$WB_ROOT/scripts/install-macbook-client.py" \
  --source "$WB_ROOT" \
  --authority-state-root "$WB_STATE_ROOT" \
  --authority-ssh-alias "$WB_AUTHORITY_ALIAS" \
  --authority-lan-host "$WB_AUTHORITY_LAN_HOST" \
  --authority-lan-port "$WB_AUTHORITY_LAN_PORT" \
  --authority-tailnet-host "$WB_AUTHORITY_TAILNET_HOST" \
  --tailscale-socket "$WB_CLIENT_TAILSCALE_SOCKET" \
  --home-network "<HOME_CIDR_1>" \
  --home-network "<HOME_CIDR_2>" \
  --ssh-transport location-aware

export WB_CLIENT_ROOT="$HOME/Library/Application Support/Codex Workbench Client"
"$WB_CLIENT_ROOT/bin/workbench-location-proxy" \
  --config "$WB_CLIENT_ROOT/transport.json" \
  --select

codex mcp get codex-workbench
curl --ipv4 --fail --silent --show-error http://localhost:18766/health
```

`location-aware` 会按以下规则按次决策：

1. 每次连接先检查 MacBook 当前非回环网络接口地址是否匹配任意 `--home-network` CIDR。
2. 在家网段且 LAN TCP 可达时，走 `--authority-lan-host:--authority-lan-port`，不使用 Tailscale。
3. 其余场景走 `--authority-tailnet-host` 的 Tailscale 原生 SSH TCP Serve（不改变认证模型，不使用 userspace SSH）。
4. 两条链路都失败时，返回 `degraded` receipt 并进入 outbox，不会自动改写系统网络或绕过登录。

heartbeat 使用同一选路器把 `route`、`reason` 与新鲜观察时间送到 Authority。只有 `home_network_lan_probe_ok` 会产生最多十分钟的家庭 LAN 租约；Tailscale 可达、手机在线或普通私网地址都不能产生该租约。

`--ssh-transport auto` 在同时提供 `--authority-lan-host`、`--authority-tailnet-host` 与至少一条 `--home-network` 时，等效于 `location-aware`。未配置完整参数时请明确保留 `location-aware`，避免默认回退被误解为“纯 auto”。

`--home-network` 不能使用 Tailscale 的 `100.64.0.0/10`；否则 Tailscale 接口会让 MacBook 在任何地点都被误判为“在家”。应填写家中路由器实际分配给 Wi-Fi 或有线网络的 CIDR。

如果 MacBook 使用 userspace tailscaled，设置 `--tailscale-socket` 为现有 socket 路径；选路器会把它作为单个 `--socket=...` 参数传给 `tailscale nc`。使用系统默认 tailscaled 时省略该参数和对应环境变量。

若连接失败，停止并修复 Tailscale、SSH key、别名或 Authority 健康；不要把 `tailscale-userspace` 当成绕过认证的后备方案。

### 3.4 安装个人 Codex 插件并信任 Hook

插件通过公开仓库的 marketplace 发行。marketplace 名为 `codex-workbench`，包含主
`codex-workbench` 插件、通用 `codex-radar-provider` 0.2.0 和 `ai-frontier-provider` 0.1.0；
两个 Provider 插件只提供 Skill/消费契约，不会在 MacBook 启动第二个采集 writer。

```bash
codex plugin marketplace add "$WB_MARKETPLACE_SOURCE" --ref "$WB_REF" --json
codex plugin marketplace list --json

codex plugin list --marketplace "$WB_MARKETPLACE" --available --json
codex plugin add "codex-workbench@$WB_MARKETPLACE" --json
codex plugin add "codex-radar-provider@$WB_MARKETPLACE" --json
codex plugin add "ai-frontier-provider@$WB_MARKETPLACE" --json
codex plugin list --json
```

接着在 Codex 的交互式界面中完成一次人工信任：

1. 打开 `/hooks`。
2. 找到 `codex-workbench` 的 `UserPromptSubmit` Hook，核对它来自刚安装的插件版本，且命令仅指向插件内的 `scripts/wb_hook.py`。
3. 由操作者显式信任该 Hook；Hook 内容或版本变化后应重新审核。
4. 不得用任何绕过 Hook 信任的命令启动 Codex。

完成信任后，在任意位于 Git 仓库中的新会话或已有会话输入小写 `wb`。只有看到 `WB_SYNC_RECEIPT` 且其状态为 `active`，才可声称该会话已由 Mac mini Workbench 接管。`$WB` 和从 `/skills` 选择 `WB` 是备选入口；不要依赖自定义顶层 slash command。

### 3.5 可选：让手机 Codex App 连接 Authority

手机接入使用 Codex 自带的 Remote，不建立第二套 Workbench 总账。Workbench 只配置 plugin/MCP；原生 Remote host 和二维码由 Mac mini 的 ChatGPT/Codex 桌面 App 独占管理。先执行只读状态与 dry-run：

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile status
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile enable --dry-run
```

确认计划只包含当前 Workbench marketplace、插件、MCP 与 Codex app-server 后，再启用：

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile enable
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" mobile pair
```

`mobile enable` 不会启动 `codex app-server daemon`，避免与桌面 App 的单实例 Remote host 冲突。`mobile pair` 返回桌面设置路径：在 Mac mini 打开 `Settings > Connections > Control this Mac or PC > Set up or Add`，显示二维码后用手机扫描并批准。手机配对后新建/打开远程 Codex 会话并输入 `wb`；只有远端会话收到 `WB_SYNC_RECEIPT=active`，且手机真实完成一次查看和发送，才可把手机旅程标为通过。

## 4. 验证矩阵

按下表逐项保留输出。任何 `error` 都应停止在对应层，不应通过启动模型、修改认证或重复安装来“试试”。

| 层 | 命令或观察 | 可确认的事实 | 不可据此声称的事实 |
| --- | --- | --- | --- |
| 固定来源 | `git -C "$WB_ROOT" rev-parse HEAD`、`git status --porcelain` | 实际来源提交及工作树状态 | GitHub 权限、发布质量 |
| Authority 预检 | `install-macos.py ... --dry-run` | 目标、Research Skill、Codex runtime、Tailscale socket 可被安装器接受 | 模型登录、Claude 余额、客户端连通性 |
| Authority 健康 | `doctor`、`harness health`、`curl ...:8766/health` | 本地服务、治理投影、回环健康端点 | 任一真实模型回合或任务已验收 |
| 能力目录 | `capabilities status/show/diff` | 当前激活 generation、模型/Agent 版本与静态策略 | 某模型在真实任务上的质量优于另一模型 |
| Radar Provider | `radar consent-personal-use`、`radar status/show`、`/api/radar` | 本地 consent 字段、SQLite backend/schema/path/row counts、缓存、快照 ID/digest、fresh/stale/expired 与归因；查看不联网 | personal-use consent 不是站方授权；未完成真实采集时不能声称已有生产 Radar 数据；Radar 不是配额或本机成功率 |
| AI Frontier Provider | `ai-frontier consent-personal-use/refresh/status/show`、`/api/ai-frontier` | 72 小时节流、SQLite LKG、模型/分类/原始 payload 行数、精确模型 selected/skipped、快照 ID/digest 与零模型调用 | personal-use consent 不是 Martian 授权；Quality/Consistency/Real Cost 不是本机成功率、订阅 quota 或已证明的美元成本 |
| 500K 上下文 | 两机用户配置解析、App 内嵌 CLI 解析、Workbench argv 测试 | 新 App/CLI/Workbench 任务会请求 500K/450K | 不能证明旧任务已追溯扩容，也不代表每回合使用满 500K |
| Cockpit | `install-macbook-client.py`、`codex mcp get`、`curl ...:18766/health` | SSH/MCP 配置与本地隧道可用 | 插件 Hook 已获信任 |
| 插件 | `codex plugin list --json`，随后人工 `/hooks` 审核 | 插件被安装且 Hook 被人工审阅 | 会话已同步或远端任务正在执行 |
| 会话接管 | `WB_SYNC_RECEIPT` 的 `active` 状态 | Context Bundle 已被 Authority 持久绑定 | 任务已 accepted 或模型已调用 |
| 手机 Remote | `mobile status`、桌面 App 生成二维码、手机真机查看/发送 | 原生 Remote 配置与真实手机旅程 | 仅凭 plugin/MCP 就绪不能声称已配对 |
| Worktree 恢复 | `worktree status`，以及一次受控归档/恢复 | 隔离、NAS verified receipt、恢复路径与清理状态 | 没有真实 NAS/SMB 与 Tailscale 旅程时，单元测试不能替代生产证明 |
| Claude 可选项 | `quota show` | 当前被动快照是否被识别，或是否 fail-closed | 真实剩余百分比、一次回合的最终消耗 |

## 5. 日常使用与离线回退

连接正常时，Codex 是唯一用户入口：在已有会话输入 `wb`，随后用 MCP 工具提交、查看或追加任务。Authority 持有唯一账本；MacBook 只是 cockpit，关闭或离线不会停止已在 Mac mini 运行的任务。

MCP、Hook、tunnel 与 Git 同步共享同一传输 profile。Git 的 `sync` 与 `tailscale bundle` 也会复用安装时写入的 `workbench-location-proxy` 路径与 `ProxyCommand` 规则，故 transport 语义对同一设备一致。

终态 worktree 先进入 `$WB_STATE_ROOT/recycle/worktrees`。后台恢复线程每次只处理有持久 allocation 的 Workbench worktree，并遵守以下门禁：

1. 没有新鲜家庭 LAN 租约时，Authority 只执行 `git worktree move`，不删除分支或目录。
2. 有租约且 NAS 的 SMB 挂载可写时，先写 `.partial`，再做压缩流检查、完整安全解包、文件清单对账、supporting Evidence 哈希核验和 Git bundle 克隆恢复。
3. 只有归档原子改名、SHA-256 sidecar 与 SQLite `verified` receipt 都完成后，才清理源 worktree 和 Workbench 专属分支。
4. 如果归档源在远程 MacBook，可显式执行 `worktree send`；该动作强制 location proxy 使用 Tailscale，不会因为 MacBook 恰好在家就改走 LAN。Mac mini 必须先完成同样的 NAS 恢复校验并返回匹配 receipt，源端才清理。

归档或传输失败会进入 `archive_failed` 并保留源目录；后台默认至少等待 15 分钟才重试，避免每分钟重复压缩和重复占用 I/O。

可在当前 Codex 会话调用 MCP 工具 `workbench_worktree_status`、`workbench_reclaim_worktrees` 和 `workbench_restore_worktree`，也可以在 Authority 上直接使用：

```bash
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" worktree status
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" worktree sweep --max-items 10
"$WB_AUTHORITY_BIN" --home "$WB_STATE_ROOT" worktree restore <archive-id> --destination <new-empty-path>
```

远程源端已经安装完整 Workbench 和 location-aware client profile 时，可用：

```bash
codex-workbench worktree send <allocation-id> --host "$WB_AUTHORITY_ALIAS"
```

`send` 没有 direct-SSH fallback：location-aware Tailscale profile 缺失、远端校验失败或 receipt 不匹配时，压缩临时文件会清理，但隔离 worktree 保留在回收目录。

若 Hook 回执为 `degraded`，AI 必须明确报告 Mac mini 未接管，并在当前 MacBook checkout 中继续本地工作。不得伪称任务已派到 Authority，也不得复制、编辑或合并 Authority SQLite。网络恢复后，在同一会话再次输入 `wb`；Hook 会重新尝试同步当前上下文与允许的 Git 增量。

## 6. 升级与兼容性

升级前应先确保没有需要人工决定的活跃任务，并在两台机器使用同一个候选 ref。不要在运行中的交互式 Codex 回合中升级。

1. Authority 上先执行 `doctor`、`harness health`、`capabilities status` 和 `git status --porcelain`；若工作树不干净，停止。
2. 将 `$WB_STATE_ROOT` 复制到一个操作者指定、受访问控制的本地备份目录。备份仅用于恢复，不得上传或提交：

```bash
export WB_BACKUP_ROOT="<ABSOLUTE_EMPTY_LOCAL_BACKUP_DIRECTORY>"
test ! -e "$WB_BACKUP_ROOT"
mkdir -p "$WB_BACKUP_ROOT"
ditto "$WB_STATE_ROOT" "$WB_BACKUP_ROOT/state-root"
```

3. 在 Authority 与 MacBook 各自执行 `git fetch --tags --prune origin`，核对候选 ref，再用 `git checkout --detach "$WB_NEXT_REF"` 切换。
4. 先重跑相应 `--dry-run`，再重跑 Authority 或 Cockpit 安装器。
5. 在 MacBook 上刷新 marketplace：

```bash
export WB_NEXT_REF="<NEXT_RELEASE_TAG_OR_FULL_COMMIT>"
git -C "$WB_ROOT" fetch --tags --prune origin
git -C "$WB_ROOT" show --quiet --format='%H %D' "$WB_NEXT_REF"

codex plugin marketplace upgrade "$WB_MARKETPLACE"
codex plugin list --marketplace "$WB_MARKETPLACE" --available --json
```

当前 Codex CLI 没有单独的 `plugin upgrade` 子命令。若 marketplace 刷新后显示插件版本仍旧，先停止新任务、由操作者确认，再执行一次 `codex plugin remove` 后重新 `codex plugin add`，并重新审核 `/hooks`。不要在 Hook 未获重新信任时运行 `wb`。

Authority 安装器会先建立并安全激活与新 runtime 配套的能力目录，再启动主服务和刷新 sidecar。旧任务仍使用 TaskContract 固定的旧 `catalog_id`；新目录不安全时安装会回滚。安装后应比较 `capabilities status` 与 `capabilities diff`，未知/弃用模型保持 observed-only 是正确结果，不应手工把它们改成 routable。

升级失败时，两个安装器会对本次触及的文件执行事务回滚。已完成升级的应用回退应通过一个已知兼容的旧 ref 重跑相同安装器完成；如果 release notes 没有声明账本 schema 可逆或兼容，停止并从升级前的状态备份恢复，而不是只替换应用目录。

在已确认 schema 兼容且操作者明确要求应用回退时，可按下面方式回到已知 ref，再依次重跑本指南第 2.3 节和第 3.3 节的安装命令：

```bash
export WB_PREVIOUS_REF="<KNOWN_COMPATIBLE_RELEASE_TAG_OR_FULL_COMMIT>"
test -z "$(git -C "$WB_ROOT" status --porcelain)"
git -C "$WB_ROOT" checkout --detach "$WB_PREVIOUS_REF"
git -C "$WB_ROOT" rev-parse HEAD
```

## 7. 卸载与回退

当前安装器负责失败时的事务回滚，但不提供“删除所有内容”的一键卸载器。AI 必须采用可恢复的顺序：

1. 确认没有活跃任务需要保留，并由操作者确认要停止 Cockpit/Authority 服务。
2. 仅移除 Workbench MCP 与插件：

```bash
codex plugin remove codex-workbench --marketplace "$WB_MARKETPLACE"
codex mcp remove codex-workbench
```

3. 只有确认该 marketplace 没有其他正在使用的插件后，才移除 marketplace：

```bash
codex plugin list --marketplace "$WB_MARKETPLACE"
codex plugin marketplace remove "$WB_MARKETPLACE"
```

4. 枚举、展示并由操作者确认所有名称包含 `codex-workbench` 的 LaunchAgent plist；不要依据模糊名称、通配符删除或停止服务：

```bash
find "$HOME/Library/LaunchAgents" -maxdepth 1 -type f -name '*codex-workbench*.plist' -print
```

5. 对**每一个**已由操作者确认的单一 plist，设置精确路径后再停止。不要把通配符或目录赋给变量：

```bash
export WB_APPROVED_PLIST="<ONE_EXACT_APPROVED_PLIST_PATH>"
test -f "$WB_APPROVED_PLIST"
launchctl bootout "gui/$(id -u)" "$WB_APPROVED_PLIST"
```

6. 将 `$WB_STATE_ROOT` 移至操作者选定的本地备份位置，保留至确认不再需要任务账本、Evidence 或回退。不要自动删除它。
7. 默认不要删除用户级 Code-as-Harness、Archify 或其他 `~/.codex` / `~/.claude` 内容；这些路径可能被其他已安装工具使用，必须先单独确认所有权。

## 8. 必须停止并请求人工决定的情况

- 不在 macOS、Codex CLI 不包含 marketplace/MCP 命令，或仓库 ref 无法固定。
- 目标版本缺少 `.agents/plugins/marketplace.json` 或 `plugins/codex-workbench/.codex-plugin/plugin.json`。
- Codex runtime、其 companion host、Research Skill、Tailscale socket、SSH key 或 Authority MCP 预检任一失败。
- 桌面 App 不提供 `Connections > Control this Mac or PC` 时，不启动 CLI app-server、开放端口或自制中继冒充原生 Remote；先核对桌面/手机版本、同一账号与 workspace，以及管理员是否允许 Remote。
- 安装器报告已有非 Workbench 管理的同名 Skill、策略块、文件或 symlink。
- Hook 未经人工信任、Hook 内容与预期插件不一致，或 `wb` 没有产生 `active` 回执。
- Claude CLI 缺失、认证/配额未知、版本或使用量显示不兼容。此时可以继续 Codex-only Workbench，但不得强行启用 Claude。
- 需要从已完成升级回退，却没有已知兼容的旧版本或升级前状态备份。

这些停止条件不是失败的替代品：它们防止 AI 把不确定性误报为已安装、已同步、已登录或已验收。
