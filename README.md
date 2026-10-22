# 🐱可视化，结构紧凑明快。

---

### 修改后的 `README.md` 全文

```markdown
# Codex Against Traffic Spam (CATS)

> **让大猫少搬几箱上下文砖，拒绝“流量刺客”与无谓重传。**

[![Platform](https://img.shields.io/badge/Platform-Windows%20 🐾 Codex Against Traffic Spam (CATS)

<p align="center">
  <img src="assets/cat.png" alt="CATS 猫图" width="360" />
</p>

<p align="center">
  <strong>x64-blue?logo=windows)](https://github.com/Khiyyya/codex-cats/releases)
[![Python](https://img.shields.io/badge/Python-Embedded%203.14-让大猫少搬几箱上下文砖，把流量留给真正的思考。</strong><br>
  面向green?logo=python)](https://github.com/Khiyyya/codex-cats)
[![License](https:// Windows 官方 Codex Desktop 的智能上下文网关伴侣
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Windows_x64-blue.svg?styleimg.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/Cod.shields.io/badge/Status-Experimental-orange)](#)

[English](README.en.md) |ex_Desktop-Compatible-brightgreen.svg?style=flat-square" alt="Codex">
  < [更新日志](CHANGELOG.md)

![CATS 架构概览](assets/cat.png)

**img src="https://img.shields.io/badge/Traffic_Saved-~50%25-CATS** 是一个专为 Windows x64 上官方 **Codex Desktop** 打造的本地智能上下文网关。

orange.svg?style=flat-square" alt="Traffic Saved">
  <img src="https://img.shields.io/badge/Privacy-Local_Only-green.svg?style=flat-square" alt="Privacy">
在多轮长对话、频繁调用工具或附带高分辨率图片的场景下，客户端往往会向云端反复上传庞大的完整历史上下文。CATS 充当轻量级本地伴侣，**精准识别可安全延续的对话上下文**，将冗余的全量重传压缩为极小的增量请求，显著降低上行带宽占用并减少连接  <img src="https://img.shields.io/badge/License-MIT-lightgrey.svg?style=flat-square" alt="License">
</p>

---

## 💡 为什么需要 CATS？

官方 Codex Desktop 在处理长对话或包含多张大图的任务时，即便对话只追加了一句简短提问，也经常会**全量重新打包并重复上传整个历史上下文**。

CATS 作为一个抖动风险。

---

## ✨ 核心亮点

- ⚡ **智能增量延续**：严格校验本地透明网关，能够在本地识别可复用的上下文状态，智能转换为 **`上一代 ID + 增量输入会话指纹与上下文连贯性，自动将符合条件的重传转换为 `previous_response_id + 增量 Delta`，多模态长对话收益尤为显著。
- 🛡️ **自适应健康探测与安全降级**：
  - 针对大载荷自动执行 WS Ping / HTTP HEAD 预检，避免网络`** 发送给上游，显著减少不必要的大载荷重传。

- 📉 **流量立脆弱时盲目大包冲撞。
  - 遇到状态不一致或上游明确拒绝（如失效减**：长多轮/含图会话上行流量直接砍半（实测减少 ~49% 代理上行）。
- ⚡ **无感加速**：无需重复上传数 MB 的 Base64 图片与会话）时，**零感知安全回退为全量重发**，绝不丢上下文，也不替历史记录，提问响应更轻快。
- 🛡️ **安全平替**：仅在严格确认上下文一致时启用增量；无法复用时自动无感回退全量。

---

## 📊 性能实测

> **测试场景**：单会话含 3 张 PNG 图片（Base64 模型“瞎猜”。
- 🔒 **隐私与安全优先**：
  - 纯本地 Loopback 回约 10.8 MB），共进行 4 代连续交互（含网络波动重连模拟）。模型环监听，不记录任何聊天文本、图片正文、账号 Token 或 Cookie。
  - 本地 Metrics为 `gpt-6-astra`。

| 核心指标 | 原生官方链路 | 挂载 CATS | 优化效果 |
| :--- | :---: | :---: | :---: |
| **代理链 仅统计流量大小与调度状态；不修改系统全局代理与证书链。
- 📦 **零系统路上行流量** (TLS + CONNECT) | 32.44 MB | **16.55 MB** |污染（Zero-Config）**：
  - 内置独立沙箱运行环境（固定版私有 Python 🔻 **减少 48.97%** |
| **应用层 JSON 请求载荷** | 43.23 MB | **21.58 MB** | 🔻 **减少 50.07 运行时及全套依赖），不依赖、不污染系统的 Python 环境。
  - 一键安装/卸载，支持原%** |
| **第 3 代单次发送载荷** | 10.82 MB | **2.48 KB** | ⚡ **近乎瞬时发送** |
| **第 4 代单次发送配置秒级无损还原。

---

## 📊 性能实测

在包含 **3 张高分辨率 PNG（载荷** | 10.82 MB | **2.50 KB** | ⚡ **近乎瞬时发送合计约 10.8 MB 上下文）**、经历模拟网络抖动恢复的 4 轮真实多模态会话中实测对比：

![隔离实测结果](assets/test-results.svg)** |
| **多模态与工具调用结果** | 正常 | **100% 准确一致** | 无损透传 |

*注：流量统计取自实际代理上行链路字节，包含

| 核心指标 | 原生官方直连链路 | 接入 CATS 本地网关 | 优化握手与网络传输开销。*

---

## ✨ 核心特性

- **开箱即用，零幅度 |
| :--- | :---: | :---: | :---: |
| **代理链依赖**：安装包内嵌独立专属运行环境，**无需电脑预装 Python**，不污染系统 PATH路上行传输量 (TLS+CONNECT)** | 32.44 MB | **16.55 MB，不改动系统代理与系统证书。
- **纯内存基线维护**：增量基线保存在本地** | 📉 **-48.97%** |
| **已处理规范化应用层载荷** | 43.23 MB | **21.58 MB** | 📉 **-5内存中，连接失效自动刷新，不依靠粗暴删减历史记录或压缩图片质量来省流量。
-0.07%** |
| **第 3 轮增量请求载荷** | ~1 **主动健康探测**：针对大请求（>256 KiB）自动进行静默健康探测，上游不可达0.82 MB | **2,480 B** | 🚀 **近乎零开销** |
| **第 4 轮增量请求载荷** | ~10.82 MB | **2,5时提前拦截，避免无效等待与客户端硬重连。
- **配置自愈与安全隔离**：安装05 B** | 🚀 **近乎零开销** |
| **多模态记忆与工具调用过程带原子检查与自动回退保护，卸载时一键完全还原 Codex 原始配置。

---检查** | 4 轮全部通过 | 4 轮全部通过 | 🎯 **100% 保

## 🚀 快速上手

### 系统要求
* Windows x64
* 已安装官方 [Codex Desktop](https真** |

> *注：上述测试基于官方 `codex.exe` 核心（`gpt-://openai.com/) 并登录账号

### 1. 安装
1. 前往 [Releases](https://github.com/Khiyyya/codex-cats/releases) 下载最新 `codex-cats-vX.X.X6-astra` 模型）。节省效果取决于对话中历史重复度与多模态资源占比；原生短断线.zip` 并解压。
2. 双击运行 `INSTALL.cmd`。
3. 重启一次恢复或单轮短文本对话的收益会有所不同。*

---

## 🚀 快速开始

###正在运行的 Codex Desktop 即可生效。

### 2. 常用管理入口

解压目录已预置一 前提条件
- 操作系统：Windows 10 / 11 (x64)
- 已键管理脚本：

| 脚本文件 | 说明 |
| :--- | :--- |
| `OPEN-CODEX.cmd` | 推荐入口：先检查/启动网关，随后打开 Codex |
| `STATUS.cmd` | 查看当前安装并登录官方 [Codex Desktop](https://openai.com/)

### 一键安装
1. 前网关状态与已节省的流量指标 (Metrics) |
| `START-GATEWAY.cmd` | 单往 [Releases](https://github.com/Khiyyya/codex-cats/releases) 下载最新版本的独启动本地后台网关 |
| `STOP-GATEWAY.cmd` | 停止本地后台网关 |
| 压缩包并解压至本地。
2. 双击运行 **`INSTALL.cmd`**。
3.`DOCTOR.cmd` | 运行一键健康与连通性自检 |
| `UNINSTALL.cmd` | 停止 **重启一次正在运行的 Codex**，即可完成无缝接管。

### 日常控制入口

解服务并彻底卸载，**自动恢复官方原始配置** |

---

## ⚙️ 工作原理

```mermaid
flowchart TD
    Client[Codex Desktop Client] -->|本地回环| CATS[CATS 本地网关]
压目录及安装目录均包含快捷控制脚本：

| 脚本 | 说明 |
| :--- | :--- |
| `OPEN-CODEX.cmd` | **【推荐】** 先拉起后台网关，随后自动打开 Codex |
| `START-GATEWAY.cmd` | 单独启动本地后台网关 |
| `STOP    
    subgraph CATS 核心逻辑
        CATS --> Check{上下文历史与上游连接是否严格-GATEWAY.cmd` | 停止网关运行 |
| `STATUS.cmd` | 查看网关健康状态与实时流量节省摘要 |
| `DOCTOR.cmd` | 运行一键环境体检与链路匹配?}
        Check -- 严格匹配 --> Delta[发送 previous_response_id + 增量 Input]
        Check -- 不诊断 |
| `UNINSTALL.cmd` | 完全卸载并**一键自动恢复 Codex 初始配置** |

---匹配/新连接 --> Full[透传完整上下文]
        Delta --> HealthCheck{大请求连接探测}
        Full --> HealthCheck
    end
    
    HealthCheck -->|连接健康| Upstream

## 🛠️ 工作原理

CATS 采用严格的**状态确认机制**，仅在确保上下游基[真实 ChatGPT 上游接口]
    HealthCheck -- 探测失败 --> Reject线一致的前提下触发增量优化：

```mermaid
flowchart TD
    Req[客户端发出请求] --> Check{检测上下文会话基线}
    Check -- 严格一致 & WS存活 --> Delta[本地 503 快速失败，触发客户端平滑重连]
