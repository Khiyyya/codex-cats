# Codex Against Traffic Spam (CATS)

<p align="center">
  <img src="assets/cat.png" alt="CATS cat" width="240" />
</p>

<p align="center"><strong>Save your Codex proxy traffic</strong></p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows_x64-blue" alt="Windows x64" />
  <img src="https://img.shields.io/badge/status-experimental-orange" alt="Experimental" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/code_license-MIT-green" alt="Code license: MIT" /></a>
</p>

<p align="center">
  <a href="README.md">中文</a> ·
  <a href="#installation-and-use">Installation and use</a>
</p>

CATS is an experimental local gateway companion for the official Windows Codex Desktop. It combines server-confirmed context reuse with connection probes to reduce repeated uploads of multimodal context and messages, saving bandwidth and proxy traffic.

## Why CATS?

When a long conversation resends its complete history, the same multimodal context consumes proxy traffic again. CATS applies one continuation policy to HTTP and WebSocket context requests. Matching accounts, sessions, configuration and history can use `previous_response_id + incremental input`; other requests send their complete input to establish a new baseline.

Reconnections, transport fallback, session recovery and other events can trigger repeated uploads of historical images and file content. CATS handles these repeated-upload scenarios across HTTP and WebSocket, evaluating request content against confirmed server context. Eligible requests send deltas, and larger payloads receive a connection check before sending.

## Capabilities

- **Automatic context continuation:** Codex can trigger unnecessary full-context uploads in some situations. CATS applies the same strict continuation checks to HTTP and WebSocket requests, reusing completed context on the same live upstream connection.
- **Large-payload connection checks:** unstable connections can lead to repeated uploads during retries that still consume proxy traffic. CATS runs WS Ping or HTTP HEAD probes before larger sends. Failed probes return a retry signal so the client can reconnect.

## Measurement: about 10.8 MB of image context

A paired experiment used **three different PNGs**, totaling about **10.8 MB of Base64 context**, to compare native request forwarding and CATS across connection interruption, recovery and subsequent requests. Each arm completed four generations.

![CATS traffic comparison](assets/test-results.svg)

| Metric | Native requests via relay | CATS | **Upload reduction** |
| --- | ---: | ---: | ---: |
| Observed proxy-path upload (TLS + CONNECT) | 32.44 MB | 16.55 MB | **48.97%** |
| Canonical JSON payload for completed generations | 43.23 MB | 21.58 MB | **50.07%** |
| Third-generation JSON payload | 10,822,938 B | 2,480 B | **99.98%** |
| Fourth-generation JSON payload | 10,824,191 B | 2,505 B | **99.98%** |

## How it works

```mermaid
flowchart TD
    A["Codex context request: HTTP / WebSocket"] --> B["Local CATS gateway"]

    subgraph CONTEXT["Automatic context continuation"]
        B --> C{"Account, session, configuration and history match?"}
        C -->|Match and original upstream WS alive| D["previous_response_id + incremental input"]
        C -->|Establish new baseline| E["Complete input"]
    end

    subgraph HEALTH["Large-payload connection checks"]
        D --> F{"Pending payload reaches 256 KiB?"}
        E --> F
        F -->|At threshold| G["WS Ping / HTTP HEAD"]
        F -->|Small payload| H["Send request"]
        G -->|Probe passes| H
        G -->|Probe fails| I["HTTP 503 / WS 1013"]
        I --> J["Client continues reconnecting"]
    end

    H --> K["Real ChatGPT upstream"]
    K -->|response.completed| L["Update confirmed context baseline"]
```

Context baselines stay in memory. When a client connection changes, CATS checks the existing upstream connection and baseline. A rebuilt upstream connection establishes a fresh baseline through complete input, then evaluates subsequent requests for continuation.

Health probes describe connectivity at the time of the check. Failed probes return HTTP 503, or a WebSocket error followed by close code 1013, allowing the client to reconnect. Requests over 12 MiB may prepare context using batched `generate=false` warmups. A CATS-generated delta explicitly rejected with `previous_response_not_found` in the first upstream event receives one full retry.

HTTP-to-WebSocket conversion adapts the transport envelope. Other native endpoints retain their request paths, and HTTP requests with body-bound signatures use native forwarding. Complete image content and historical input form the basis for forwarding and continuation checks.

## Installation and use

**Requirements:** Windows x64, PowerShell 5.1+, Git; official Codex Desktop signed in through ChatGPT with the default OpenAI provider; a direct connection or credential-free HTTP(S) proxy.

Run in PowerShell:

```powershell
git clone https://github.com/Khiyyya/codex-cats.git
cd codex-cats
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 -Action install
```

The installer prepares a dedicated Python environment, discovers Codex, and configures the gateway and current-user login startup. Restart Codex after installation.

### Management commands

Run the corresponding file from the project or installation directory:

| Entry point | Function |
| --- | --- |
| `INSTALL.cmd` | Install or update |
| `OPEN-CODEX.cmd` | Start the gateway and open Codex |
| `START-GATEWAY.cmd` | Start the gateway and enable integration |
| `STOP-GATEWAY.cmd` | Restore integration configuration and stop the gateway |
| `STATUS.cmd` | Show status and application-payload statistics |
| `DOCTOR.cmd` | Check environment, autostart and proxy connectivity |
| `UNINSTALL.cmd` | Restore configuration, remove startup entries and clean the installation directory |

After stopping or uninstalling, restart Codex to load the restored configuration.
