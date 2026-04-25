# CodexSwitcher

Windows 桌面工具，用于管理 Codex CLI、VS Code Codex 插件和本地 OpenAI 相关配置。项目使用 Python 3.12、PySide6 和 uv，主入口为 `pyside_switcher.py`。

## 功能
- **Codex CLI 状态**：检测本机 Codex 路径和版本，支持一键更新、启动 Codex CLI / VS Code、修复常见 WebView 视图问题。
- **config 管理**：查看和编辑当前 `~/.codex/config.toml`，并在配置库中保存、预览、切换多个 `.toml` 配置。
- **多账号切换**：管理多个账号、API Key、组织 ID 和中转站地址，一键写入当前 Codex 配置。
- **Codex 会话管理**：索引、检索、查看、导出和清理本地会话，支持从 CLI 或 VS Code 打开会话目录。
- **Skill 管理**：扫描本地 Codex skill，支持查看、导入、备份和删除用户 skill。
- **VS Code Codex 插件增强**：扫描 OpenAI/Codex 插件，支持备份后修改可用模型和关闭 VS Code 插件自动更新。
- **接口诊断**：检测中转站连通性、接口可用性，以及模型、embedding、moderation 请求结果。
- **OpenAI 官网状态**：读取 `status.openai.com` 状态 API 并展示组件状态。
- **更多设置**：切换界面主题，通过 ZIP 配置更新包批量应用本地配置变更，并显示已应用的配置包版本。

## 运行与构建
- 安装依赖：`uv sync`
- 本地启动：`uv run python pyside_switcher.py`
- 构建依赖：`uv sync --group build`
- Windows 打包：`.\build.ps1`

打包产物位于 `dist/`，文件名由 `APP_VERSION` 生成，形如 `CodexSwitcher_v2.0.5.exe`。

## ZIP 配置更新包
在「更多设置」中选择 ZIP 包或把 ZIP 文件拖入配置更新区域后，工具会读取包内的 `codex_update.yml` 或 `codex_update.yaml`，先预览操作，再备份并应用。所有 `target` 都必须位于 `%USERPROFILE%\.codex\` 或 `%USERPROFILE%\.codex-config-switch\` 下。

清单可提供顶层 `version` 字段。应用成功后，工具会把版本记录到 `%USERPROFILE%\.codex-config-switch\package_update_state.json`；之后如果更新包版本小于或等于已记录版本，会提醒用户，但不会阻止继续应用。

支持的动作：
- `copy`：从 ZIP 包内复制文件或目录到 `target`。目标不存在时是新增，目标文件已存在时默认覆盖。
- `delete`：删除指定 `target`。删除前会自动备份已有文件或目录。
- `mkdir`：创建指定目录。目录已存在时视为未变化。

清单示例：

```yaml
version: 2.0.5

operations:
  - action: copy
    source: payload/config.toml
    target: .codex/config.toml

  - action: copy
    source: payload/skills/demo
    target: .codex/skills/demo

  - action: copy
    source: payload/profiles.json
    target: .codex-config-switch/codex_profiles.json
    overwrite: false

  - action: mkdir
    target: .codex-config-switch/custom_dir

  - action: delete
    target: .codex/old_file.json
```

说明：
- `source` 是相对于 `codex_update.yml` 所在目录的 ZIP 内路径。
- `version` 用于记录和提醒重复应用或回退应用；未提供版本时不会进行版本提醒。
- `copy` 目录时会展开目录下所有文件复制到目标目录。
- `overwrite: false` 可禁止覆盖已存在文件。
- 覆盖和删除前会备份到 `%USERPROFILE%\.codex-config-switch\package_update_backups\`。
- 不允许绝对路径、`..` 路径逃逸、删除 `.codex` / `.codex-config-switch` 根目录，或直接修改备份目录。
