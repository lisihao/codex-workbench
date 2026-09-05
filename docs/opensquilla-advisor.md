# OpenSquilla 本地 advisor 安装

此安装器只安装 Workbench 可调用的 OpenSquilla V4 Phase 3 本地需求分层 advisor。它是兼容子集：不会安装或启动完整 OpenSquilla gateway，也不会启用 self-learning trainer。

advisor 输出的是 `c0`–`c3` 需求分层，不是任务成功率、模型成功率、概率，也不会选择 provider 或发起模型调用。

## 前提

- 已存在的 Codex Workbench 状态目录（`--home`）。
- 一个本地 OpenSquilla Git checkout，其 `HEAD` 必须是 `94ac35eb99a564e15fa651abf8300c89f21efa0f`。
- 实际 V4 Phase 3 18 文件 bundle；安装器会将它的 `artifact_manifest.json` 与该 source pin 的 manifest 按字节比较，并在复制到最终目录后逐项校验大小和 SHA-256。
- 包含 `requirements-native-pinned.txt` 的本地 wheelhouse。该固定 requirements 文件含 `numpy`、`lightgbm`、`joblib`、`scikit-learn`、`onnxruntime`、`tokenizers`、`structlog`、`PyYAML` 及其所需的本地传递依赖。
- 可执行的 Python 3.12 或更高版本。

安装不会联网：source 用本地 `git clone --no-hardlinks` 克隆，pip 使用 `--no-index --find-links <wheelhouse> -r <wheelhouse>/requirements-native-pinned.txt`。

## 先做无写入检查

从 Codex Workbench 仓库根目录运行：

```sh
/opt/homebrew/bin/python3.12 scripts/install-squilla-advisor.py \
  --home "$HOME/Library/Application Support/Codex Workbench" \
  --source-root /private/tmp/opensquilla-research.4Om6Cs/source \
  --bundle-dir /private/tmp/opensquilla-native-Dmyrla/bundle \
  --wheelhouse /private/tmp/opensquilla-native-Dmyrla/wheelhouse \
  --python /opt/homebrew/bin/python3.12 \
  --dry-run
```

`--dry-run` 只检查输入、source pin、Python 版本、wheelhouse 名称和 supplied bundle 的完整 manifest/SHA；不会创建目录、克隆、建 venv、安装依赖、运行 smoke 或修改配置。

## 安装

```sh
/opt/homebrew/bin/python3.12 scripts/install-squilla-advisor.py \
  --home "$HOME/Library/Application Support/Codex Workbench" \
  --source-root /private/tmp/opensquilla-research.4Om6Cs/source \
  --bundle-dir /private/tmp/opensquilla-native-Dmyrla/bundle \
  --wheelhouse /private/tmp/opensquilla-native-Dmyrla/wheelhouse \
  --python /opt/homebrew/bin/python3.12
```

安装目标固定为：

```text
<home>/advisors/opensquilla/
├── source/  # 本地 Git clone，保留 .git，供运行时 HEAD 校验
├── bundle/  # 已验证的 V4 Phase 3 bundle
├── venv/    # 直接建在最终路径，避免 Python venv 的绝对 shebang 失效
└── installation-receipt.json  # 一次本地 smoke 的无提示 receipt
```

安装在写入 `config.json` 前，使用最终 `venv`、`source` 和 `bundle` 运行一次无凭据的 `SquillaAdvisor.advise_batch` 本地 smoke。其 prompt-free `to_receipt()` 会先写入 `installation-receipt.json`，然后才更新配置；这让后续只读 health 检查可查看当次 source/classifier 证据，而无需再次推理。若 clone、bundle 校验、离线 pip、smoke 或 receipt 持久化失败，安装器会恢复先前的 `opensquilla` 目录和原始 `config.json` 字节。

若已有安装，成功替换后的先前版本会保留为 `<home>/advisors/.opensquilla.previous-<id>`；成功 JSON 的 `previous_install_backup` 返回它的确切路径，安装器不会自动清理它。

成功后只合并 `config.json` 的 `squilla_advisor` 字段，保留其他顶层字段和该对象的未知嵌套字段：

```json
{
  "squilla_advisor": {
    "enabled": true,
    "runtime_python": "<home>/advisors/opensquilla/venv/bin/python",
    "source_root": "<home>/advisors/opensquilla/source",
    "bundle_dir": "<home>/advisors/opensquilla/bundle",
    "timeout_seconds": 45.0
  }
}
```

安装器不会自动重启任何 MCP 或常驻服务。成功 JSON 中的 `restart_required.long_running_mcp_or_service: true` 表示该类进程需要由操作员在合适时机重启；一次性 CLI 会在下一次调用时读取新配置（`one_shot_cli: "next_invocation"`）。

`--dry-run` 不会运行 native smoke，因此输出不含 `native_receipt`。

## 定向测试

测试只使用合成的小型 Git/bundle fixture，并 mock 掉 clone、venv、pip 与 advisor 边界；不会下载依赖或运行实际安装：

```sh
/opt/homebrew/bin/python3.12 -m unittest tests/test_squilla_installer.py
```
