#!/usr/bin/env python3
"""Claude Code Usage — macOS menu bar app (rumps)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import objc
import rumps
from AppKit import NSAttributedString, NSFont, NSFontAttributeName
from Foundation import NSDictionary, NSObject
from PyObjCTools import AppHelper

from claude_usage import (
    CST,
    FAMILY_PRICING,
    aggregate_by_date,
    calc_daily_deltas,
    collect,
    format_bar,
    format_time_left,
    format_tokens_compact,
    load_daily_data,
    load_local_usage,
    load_snapshots,
    load_weekly_usage,
    scan_local_sessions,
)

REFRESH_INTERVAL = 30 * 60  # 30 minutes
MONO_FONT = NSFont.monospacedSystemFontOfSize_weight_(13, 0)
MONO_ATTRS = NSDictionary.dictionaryWithObject_forKey_(MONO_FONT, NSFontAttributeName)


def _noop(_):
    """No-op callback so macOS renders the menu item as enabled (non-grey)."""
    pass


def _set_mono(item: rumps.MenuItem, text: str):
    """Set menu item title with monospaced font via NSAttributedString."""
    attr_str = NSAttributedString.alloc().initWithString_attributes_(text, MONO_ATTRS)
    item._menuitem.setAttributedTitle_(attr_str)


def _mono_info_item(title: str) -> rumps.MenuItem:
    """Create an enabled menu item with monospaced font."""
    item = rumps.MenuItem(title, callback=_noop)
    _set_mono(item, title)
    return item


class _MenuDelegate(NSObject):
    """NSMenuDelegate that refreshes data when the menu is opened."""

    app = None

    def menuWillOpen_(self, menu):
        if self.app:
            self.app._do_collect_and_update()


class ClaudeUsageApp(rumps.App):
    def __init__(self):
        super().__init__("☁ --.--%", quit_button=None)

        # Static menu items (will update titles in-place)
        self.mi_all = _mono_info_item("All models  --.--%")
        self.mi_session = _mono_info_item("Session     --.--%")
        self.mi_extra = _mono_info_item("Extra usage  ---")

        self.mi_today_header = _mono_info_item("Today  ---")
        self.mi_today_tokens = _mono_info_item("  ---")
        self.mi_today_models = rumps.MenuItem("  Cost Detail")
        _set_mono(self.mi_today_models, "  Cost Detail")

        self.mi_local_daily = rumps.MenuItem("Local Daily")
        _set_mono(self.mi_local_daily, "Local Daily")
        self.mi_quota_daily = rumps.MenuItem("Quota Daily")
        _set_mono(self.mi_quota_daily, "Quota Daily")
        self.mi_weekly = rumps.MenuItem("Weekly")
        _set_mono(self.mi_weekly, "Weekly")

        self.mi_updated = _mono_info_item("Updated --:--")
        self.mi_quit = rumps.MenuItem("Quit", callback=rumps.quit_application)
        _set_mono(self.mi_quit, "Quit")

        self.mi_usage_url = rumps.MenuItem("Usage Settings", callback=self._open_usage)
        _set_mono(self.mi_usage_url, "Usage Settings")

        self.menu = [
            self.mi_all,
            self.mi_session,
            self.mi_extra,
            None,  # separator
            self.mi_today_header,
            self.mi_today_tokens,
            self.mi_today_models,
            None,
            self.mi_local_daily,
            self.mi_quota_daily,
            self.mi_weekly,
            None,
            self.mi_updated,
            self.mi_usage_url,
            None,
            self.mi_quit,
        ]

        # Timer for periodic refresh
        self.timer = rumps.Timer(self._timer_tick, REFRESH_INTERVAL)
        self.timer.start()

        # Load cached data immediately, schedule first collect after 2s
        self._update_from_cache()
        rumps.Timer(self._first_collect, 2).start()

        # Set NSMenu delegate so clicking the icon refreshes from cache
        def _setup_delegate(timer):
            timer.stop()
            ns_menu = self._nsapp.nsstatusitem.menu()
            self._menu_delegate = _MenuDelegate.alloc().init()
            self._menu_delegate.app = self
            ns_menu.setDelegate_(self._menu_delegate)

        rumps.Timer(_setup_delegate, 0.5).start()

    # ── Callbacks ─────────────────────────────────────────────────────────

    def _first_collect(self, _sender):
        _sender.stop()
        self._do_collect_and_update()

    def _timer_tick(self, _sender):
        self._do_collect_and_update(keep_alive=True)

    def _open_usage(self, _sender):
        import webbrowser
        webbrowser.open("https://claude.ai/settings/usage")

    # ── Data collection (background thread) ───────────────────────────────

    def _do_collect_and_update(self, keep_alive=False):
        _set_mono(self.mi_updated, "Refreshing...")

        def _work():
            try:
                collect(keep_alive=keep_alive)
            except SystemExit:
                pass  # get_oauth_token() calls sys.exit(1) on failure
            except Exception:
                pass
            # Dispatch to main thread via performSelectorOnMainThread —
            # fires in NSRunLoopCommonModes, so it works even while menu is open
            AppHelper.callAfter(self._update_from_cache)

        threading.Thread(target=_work, daemon=True).start()

    # ── Read cache and rebuild menu ───────────────────────────────────────

    def _update_from_cache(self):
        now = datetime.now(CST)
        today_str = now.date().isoformat()

        snapshots = load_snapshots()
        local_stored = load_local_usage()
        daily_stored = load_daily_data()
        weekly_list = load_weekly_usage()
        daily_pcts = calc_daily_deltas(snapshots)

        # Latest snapshot for quota info
        api = {}
        if snapshots:
            latest = snapshots[-1]
            api = {
                "seven_day": latest.get("seven_day"),
                "five_hour": latest.get("five_hour"),
                "extra_usage": latest.get("extra_usage"),
            }

        seven = api.get("seven_day") or {}
        five = api.get("five_hour") or {}
        extra = api.get("extra_usage") or {}

        seven_util = seven.get("utilization")
        five_util = five.get("utilization")

        # ── Title bar (prefer session) ──
        if five_util is not None:
            self.title = f"☁ {five_util:.1f}%"
        elif seven_util is not None:
            self.title = f"☁ {seven_util:.1f}%"
        else:
            self.title = "☁ --.--%"

        # ── Quota rows ──
        if seven_util is not None:
            time_str = f"  {format_time_left(seven['resets_at'])}" if seven.get("resets_at") else ""
            _set_mono(self.mi_all, f"All models  {seven_util:5.1f}%{time_str}")

        if five_util is not None:
            time_str = f"  {format_time_left(five['resets_at'])}" if five.get("resets_at") else ""
            _set_mono(self.mi_session, f"Session     {five_util:5.1f}%{time_str}")
        else:
            _set_mono(self.mi_session, "Session      idle")

        if extra and extra.get("is_enabled"):
            used = extra.get("used_credits", 0)
            limit = extra.get("monthly_limit", 0)
            util = extra.get("utilization", 0)
            _set_mono(self.mi_extra, f"Extra usage {util:5.1f}%  ${used:.0f}/${limit:.0f}")
        else:
            _set_mono(self.mi_extra, "Extra usage  N/A")

        # ── Today ──
        messages = scan_local_sessions(now.date(), now.date())
        daily_agg = aggregate_by_date(messages)
        today = daily_agg.get(today_str)
        today_pct = daily_pcts.get(today_str, 0)

        if today:
            tokens_in = format_tokens_compact(today["tokens_in"])
            tokens_out = format_tokens_compact(today["tokens_out"])
            _set_mono(self.mi_today_header, f"Today ${today['cost']:.2f} · {today_pct:.1f}% quota")
            _set_mono(self.mi_today_tokens, f"  {tokens_in} in / {tokens_out} out")

            # Model breakdown submenu
            self._rebuild_submenu(self.mi_today_models, self._build_model_breakdown(today))
        else:
            _set_mono(self.mi_today_header, "Today  ---")
            _set_mono(self.mi_today_tokens, "  ---")
            self._rebuild_submenu(self.mi_today_models, [_mono_info_item("---")])

        # ── Local Daily submenu ──
        self._rebuild_submenu(self.mi_local_daily, self._build_local_daily(local_stored, daily_agg, today_str))

        # ── Quota Daily submenu ──
        self._rebuild_submenu(self.mi_quota_daily, self._build_quota_daily(daily_stored, daily_pcts))

        # ── Weekly submenu ──
        self._rebuild_submenu(self.mi_weekly, self._build_weekly(weekly_list))

        # ── Updated (show last snapshot time, not current time) ──
        if snapshots:
            try:
                snap_dt = datetime.fromisoformat(snapshots[-1]["ts"]).astimezone(CST)
                _set_mono(self.mi_updated, f"Updated {snap_dt.strftime('%H:%M')} CST")
            except (ValueError, KeyError):
                _set_mono(self.mi_updated, "Updated --:--")
        else:
            _set_mono(self.mi_updated, "Updated --:--")

    # ── Submenu builders ──────────────────────────────────────────────────

    @staticmethod
    def _rebuild_submenu(parent: rumps.MenuItem, items: list[rumps.MenuItem]):
        if parent._menu is not None:
            parent.clear()
        for item in items:
            parent.add(item)

    @staticmethod
    def _build_model_breakdown(today: dict) -> list[rumps.MenuItem]:
        models_sorted = sorted(today["by_model"].items(), key=lambda x: x[1]["cost"], reverse=True)
        if not models_sorted:
            return [_mono_info_item("---")]
        items = []
        for family, bm in models_sorted:
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
            items.append(_mono_info_item(f"{family}: ${bm['cost']:.2f} = ({formula}) /M"))
        return items

    @staticmethod
    def _build_local_daily(local_stored: dict, daily_agg: dict, today_str: str) -> list[rumps.MenuItem]:
        # Merge today's live data into local_stored for display
        merged = dict(local_stored)
        today = daily_agg.get(today_str)
        if today:
            merged[today_str] = {
                "cost": today["cost"],
                "tokens_in": today["tokens_in"],
                "tokens_out": today["tokens_out"],
            }

        days = sorted(
            [d for d in merged if isinstance(merged[d], dict) and merged[d].get("cost", 0) > 0],
            reverse=True,
        )[:30]
        items = []
        for d in days:
            entry = merged[d]
            cost = entry.get("cost", 0)
            tokens_in = entry.get("tokens_in", 0)
            tokens_out = entry.get("tokens_out", 0)
            try:
                weekday = datetime.fromisoformat(d).strftime("%a")
            except ValueError:
                weekday = "???"
            items.append(_mono_info_item(
                f"{d[5:]} {weekday}  ${cost:.2f}  {format_tokens_compact(tokens_in)} in / {format_tokens_compact(tokens_out)} out"
            ))
        return items or [_mono_info_item("No data")]

    @staticmethod
    def _build_quota_daily(daily_stored: dict, daily_pcts: dict) -> list[rumps.MenuItem]:
        all_days = dict(daily_stored)
        for d, pct in daily_pcts.items():
            if d not in all_days:
                all_days[d] = {}
            all_days[d]["pct"] = pct

        days = sorted(
            [d for d in all_days if isinstance(all_days[d], dict) and all_days[d].get("pct") is not None],
            reverse=True,
        )[:30]
        items = []
        for d in days:
            pct = all_days[d].get("pct", 0)
            try:
                weekday = datetime.fromisoformat(d).strftime("%a")
            except ValueError:
                weekday = "???"
            bar = format_bar(pct, width=10)
            items.append(_mono_info_item(f"{d[5:]} {weekday}  {pct:5.1f}%  {bar}"))
        return items or [_mono_info_item("No data")]

    @staticmethod
    def _build_weekly(weekly_list: list) -> list[rumps.MenuItem]:
        weeks = sorted(weekly_list, key=lambda x: x.get("cycle_end", ""), reverse=True)[:8]
        items = []
        for w in weeks:
            try:
                start = datetime.fromisoformat(w["cycle_start"]).strftime("%m-%d %H:%M")
                end = datetime.fromisoformat(w["cycle_end"]).strftime("%m-%d %H:%M")
            except (ValueError, KeyError):
                continue
            util = w.get("seven_day", {}).get("utilization", 0)
            extra_info = ""
            ex = w.get("extra_usage")
            if ex and ex.get("is_enabled"):
                extra_info = f"  (extra: ${ex.get('used_credits', 0):.0f}/${ex.get('monthly_limit', 0):.0f})"
            items.append(_mono_info_item(f"{start} ~ {end}  {util:5.1f}%{extra_info}"))
        return items or [_mono_info_item("No data")]


if __name__ == "__main__":
    ClaudeUsageApp().run()
