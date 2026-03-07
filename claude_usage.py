#!/usr/bin/env python3
"""Claude Code Usage Tracker — prints weekly limits, daily tokens/cost, and 30-day history."""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", message="urllib3")

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Timezone ──────────────────────────────────────────────────────────────────
CST = timezone(timedelta(hours=8))

# ── Pricing ($/M tokens) ─────────────────────────────────────────────────────
PRICING = {
    "claude-opus-4-6":            {"input": 5.0,   "output": 25.0},
    "claude-opus-4-5-20251101":   {"input": 5.0,   "output": 25.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0,   "output": 15.0},
    "claude-haiku-4-5-20251001":  {"input": 1.0,   "output": 5.0},
}
# cache_read (cache hits) = input * 0.1, cache_write (5m creation) = input * 1.25

STORAGE_DIR = Path.home() / ".claude-usage"
STORAGE_FILE = STORAGE_DIR / "daily_usage.json"
SNAPSHOTS_FILE = STORAGE_DIR / "snapshots.json"
LOCAL_USAGE_FILE = STORAGE_DIR / "local_usage.json"
WEEKLY_USAGE_FILE = STORAGE_DIR / "weekly_usage.json"

# ── Data fetching ─────────────────────────────────────────────────────────────

def get_oauth_token() -> str:
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return json.loads(raw)["claudeAiOauth"]["accessToken"]
    except Exception:
        print(f"  ⚠  [{datetime.now(CST).strftime('%H:%M')}] Cannot read OAuth token from Keychain.")
        print("     Make sure Claude Code is installed and you are logged in.")
        sys.exit(1)


def fetch_usage_api(token: str) -> dict | str | None:
    """Returns dict on success, "unauthorized" on 401, None on other errors."""
    try:
        r = requests.get(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print(f"  ⚠  [{datetime.now(CST).strftime('%H:%M')}] Token expired (401)")
            return "unauthorized"
        print(f"  ⚠  [{datetime.now(CST).strftime('%H:%M')}] API request failed: {e}")
        return None
    except Exception as e:
        print(f"  ⚠  [{datetime.now(CST).strftime('%H:%M')}] API request failed: {e}")
        return None


def scan_local_sessions(start_date, end_date) -> list[dict]:
    """Scan all project JSONL files for messages in [start_date, end_date] (inclusive, UTC dates)."""
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    messages = []
    for jsonl_file in claude_dir.glob("*/*.jsonl"):
        try:
            with open(jsonl_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = obj.get("message")
                    if not msg or msg.get("role") != "assistant":
                        continue
                    usage = msg.get("usage")
                    ts_str = obj.get("timestamp")
                    if not usage or not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    ts_cst = ts.astimezone(CST)
                    d = ts_cst.date()
                    if d < start_date or d > end_date:
                        continue
                    messages.append({
                        "date": d.isoformat(),
                        "model": msg.get("model", "unknown"),
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    })
        except Exception:
            continue
    return messages


# ── Calculation ───────────────────────────────────────────────────────────────

def get_pricing(model: str) -> dict:
    if model in PRICING:
        return PRICING[model]
    # Fuzzy match by family
    for key, val in PRICING.items():
        if "opus" in key and "opus" in model:
            return val
        if "sonnet" in key and "sonnet" in model:
            return val
        if "haiku" in key and "haiku" in model:
            return val
    return {"input": 3.0, "output": 15.0}  # default to Sonnet pricing


def calc_cost(model: str, input_tokens: int, output_tokens: int,
              cache_read: int, cache_write: int) -> float:
    p = get_pricing(model)
    cost = (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read * p["input"] * 0.1
        + cache_write * p["input"] * 1.25
    ) / 1_000_000
    return cost


def get_model_display_name(model: str) -> str:
    if "opus" in model:
        return "Opus 4.6" if "4-6" in model else "Opus 4.5"
    if "sonnet" in model:
        return "Sonnet 4.5"
    if "haiku" in model:
        return "Haiku 4.5"
    return model


def aggregate_by_date(messages: list[dict]) -> dict:
    """Group messages by date → { date_str: { tokens_in, tokens_out, cost, by_model } }"""
    daily = {}
    for m in messages:
        d = m["date"]
        if d not in daily:
            daily[d] = {"tokens_in": 0, "tokens_out": 0, "cost": 0.0, "by_model": {}}
        daily[d]["tokens_in"] += m["input_tokens"] + m["cache_read"] + m["cache_write"]
        daily[d]["tokens_out"] += m["output_tokens"]
        cost = calc_cost(m["model"], m["input_tokens"], m["output_tokens"],
                         m["cache_read"], m["cache_write"])
        daily[d]["cost"] += cost
        # per-model breakdown
        family = get_model_display_name(m["model"])
        bm = daily[d]["by_model"]
        if family not in bm:
            bm[family] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
        bm[family]["input"] += m["input_tokens"]
        bm[family]["output"] += m["output_tokens"]
        bm[family]["cache_read"] += m["cache_read"]
        bm[family]["cache_write"] += m["cache_write"]
        bm[family]["cost"] += cost
    return daily


def calc_daily_deltas(snapshots: list) -> dict:
    """Calculate each day's daily % from snapshot deltas.

    Same cycle: daily_pct[D] = last_snapshot[D].util - last_snapshot[D-1].util
    Cross cycle boundary: daily_pct[D] = last_snapshot[D].util (new cycle's usage belongs to new day)
    """
    # Group by CST date, keep last snapshot per day
    by_date = {}
    for s in snapshots:
        if not s.get("seven_day"):
            continue
        dt = datetime.fromisoformat(s["ts"]).astimezone(CST)
        d = dt.date().isoformat()
        by_date[d] = s  # later snapshots overwrite earlier ones for same day

    dates = sorted(by_date.keys())
    result = {}
    for i, d in enumerate(dates):
        snap = by_date[d]
        util = snap["seven_day"]["utilization"]
        resets_at = snap["seven_day"]["resets_at"]

        if i == 0:
            # First snapshot, no previous day reference — skip (cumulative util != daily)
            continue
        else:
            prev_d = dates[i - 1]
            prev_snap = by_date[prev_d]
            prev_resets = prev_snap["seven_day"]["resets_at"]

            if _same_cycle(resets_at, prev_resets):
                # Same cycle — delta is this day's usage
                result[d] = max(util - prev_snap["seven_day"]["utilization"], 0)
            else:
                # Cycle switched — new cycle's utilization is this day's usage
                result[d] = util

    return result


def _same_cycle(resets_a: str, resets_b: str) -> bool:
    """Check if two resets_at timestamps belong to the same cycle (within 5 min tolerance)."""
    try:
        a = datetime.fromisoformat(resets_a)
        b = datetime.fromisoformat(resets_b)
        return abs((a - b).total_seconds()) < 300
    except ValueError:
        return resets_a == resets_b


# ── Snapshot Storage ─────────────────────────────────────────────────────────

def load_snapshots() -> list:
    if SNAPSHOTS_FILE.exists():
        try:
            return json.loads(SNAPSHOTS_FILE.read_text())
        except Exception:
            return []
    return []


def save_snapshots(data: list):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ── Daily Usage Storage ──────────────────────────────────────────────────────

def load_daily_data() -> dict:
    if STORAGE_FILE.exists():
        try:
            return json.loads(STORAGE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_daily_data(data: dict):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ── Local Device Usage Storage ───────────────────────────────────────────────

def load_local_usage() -> dict:
    if LOCAL_USAGE_FILE.exists():
        try:
            return json.loads(LOCAL_USAGE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_local_usage(data: dict):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_USAGE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ── Weekly Usage Storage ─────────────────────────────────────────────────────

def load_weekly_usage() -> list:
    if WEEKLY_USAGE_FILE.exists():
        try:
            return json.loads(WEEKLY_USAGE_FILE.read_text())
        except Exception:
            return []
    return []


def save_weekly_usage(data: list):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    WEEKLY_USAGE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ── Display ───────────────────────────────────────────────────────────────────

def format_time_left(resets_at: str) -> str:
    try:
        dt = datetime.fromisoformat(resets_at)
    except ValueError:
        return "?"
    now = datetime.now(CST)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "expired"
    total_secs = int(delta.total_seconds())
    days = total_secs // 86400
    hours = (total_secs % 86400) // 3600
    minutes = (total_secs % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes}m")
    return " ".join(parts) + " left" if parts else "<1m left"


def format_reset_time(resets_at: str) -> str:
    try:
        dt = datetime.fromisoformat(resets_at).astimezone(CST)
        return dt.strftime("%Y-%m-%d %H:%M CST")
    except ValueError:
        return resets_at


def format_bar(pct: float, width: int = 20) -> str:
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def format_number(n: int) -> str:
    return f"{n:,}"


def format_tokens_compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


# Model display name → pricing for cost formula
FAMILY_PRICING = {
    "Opus 4.6":   {"input": 5.0,   "output": 25.0},
    "Opus 4.5":   {"input": 5.0,   "output": 25.0},
    "Sonnet 4.5": {"input": 3.0,   "output": 15.0},
    "Haiku 4.5":  {"input": 1.0,   "output": 5.0},
}


def print_report(api_data: dict | None, snapshot_ts: str | None,
                 daily_agg: dict, daily_pcts: dict,
                 stored: dict, local_stored: dict, weekly_list: list,
                 today_str: str):
    now = datetime.now(CST)
    print(f"\n  Claude Code Usage — {now.strftime('%Y-%m-%d %H:%M')} (CST)\n")

    # ── Usage ──
    if api_data:
        print("  Usage")
        seven = api_data.get("seven_day") or {}
        five = api_data.get("five_hour") or {}
        extra = api_data.get("extra_usage") or {}

        if seven:
            util = seven["utilization"]
            reset = seven["resets_at"]
            print(f"    All models .......  {util:.1f}%  (resets at {format_reset_time(reset)}, {format_time_left(reset)})")
        if five:
            util = five["utilization"]
            reset = five["resets_at"]
            print(f"    Current session ..  {util:.1f}%  (resets at {format_reset_time(reset)}, {format_time_left(reset)})")
        if extra and extra.get("is_enabled"):
            used = extra.get("used_credits", 0)
            limit = extra.get("monthly_limit", 0)
            util = extra.get("utilization", 0)
            print(f"    Extra usage ......  {util:.1f}%  (${used:.0f} / ${limit:.0f} monthly)")
        if snapshot_ts:
            try:
                snap_dt = datetime.fromisoformat(snapshot_ts).astimezone(CST)
                print(f"    (snapshot: {snap_dt.strftime('%H:%M')} CST)")
            except ValueError:
                pass
        print()

    # ── Local Device Today's Usage ──
    today = daily_agg.get(today_str)
    if today:
        print("  Local Device Today's Usage")
        print(f"    Tokens: {format_number(today['tokens_in'])} in / {format_number(today['tokens_out'])} out")
        total_cost = today["cost"]
        print(f"    Cost:   ${total_cost:.2f}")
        for family in sorted(today["by_model"], key=lambda f: today["by_model"][f]["cost"], reverse=True):
            bm = today["by_model"][family]
            p = FAMILY_PRICING.get(family, {"input": 3.0, "output": 15.0})
            cr_rate = p["input"] * 0.1
            cw_rate = p["input"] * 1.25
            parts = [f'{format_tokens_compact(bm["input"])}×${p["input"]:.0f}']
            parts.append(f'{format_tokens_compact(bm["output"])}×${p["output"]:.0f}')
            if bm["cache_read"] > 0:
                parts.append(f'{format_tokens_compact(bm["cache_read"])}×${cr_rate:.2f}(cr)')
            if bm["cache_write"] > 0:
                parts.append(f'{format_tokens_compact(bm["cache_write"])}×${cw_rate:.2f}(cw)')
            formula = " + ".join(parts)
            print(f"            {family}: ${bm['cost']:.2f} = ({formula}) /M")
        pct = daily_pcts.get(today_str, 0)
        print(f"    Daily:  {pct:.1f}% of weekly quota")
        print()

    # ── Local Device Daily Usage ──
    local_days = sorted(
        [d for d in local_stored if isinstance(local_stored[d], dict) and local_stored[d].get("cost", 0) > 0],
        reverse=True,
    )[:30]
    if local_days:
        print("  Local Device Daily Usage (last 30 days)")
        for d in local_days:
            entry = local_stored[d]
            cost = entry.get("cost", 0)
            tokens_in = entry.get("tokens_in", 0)
            tokens_out = entry.get("tokens_out", 0)
            try:
                weekday = datetime.fromisoformat(d).strftime("%a")
            except ValueError:
                weekday = "???"
            cost_str = f"${cost:.2f}"
            print(f"    {d} ({weekday})  {cost_str:>8s}  {format_tokens_compact(tokens_in)} in / {format_tokens_compact(tokens_out)} out")
        print()

    # ── Quota Daily Usage (30 days) ──
    all_days = dict(stored)
    for d, pct in daily_pcts.items():
        if d not in all_days:
            all_days[d] = {}
        all_days[d]["pct"] = pct

    active_days = sorted(
        [d for d in all_days if isinstance(all_days[d], dict) and all_days[d].get("pct", 0) > 0],
        reverse=True,
    )[:30]
    if active_days:
        print("  Quota Daily Usage (% of weekly quota, last 30 days)")
        for d in active_days:
            entry = all_days[d]
            pct = entry.get("pct", 0)
            try:
                weekday = datetime.fromisoformat(d).strftime("%a")
            except ValueError:
                weekday = "???"
            bar = format_bar(pct)
            print(f"    {d} ({weekday})  {pct:5.1f}%  {bar}")
        print()

    # ── Weekly Usage ──
    if weekly_list:
        print("  Weekly Usage")
        for w in sorted(weekly_list, key=lambda x: x.get("cycle_end", ""), reverse=True)[:8]:
            try:
                start = datetime.fromisoformat(w["cycle_start"]).strftime("%m-%d %H:%M")
                end = datetime.fromisoformat(w["cycle_end"]).strftime("%m-%d %H:%M")
            except (ValueError, KeyError):
                continue
            util = w.get("seven_day", {}).get("utilization", 0)
            bar = format_bar(util)
            extra_info = ""
            ex = w.get("extra_usage")
            if ex and ex.get("is_enabled"):
                extra_info = f"  (extra: ${ex.get('used_credits', 0):.0f} / ${ex.get('monthly_limit', 0):.0f})"
            print(f"    {start} ~ {end}  {util:5.1f}%  {bar}{extra_info}")
        print()

    print(f"  Data: {STORAGE_DIR}")
    print(f"    snapshots.json / daily_usage.json / local_usage.json / weekly_usage.json\n")


# ── Local Usage Update ───────────────────────────────────────────────────────

def update_local_usage(now_cst: datetime):
    """Scan local JSONL for today and save to local_usage.json."""
    today_str = now_cst.date().isoformat()
    messages = scan_local_sessions(now_cst.date(), now_cst.date())
    daily_agg = aggregate_by_date(messages)
    today_agg = daily_agg.get(today_str)
    if not today_agg:
        return

    local_stored = load_local_usage()
    by_model = {}
    for family, bm in today_agg["by_model"].items():
        p = FAMILY_PRICING.get(family, {"input": 3.0, "output": 15.0})
        by_model[family] = {
            "input": bm["input"],
            "output": bm["output"],
            "cache_read": bm["cache_read"],
            "cache_write": bm["cache_write"],
            "cost": round(bm["cost"], 4),
            "price_per_mtok": {
                "input": p["input"],
                "output": p["output"],
                "cache_read": p["input"] * 0.1,
                "cache_write": p["input"] * 1.25,
            },
        }
    local_stored[today_str] = {
        "updated_at": now_cst.isoformat(),
        "tokens_in": today_agg["tokens_in"],
        "tokens_out": today_agg["tokens_out"],
        "cost": round(today_agg["cost"], 4),
        "by_model": by_model,
    }
    save_local_usage(local_stored)


# ── Collect Mode ─────────────────────────────────────────────────────────────

def collect(keep_alive: bool = True):
    """Collect mode: call API → save snapshot → compute daily deltas → update daily_usage."""
    token = get_oauth_token()
    api_data = fetch_usage_api(token)
    if api_data == "unauthorized":
        # Only refresh token on 401, not on timeout/network errors
        try:
            subprocess.run(
                ["claude", "--print", "--model", "haiku", "-p", "hi"],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass
        token = get_oauth_token()
        api_data = fetch_usage_api(token)
    if not api_data or api_data == "unauthorized":
        return

    now_cst = datetime.now(CST)

    snapshots = load_snapshots()

    # Keep 5-hour session alive (every collect() from 08:00 onwards)
    if keep_alive and now_cst.hour >= 8:
        try:
            subprocess.run(
                ["claude", "--print", "--model", "haiku", "-p", "hi"],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass

    # 3. Append snapshot
    snapshots.append({
        "ts": now_cst.isoformat(),
        "seven_day": api_data.get("seven_day"),
        "five_hour": api_data.get("five_hour"),
        "extra_usage": api_data.get("extra_usage"),
    })

    # 3. Save snapshots
    save_snapshots(snapshots)

    # 4. Compute daily deltas from snapshots, merge into daily_usage.json
    daily_pcts = calc_daily_deltas(snapshots)
    stored = load_daily_data()
    for d, pct in daily_pcts.items():
        stored[d] = {"updated_at": now_cst.isoformat(), "pct": round(pct, 2)}
    save_daily_data(stored)

    # 5. Scan local JSONL and update local_usage.json
    update_local_usage(now_cst)

    # 6. Update weekly_usage.json
    seven = api_data.get("seven_day") or {}
    if seven:
        try:
            resets_at_dt = datetime.fromisoformat(seven["resets_at"])
            cycle_end = resets_at_dt.astimezone(CST).isoformat()
            cycle_start = (resets_at_dt - timedelta(days=7)).astimezone(CST).isoformat()
        except ValueError:
            cycle_start = cycle_end = None

        if cycle_start:
            weekly_list = load_weekly_usage()
            extra = api_data.get("extra_usage") or {}
            entry = {
                "updated_at": now_cst.isoformat(),
                "cycle_start": cycle_start,
                "cycle_end": cycle_end,
                "seven_day": {
                    "utilization": seven.get("utilization"),
                },
                "extra_usage": {
                    "is_enabled": extra.get("is_enabled", False),
                    "used_credits": extra.get("used_credits", 0),
                    "monthly_limit": extra.get("monthly_limit", 0),
                    "utilization": extra.get("utilization", 0),
                } if extra.get("is_enabled") else None,
            }

            # Update existing cycle or append new
            matched = False
            for i, w in enumerate(weekly_list):
                if w.get("cycle_end") and _same_cycle(w["cycle_end"], cycle_end):
                    weekly_list[i] = entry
                    matched = True
                    break
            if not matched:
                weekly_list.append(entry)

            save_weekly_usage(weekly_list)


# ── Report Mode ──────────────────────────────────────────────────────────────

def report():
    """Report mode: collect fresh snapshot, then display usage report."""
    # 0. Collect a fresh snapshot first so data is up-to-date
    collect(keep_alive=False)

    now_cst = datetime.now(CST)
    today_str = now_cst.date().isoformat()

    # 1. Read latest snapshot for Usage display
    snapshots = load_snapshots()
    api_data = None
    snapshot_ts = None
    if snapshots:
        latest = snapshots[-1]
        snapshot_ts = latest.get("ts")
        api_data = {
            "seven_day": latest.get("seven_day"),
            "five_hour": latest.get("five_hour"),
            "extra_usage": latest.get("extra_usage"),
        }

    # 2. Scan local JSONL for today's token/cost details
    messages = scan_local_sessions(now_cst.date(), now_cst.date())
    daily_agg = aggregate_by_date(messages)

    # 3. Read stored data
    stored = load_daily_data()
    daily_pcts = calc_daily_deltas(snapshots)
    local_stored = load_local_usage()
    weekly_list = load_weekly_usage()

    # 4. Display
    print_report(api_data, snapshot_ts, daily_agg, daily_pcts, stored,
                 local_stored, weekly_list, today_str)


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Claude Code Usage Tracker")
    parser.add_argument("--collect", action="store_true", help="Collect mode: call API and save snapshot")
    args = parser.parse_args()

    if args.collect:
        collect(keep_alive=False)
    else:
        report()
