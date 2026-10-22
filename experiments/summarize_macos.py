#!/usr/bin/env python3
"""Render a paired experiment report from saved, content-free request statistics."""
import argparse
import json
from pathlib import Path
import statistics


def render(data):
    arms = data['arms']
    pairs = {}
    for arm in arms:
        key = (arm['scenario'], arm['repetition'])
        pairs.setdefault(key, {})[arm['mode']] = arm
    if any(set(pair) != {'native', 'cats'} for pair in pairs.values()):
        raise ValueError('report requires complete native/CATS pairs')
    expected = {(scenario, repeat) for scenario in ('stable', 'outage') for repeat in (1, 2)}
    if set(pairs) != expected:
        raise ValueError('formal report requires stable/outage, each with both planned repetitions')
    if not arms or any(not arm.get('all_valid') for arm in arms):
        raise ValueError('a failed or incomplete arm must be investigated before making a savings report')
    scenarios = sorted({arm['scenario'] for arm in arms}, reverse=True)
    summary = {}
    table = [
        '| 场景 | 原生各次上行（B） | CATS 各次上行（B） | 原生均值（MB） | CATS 均值（MB） | 节省比例 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    for scenario in scenarios:
        rows = [arm for arm in arms if arm['scenario'] == scenario]
        grouped = {mode: sorted([arm for arm in rows if arm['mode'] == mode], key=lambda arm:arm['repetition'])
                   for mode in ('native', 'cats')}
        n, c = [statistics.mean(arm['proxy_upload_bytes'] for arm in grouped[mode]) for mode in ('native', 'cats')]
        saving = 100*(n-c)/n
        summary[scenario] = {'native': n, 'cats': c, 'saving': saving}
        label = {'stable': '稳定网络', 'outage': '首轮后断线 12 秒'}[scenario]
        raw = [' / '.join(f"{arm['proxy_upload_bytes']:,}" for arm in grouped[mode]) for mode in ('native', 'cats')]
        table.append(f'| {label} | {raw[0]} | {raw[1]} | {n/1e6:.4f} | {c/1e6:.4f} | {saving:+.3f}% |')
    image_table = [
        '| 断线实验 | 命中增量的请求数 | 省去重传的历史图片引用数 | 原完整请求合计（B） | 实际增量合计（B） | 应用载荷减少 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    optimized = []
    for arm in sorted(arms, key=lambda arm: arm['repetition']):
        if arm['mode'] != 'cats' or arm['scenario'] != 'outage':
            continue
        rows = [row for row in arm.get('gateway_metrics', []) if row['reason'] == 'live_continuation']
        if not rows:
            image_table.append(f"| 第 {arm['repetition']} 次 | 0 | 0 | — | — | 未命中 |")
            continue
        before = sum(row['application_bytes_before'] for row in rows)
        after = sum(row['application_bytes_after'] for row in rows)
        skipped = sum(row['images_before'] - row['images_after'] for row in rows)
        image_table.append(f"| 第 {arm['repetition']} 次 | {len(rows)} | {skipped} | {before:,} | {after:,} | {100*(1-after/before):.4f}% |")
        optimized.extend(rows)
    image_before = sum(row['images_before'] for row in optimized)
    image_after = sum(row['images_after'] for row in optimized)
    turns = [turn for arm in arms for turn in arm['turns']]
    colors = [turn['color_correct'] for turn in turns if 'color_correct' in turn]
    tools = [turn['tool_correct'] for turn in turns if 'tool_correct' in turn]
    cats_colors = [turn['color_correct'] for arm in arms if arm['mode'] == 'cats'
                   for turn in arm['turns'] if 'color_correct' in turn]
    cats_outage_colors = [turn['color_correct'] for arm in arms
                          if arm['mode'] == 'cats' and arm['scenario'] == 'outage'
                          for turn in arm['turns'] if 'color_correct' in turn]
    outage = summary.get('outage')
    outage_pairs = [100*(pair['native']['proxy_upload_bytes']-pair['cats']['proxy_upload_bytes'])
                    / pair['native']['proxy_upload_bytes'] for key, pair in sorted(pairs.items()) if key[0] == 'outage']
    conclusion = ('复测显示：Mac 官方核心在断线后回退 HTTP 时会重复发送完整历史；本版 CATS 在该场景有节流收益。'
                  if outage and outage['saving'] > 1 else
                  '本次正式复测没有建立可用的节流收益结论。')
    lines = [f"# macOS 真实上游流量实验（{data['date']}）", '', conclusion,
        '稳定 WebSocket 场景中，原生已经使用增量，CATS 的额外收益接近零。收益取决于传输与恢复状态，不能视为普遍加速、固定节流比例或 token/额度节省。', '',
        '## 功能一：整体上下文增量与流量结果', '',
        '每种场景重复两次，首对 native→CATS，第二对 CATS→native。MB 使用十进制；正节省比例表示 CATS 较少，负值表示较多。下表使用修正后的 v2 观察中继。', '', *table, '',
        '断线两对各自的节省比例为 ' + ' / '.join(f'{value:.2f}%' for value in outage_pairs) + '。连接重置和回退会影响收益，不能只选较好的那一对作为结论。', '',
        f"正式复测共 **{len(arms)} 次会话、{len(turns)}/{len(turns)} 轮完成、{sum(colors)}/{len(colors)} 次图片历史回忆正确、{sum(tools)}/{len(tools)} 次工具调用与返回值正确**。", '',
        '首轮只允许回答 READY，不输出颜色；后续每轮问原始某张图的中心颜色，并要求调用一次 bench_probe。后续提示词不含颜色答案，工具返回整数不预先提供。测试证明这些检查下的完成性与内容保留，不证明所有任务输出完全等价或主观质量相同。每场景仅两对样本，不做显著性或长期收益结论。', '',
        '## 功能二：历史图片免重传（图片过滤）', '',
        '这里的“图片过滤”指：确认同一存活上游连接已经保留完整历史后，发送 previous_response_id 与新增输入，使历史图片正文无需再次上行。当前代码没有独立的图片压缩、降采样或任意位置的相同图片去重器。', '',
        *image_table, '',
        f'两次断线实验共命中 {len(optimized)} 个增量请求，这些请求中的历史图片引用从 {image_before} 次降至 {image_after} 次；这是同一组三张图片在多次请求中的引用次数，不是 {image_before} 张不同图片。典型单次请求从约 10.9 MB 降至约 2 KB，三张历史图片正文从再次携带变为无需重发。', '',
        '表中约 99.98% 是命中请求的完整规范化 JSON 载荷减少比例，包含历史文字、图片和其他输入，不能称为图片编码压缩率或整场网络节省比例。两部分使用同一批实验：功能一给出整场代理字节，功能二给出其中命中请求的历史图片免重传效果，收益不能相加。', '',
        f'图片内容保留检查为上述 {sum(colors)}/{len(colors)} 次历史颜色回忆正确；其中 CATS 两种场景合计 {sum(cats_colors)}/{len(cats_colors)} 次，断线 CATS 子集合为 {sum(cats_outage_colors)}/{len(cats_outage_colors)} 次。这是合成图片的内容检查，不代表任意图像任务的完整质量评估。', '',
        '首次上传、新增图片、连接丢失后重建基线，以及历史或会话不匹配时仍须发送完整内容；稳定原生 WebSocket 已经增量时通常没有额外图片节流。CPU、峰值内存、首图上传耗时和独立处理延迟尚未做专项基准。', '',
        '## 环境与输入', '',
        f"- macOS：`{data['platform']}`。",
        f"- Desktop `{data['desktop_version']}`，内置核心 `{data['core']}`。",
        f"- 模型 `{data['model']}`，reasoning effort 为 `{data['effort']}`；Python 3.12.4，依赖使用项目固定版本。",
        '- 三张 1024×1024 合成 PNG，独立随机背景和明显中心色块，PNG 总计 8,137,973 B，data URL 共 10,850,698 B。图像在内存生成，附件与提示词不携带颜色名称。',
        '- 真实官方 ChatGPT 上游、现有登录和本机既有 HTTP 代理。官方核心使用临时 CODEX_HOME 中的凭据副本，结束后删除。不保存请求头值、凭据、用户聊天或图片正文；保存的回答仅为合成测试答案。',
        '- 每次使用全新的临时会话和配置，没有修改用户现有配置、安装登录自启或中断现有聊天。', '',
        '## 测量边界与方法', '',
        '```text',
        '原生：官方 Mac 核心 → 观察中继 → 字节计数 CONNECT 隧道 → 本机既有代理 → 官方上游',
        'CATS：官方 Mac 核心 → 相同观察中继实现 → CATS → 字节计数 CONNECT 隧道 → 本机既有代理 → 官方上游',
        '```', '',
        '- 没有强制关闭 WebSocket。HTTP 回退由官方核心在断线后自然发生。首轮后关闭实验专属连接，12 秒内拒绝实验新建的 CONNECT；不改本机代理或影响其他会话。',
        '- 主指标为中继成功 drain 到既有代理 socket 的 CONNECT + TLS 字节，包含握手、传输压缩和重试。只允许 chatgpt.com:443，不解密 TLS 或修改证书。',
        '- **不是 WAN/NIC 抓包，不统计 TCP 重传，也不是服务端收到的确认字节。** 这是当前本机→代理路径的描述性对比。',
        '- 规范化 JSON 另行记录，不能视为网络字节。完整 Base64 上下文约 10.85 MB，压缩后首轮路径上传约 8.39 MB。只在本地收到的失败大请求不能直接算作已传云端。',
        '- 原生 HTTP 不添加自动重试、重定向或 cookie；观察 WS 协商 deflate。中继改变传输封装和时序，故不能称为 Desktop GUI 直连抓包。',
        '- v2 仅在观察副本中解码压缩 SSE、记录终止事件，实际转发字节不变；客户端在终止事件后先于 EOF 关闭时正常结束中继响应。', '',
        '## 协议观察与兼容范围', '',
        '稳定组的原生后续请求已有 previous_response_id，通常只有约 2–6 KB 规范化 JSON；CATS 记为 already_incremental，不把原生自带的续传算作优势。', '',
        '断线后原生 HTTP 请求反复携带约 10.9 MB 规范化历史，zstd 压缩体约 8.16 MB。CATS 必须先在新上游连接上传完整历史；基线确认完成后，符合条件的同轮工具结果请求可变成约 2 KB 增量。具体每次完整/增量字节在 gateway_metrics 中。', '',
        '独立诊断中会话与 principal 保持一致，但每轮 x-codex-turn-metadata 变化使严格握手指纹不同，跨轮会新建基线。同轮工具结果握手指纹一致时能复用。当前实现没有为了提高节流比例而忽略这些头部；跨轮复用仍有待独立验证。', '',
        '## 旧批次与观察脚本修正', '',
        'v1 八次会话均完成了内容检查，但观察脚本对压缩 SSE 完成标志和客户端先于 EOF 关闭处理不正确，且断线后出现多次连接重置。v1 当时未观察到节流。修正观察脚本后，独立诊断出现了可复用增量，因此重新执行完整的成对实验；**最终结论只采用 v2，v1 与诊断轮不并入正式样本**。不能将差异直接归因于产品逻辑修复：CATS 网关和规划器在这两批实验之间未改动，网络瞬态同样可能影响结果。', '',
        'v1 原始数据及代码指纹保留用于审计。上述修正经过离线测试：已知 1 MiB 数据连同 CONNECT 精确计数一次，阻断不增加上行，非官方目标被拒绝，压缩 SSE 转发内容保持逐字节相同。完整本地回归 **37 项通过**。', '',
        '## 复现', '',
        '使用当前 ChatGPT 登录，会发出真实模型请求并使用账户额度。', '',
        '```bash',
        '.venv/bin/python experiments/traffic_macos.py \\',
        '  --scenarios stable outage --repetitions 2 \\',
        '  --output experiments/results/reproduction.json',
        '.venv/bin/python experiments/summarize_macos.py \\',
        '  --input experiments/results/reproduction.json \\',
        '  --output experiments/REPRODUCTION.macos.md',
        '.venv/bin/python -m unittest discover -s tests -v',
        '```', '',
        '## 产物与未验证内容', '',
        '- [正式 v2 原始统计](results/macos-2026-10-22-v2.json)；[v2 代码指纹](results/macos-v2-manifest.json)。',
        '- [单独诊断](results/macos-2026-10-22-diagnostic.json)。',
        '- [已取代的 v1 数据](results/macos-2026-10-22-v1.json)；[v1 代码指纹](results/macos-v1-manifest.json)。',
        '- [真实实验脚本](traffic_macos.py)；[报告生成器](summarize_macos.py)。', '',
        '未验证真实 Desktop GUI、账号切换、LaunchAgent 登录周期、睡眠唤醒、Intel Mac，以及其他网络。Mac 版继续标为实验版；应依据实际 HTTP 回退与上下文重复情况选择是否启用。', '',
        '协议背景：[官方 WebSocket 续传与重连](https://developers.openai.com/api/docs/guides/websocket-mode)、[官方 app-server 与动态工具协议](https://learn.chatgpt.com/docs/app-server)。实验使用安装版本生成的 schema；公开 API 文档不构成对 ChatGPT 私有后端适配的官方保证。', '',
    ]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(render(json.loads(args.input.read_text())))


if __name__ == '__main__':
    main()
