# macOS 实验版

[项目说明](README.md) · [English / Windows measurement](README.en.md)

## Mac 是否存在同样的问题？

已在本机官方 Mac 核心和真实 ChatGPT 上游完成隔离流量实验：断线回退 HTTP 后确实观察到完整历史重传，CATS 的同轮工具调用增量可以减少这部分上传；稳定原生 WebSocket 场景没有明显额外收益。完整方法、成对数据与观察脚本修正见 [Mac 流量实验报告](experiments/RESULTS.macos.md)。这不是 Desktop GUI 直连抓包，也不代表普遍发生频率。

原生 WebSocket 本来就支持 `previous_response_id + 增量 input`，此时 CATS 可能没有额外收益。更值得验证的是：客户端回退到 HTTP 并在多轮中重复发送完整历史，而 CATS 仍能维持同一条上游 WebSocket 的情况。上游连接丢失后，CATS 自己也必须重新建立全量基线。

这与官方 [WebSocket 续传与重连说明](https://developers.openai.com/api/docs/guides/websocket-mode)一致。官方 [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)列出了内置 provider 的 `openai_base_url`。ChatGPT 后端的 HTTP→WS 适配仍是本项目的实验性兼容路径，公开 API 文档不构成官方对该网关的支持保证。

**Windows 上单次实验的 48.97% 不能作为 Mac 的承诺，也不代表 token 用量或订阅额度节省。**

## 前提条件

- macOS 桌面登录会话；开发验证环境为 Apple Silicon。
- 已安装包含 Codex 核心的 `Codex.app` 或 `ChatGPT.app`，并使用 ChatGPT 账号登录。
- 稳定安装的 Python 3.11+，包含 `venv` / `pip`；首次安装需要联网下载依赖。
- 内置 `openai` provider，且没有自定义 base URL。PAC/WPAD、SOCKS 和 URL 内含凭据的代理会被拒绝；可以显式指定不含凭据的 HTTP(S) 代理。

本版建立独立虚拟环境，仍依赖创建该环境的基础 Python。它没有 Windows 版本的内嵌 Python，也没有 `.app` / `.dmg`、签名或公证分发包。Intel Mac、Mac GUI 实际模型请求和真实登录自启尚未验证。

## 安装与管理

在仓库目录双击 `INSTALL-MAC.command`，或在终端执行：

```bash
bash scripts/macos.sh install
```

默认状态与运行时位于 `~/Library/Application Support/CodexCATS`。安装成功后，重启已经打开的 Codex/ChatGPT 一次，让它读取配置。

| Finder 入口 | 终端命令 | 功能 |
| --- | --- | --- |
| `INSTALL-MAC.command` | `bash scripts/macos.sh install` | 安装运行时、启动后台、激活配置 |
| `OPEN-CODEX-MAC.command` | `bash scripts/macos.sh open` | 确保网关启动后打开检测到的应用 |
| `START-GATEWAY-MAC.command` | `bash scripts/macos.sh start` | 启动并重新激活 |
| `STOP-GATEWAY-MAC.command` | `bash scripts/macos.sh stop` | 恢复配置并移除登录自启、停止后台 |
| `STATUS-MAC.command` | `bash scripts/macos.sh status` | 本地健康、配置、LaunchAgent 与应用层载荷统计 |
| `DOCTOR-MAC.command` | `bash scripts/macos.sh doctor` | 应用识别、代理来源与启动错误诊断 |
| `UNINSTALL-MAC.command` | `bash scripts/macos.sh uninstall` | 移除集成，保留运行时和备份供手动清理 |

停止或卸载后也应重启原本已打开的 Codex。确认卸载成功后，可以手动删除状态目录；该目录可能含配置备份，应按私人数据处理。卸载不会删除原始 Python 或源代码。

如应用或 Python 安装位置不同，可在第一次安装时指定：

```bash
CATS_CODEX_APP="/Applications/ChatGPT.app" \
CATS_PYTHON="/absolute/path/to/python3" \
bash scripts/macos.sh install
```

`CATS_CODEX_APP` 按 bundle identifier `com.openai.codex` 和内置核心识别，不单凭显示名称。`CATS_HOME` 可改为其他绝对状态目录，之后每次管理都须使用相同值；应用目录变更时继续设置 `CATS_CODEX_APP`。

Finder 的 Terminal 环境可能与平时的 shell 不同。找不到合适 Python 时，用上述终端命令指定。更换运行时或环境代理前先 `stop`，随后再 `start`。升级项目代码时，先 `uninstall`，移走保留的状态目录，再从新源代码执行安装；本版没有自动升级器。基础 Python 移除或损坏会使 venv 不可用，应在变更前先停止 CATS。

## macOS 适配内容

- 复用同一 Python 网关、严格会话匹配和配置所有权恢复逻辑。
- 默认发现 `/Applications`、`~/Applications` 下的 Codex/ChatGPT 应用，支持显式 `.app` 路径。
- 优先读取显式代理环境变量，否则通过 `scutil --proxy` 检测系统 HTTP(S) 代理。不会修改系统代理、PATH 或证书。
- 用用户级 `~/Library/LaunchAgents/io.github.khiyyya.codex-cats.<scope>.plist` 和 `launchctl bootstrap/bootout` 管理后台，无需 sudo。参考 [Apple 的 LaunchAgent 说明](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)。
- 登录自启时重新检测系统代理；安装时显式指定的代理会传入 LaunchAgent，值必须不含凭据。更换它需先停止再启动。
- `SIGTERM` / `SIGINT` 触发正常关闭及配置恢复；强制杀进程、系统崩溃等不能保证执行清理。
- 先确认本地健康再激活配置；失败时尝试回滚。端口被其他监听者占用或 LaunchAgent 所有权不符时拒绝接管。

用户配置默认是 `~/.codex/config.toml`，也遵循 `CODEX_HOME`。已有非 CATS base URL / 自定义 provider 时会保留并拒绝自动接入；停止时保留配置中的其他用户改动。

## 本版验证与限制

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

测试在临时目录运行，使用真实 loopback HTTP/WS、模拟上游，以及真实 Python 网关进程。LaunchAgent 管理和应用发现使用测试替身，不写用户真实 Codex 配置、账号或登录自启目录。受限沙箱需允许本地 loopback 监听。

覆盖代理检测、应用识别、带空格路径、保留 venv 解释器、启动/停止/重启、SIGTERM、安装失败回滚、配置恢复、用户改动保护、端口冲突和 LaunchAgent 所有权；网关检查覆盖同连接增量、断线后全量、显式拒绝后的一次回退、会话隔离和控制令牌。

以上离线检查不等于真实 LaunchAgent 登录恢复或 Mac GUI 端到端兼容性。此外已完成 [真实核心/上游的成对流量实验](experiments/RESULTS.macos.md)，分别覆盖原生 WS、自然 HTTP 回退、12 秒断线、多图历史回忆和动态工具结果；记录应用载荷及本机到既有代理的 CONNECT/TLS 字节，不能视为 WAN/NIC 流量。真实 GUI、退出/重新登录、睡眠唤醒及其他网络仍待验证。
