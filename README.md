# Claude Code Usage Tracker

macOS 工具，追踪 Claude Code（会员订阅）的用量：配额、Token 消耗与费用估算、每日使用情况等。

## 前置条件

- macOS（通过 Keychain 读取 OAuth token）
- Claude Code 已登录（`claude` CLI）
- Python 3 + requests + rumps

## 菜单栏应用

常驻 macOS 菜单栏，实时显示 Claude 用量。

```
☁ 68.0%                                    ← 标题：Session 用量
─────────────────────────────────────────
All models  57.0%  3d 22h left
Session     68.0%  3h 7m left
Extra usage 69.6%  $696/$1000
─────────────────────────────────────────
Today $11.29 · 9.0% quota
  13.9M in / 5K out
  Cost Detail ▸
  │  Opus 4.6: $11.29 = (251×$5 + 5K×$25 + 13.2M×$0.50(cr) + 732K×$6.25(cw)) /M
─────────────────────────────────────────
Local Daily ▸                               ← 本机每日费用 30 天
Quota Daily ▸                               ← 每日配额% 30 天
Weekly ▸                                    ← 周期用量 8 周
─────────────────────────────────────────
Updated 11:52 CST
Usage Settings                              ← 打开 claude.ai 用量设置
─────────────────────────────────────────
Quit
```

### 数据流

- **点击图标**：显示缓存数据，同时后台调 API 采集，完成后实时更新菜单
- **每 30 分钟**：自动调 API 采集

## 快速开始

```bash
git clone https://github.com/anthropics/ai-usage.git
cd ai-usage
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动菜单栏应用
python3 claude_usage_app.py
```

> **注意**：首次运行后数据从零开始积累。Quota Daily、Local Daily、Weekly 等历史数据需要运行一段时间后才会出现。Quota Daily 的第一天因无前一天快照做差值计算，不会显示。

## CLI 工具

```bash
# 查看用量报告（自动采集最新数据后展示）
python3 claude_usage.py

# 静默采集（不输出报告，用于定时任务）
python3 claude_usage.py --collect
```

<details>
<summary>CLI 输出示例</summary>

```
  Claude Code Usage — 2026-02-12 11:52 (CST)

  Usage
    All models .......  57.0%  (resets at 2026-02-16 09:59 CST, 3d 22h left)
    Current session ..  68.0%  (resets at 2026-02-12 14:59 CST, 3h 7m left)
    Extra usage ......  69.6%  ($696 / $1000 monthly)

  Local Device Today's Usage
    Tokens: 13,907,060 in / 5,109 out
    Cost:   $11.29
            Opus 4.6: $11.29 = (251×$5 + 5K×$25 + 13.2M×$0.50(cr) + 732K×$6.25(cw)) /M
    Daily:  9.0% of weekly quota

  Local Device Daily Usage (last 30 days)
    2026-02-12 (Thu)    $11.29  13.9M in / 5K out
    2026-02-11 (Wed)    $21.66  28.1M in / 21K out

  Quota Daily Usage (% of weekly quota, last 30 days)
    2026-02-12 (Thu)    9.0%  ██░░░░░░░░░░░░░░░░░░

  Weekly Usage
    02-09 09:59 ~ 02-16 09:59   57.0%  ███████████░░░░░░░░░  (extra: $696 / $1000)
```
</details>

## 自动化功能

### 5-hour Session 保活 & Quota Reset 触发

每 30 分钟定时器触发时（CST >= 08:00），自动发送 `claude --print --model haiku -p "hi"`：

- 保持 5 小时会话窗口不过期
- 若恰好处于 7 天周期重置后，该请求同时触发新周期

仅定时器触发，点击菜单和 CLI 不会发送该请求。

## 数据存储

菜单栏应用和 CLI 共享 `~/.claude-usage/`：

| 文件 | 内容 |
|------|------|
| `snapshots.json` | API 原始快照（utilization、resets_at 等） |
| `daily_usage.json` | 每天的配额使用百分比 |
| `local_usage.json` | 本机每天的 Token/费用/模型明细 |
| `weekly_usage.json` | 每个 7 天周期的起止时间和使用率 |

## 报告说明

| 区域 | 数据来源 | 说明 |
|------|---------|------|
| **Usage** | API 快照 | 7 天配额、5 小时会话、Extra Usage 的实时用量 |
| **Today** | 本地 JSONL | 本机当天的 Token 数、费用及按模型的计算公式 |
| **Local Daily** | local_usage.json | 本机最近 30 天每天的费用和 Token 汇总 |
| **Quota Daily** | 快照差值 | 每天消耗的配额百分比（API 数据，跨设备准确） |
| **Weekly** | weekly_usage.json | 每个 7 天周期的配额使用率和 Extra Usage |

## 计算逻辑

### Usage

- 数据源：`GET https://api.anthropic.com/api/oauth/usage`
- 认证：从 macOS Keychain 读取 OAuth token
- 返回 `seven_day`（7 天配额）、`five_hour`（5 小时会话）、`extra_usage`（超额用量）

### Quota Daily %

通过相邻天快照的 `seven_day.utilization` 差值计算每天消耗的配额百分比：

- 按 CST 日期分组，每天取最后一个快照
- 同一 cycle 内：`daily_pct[D] = snapshot[D].util - snapshot[D-1].util`
- 跨 cycle 边界（`resets_at` 变化，5 分钟容差）：`daily_pct[D] = snapshot[D].util`

### Local Device Cost

扫描 `~/.claude/projects/*/*.jsonl` 中的 assistant 消息，按模型定价计算：

| 模型 | Input | Output | Cache Read | Cache Write |
|------|-------|--------|------------|-------------|
| Opus 4.6/4.5 | $5/M | $25/M | $0.50/M | $6.25/M |
| Sonnet 4.5 | $3/M | $15/M | $0.30/M | $3.75/M |
| Haiku 4.5 | $1/M | $5/M | $0.10/M | $1.25/M |

Cache Read = Input × 0.1，Cache Write = Input × 1.25

## 定时采集（launchd，可选）

菜单栏应用内置 30 分钟定时器，通常不需要 launchd。如需独立定时采集：

```bash
cp com.user.claude-usage-collector.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.claude-usage-collector.plist
```
