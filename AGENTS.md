# AGENTS.md

## 项目概览
- 本仓库是一个 **Windows 桌面工具**，用于管理和增强 Codex CLI / VS Code / OpenAI 相关本地配置。
- 技术栈以 **Python 3.12 + PySide6** 为主，依赖由 **uv** 管理。
- 当前主要入口是 `pyside_switcher.py`，共享逻辑集中在 `codex_switcher.py`。
- 打包通过 `PyInstaller` 完成，构建脚本为 `build.ps1`，spec 文件为 `codex_switcher.spec`。

## 代码结构
- `pyside_switcher.py`
  - GUI 主程序与页面实现。
  - `main()` 为本地运行入口。
  - 主要页面都定义在这个文件里，例如账号切换、网络诊断、Codex 状态、配置切换、Session 管理、Skill 管理、VS Code 插件页等。
- `codex_switcher.py`
  - 与 UI 解耦的共享逻辑。
  - 负责账号存储、配置写入、网络探测、日志、路径处理等。
- `pyproject.toml`
  - Python 项目元信息与依赖声明。
- `build.ps1`
  - Windows 下标准打包脚本：同步依赖 → 生成 ico → 执行 PyInstaller。
- `codex_switcher.spec`
  - PyInstaller 打包配置。
- `docs/`、`ui_shots/`
  - 文档截图与界面资源补充目录。

## 运行与构建
- 安装依赖：`uv sync`
- 本地启动：`uv run python pyside_switcher.py`
- 构建依赖：`uv sync --group build`
- Windows 打包：`./build.ps1`

## 运行环境假设
- 目标平台是 **Windows**。
- 代码里存在明显的 Windows 行为与路径约定，例如：
  - 使用 `USERPROFILE`
  - 操作 `~/.codex`、`~/.codex-config-switch`
  - 适配 Windows 隐藏窗口/文件属性
  - 构建脚本使用 PowerShell
- 如非必要，不要为了兼容其他平台而引入额外抽象。

## 重要数据与副作用
- 用户数据/配置主要落在以下目录：
  - `~/.codex/config.toml`
  - `~/.codex/auth.json`
  - `~/.codex/codex_profiles.json`
  - `~/.codex/codex_switcher.log`
  - `~/.codex-config-switch/`
- 修改相关逻辑时，优先保证：
  - 不破坏已有文件格式
  - 写入尽量保持幂等
  - 失败时给出清晰提示
- 不要在未确认的情况下随意删除、清空、覆盖用户本地配置或会话数据。

## 修改建议
- 这个仓库目前以 **少量文件集中实现功能** 为主，不要为了“看起来更优雅”做大规模拆文件重构，除非用户明确要求。
- 新增功能时，优先沿用现有模式：
  - UI 放在 `pyside_switcher.py`
  - 纯逻辑放在 `codex_switcher.py`
- 保持现有代码风格：
  - 中文界面文案
  - 类型标注按当前风格增量维护
  - 小步、局部修改，避免无关重排
- 不要顺手改动版本号、构建产物命名、图标资源，除非任务明确要求。

## UI 相关约定
- 这是一个真实可交互的桌面 UI 项目；涉及界面修改时，优先验证：
  - 页面能否打开
  - 按钮/输入框是否可用
  - 明暗主题下是否可读
  - 关键流程是否仍可走通
- 主题能力已经存在，涉及样式调整时要同时考虑 light / dark。
- 现有页面类较多，修改前先确认具体落点，不要在错误页面上做改动。

## 测试与验证
- 仓库当前没有明显的自动化测试目录或测试文件。
- 做完改动后，优先采用最小验证：
  - 能运行：`uv run python pyside_switcher.py`
  - 若改动影响构建，再考虑执行 `./build.ps1`
- 不要为了当前任务额外引入测试框架。

## 依赖与打包
- 依赖由 `uv` 管理，不要切回其他依赖管理方式。
- 打包依赖在 `build` dependency group 中声明。
- 修改打包相关内容时，注意 `codex_switcher.spec` 会从 `pyside_switcher.py` 中提取 `APP_VERSION`。

## 提交改动前的检查清单
- 是否只改了与任务直接相关的内容。
- 是否保持 Windows 场景可用。
- 是否避免破坏 `~/.codex` 下已有配置。
- 是否同步考虑了 light / dark 主题（如涉及 UI）。
- 是否使用 `uv` 命令进行运行/构建验证。

## 给后续 agent 的建议
- 先读 `README.md`、`pyproject.toml`、`pyside_switcher.py`、`codex_switcher.py`，再动手。
- 如果任务涉及具体页面，先搜索对应页面类名再改。
- 如果任务涉及本地配置写入，先确认目标文件路径和当前写入逻辑，避免重复造轮子。
- 如果用户只要求小修，不要主动做架构级重构。
