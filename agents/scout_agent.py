#!/usr/bin/env python3
"""
Scout Agent — Bid Intelligence & Monitoring
=============================================
Monitors CC and BC platforms, scores bids, generates daily briefings,
watches Gmail for bid invitations, and deduplicates the pipeline.

Phase 2: Full implementation per CLAUDE.md spec.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, date, timedelta
from difflib import get_close_matches, SequenceMatcher
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BIDS_FILE = BASE_DIR / "data" / "memory" / "active_bids.json"
GC_DIR = BASE_DIR / "data" / "memory" / "gc"
FACILITY_DIR = BASE_DIR / "data" / "memory" / "facility_types"
PROJECTS_DIR = BASE_DIR / "data" / "projects"
SCRIPTS_DIR = BASE_DIR / "scripts"


class ScoutAgent:
    """
    Monitors CC and BC platforms, scores bids, sends daily briefings.
    Runs on a schedule and proactively pushes to Carol Core.
    """

    def __init__(self, pipeline_state=None):
        self.pipeline = pipeline_state
        self._gc_cache = None
        self._facility_cache = None

    # ==================================================================
    # SCRAPING — run both CC + BC scrapers, dedup, return structured result
    # ==================================================================

    def run_scrapers(self) -> str:
        """
        Run scrape_cc_inbox.py and scrape_bc_inbox.py as subprocesses.
        Deduplicate after merge. Return summary with counts.
        """
        before_bids = self._load_bids()
        before_count = len(before_bids)
        results = []

        # --- CC scraper ---
        cc_script = SCRIPTS_DIR / "scrape_cc_inbox.py"
        cc_ok = False
        if cc_script.exists():
            try:
                r = subprocess.run(
                    [sys.executable, str(cc_script)],
                    capture_output=True, text=True, timeout=120,
                )
                cc_ok = r.returncode == 0
                results.append(f"CC scraper: {'OK' if cc_ok else 'FAILED'}")
                for line in r.stdout.splitlines():
                    if "SCRAPED" in line:
                        results.append(f"  {line.strip()}")
                    elif "Saved:" in line:
                        results.append(f"  {line.strip()}")
                if not cc_ok and r.stderr:
                    results.append(f"  Error: {r.stderr.strip()[:150]}")
            except subprocess.TimeoutExpired:
                results.append("CC scraper: TIMEOUT (>120s)")
            except Exception as e:
                results.append(f"CC scraper: ERROR — {e}")
        else:
            results.append("CC scraper: script not found")

        # --- BC scraper ---
        bc_script = SCRIPTS_DIR / "scrape_bc_inbox.py"
        bc_ok = False
        if bc_script.exists():
            try:
                r = subprocess.run(
                    [sys.executable, str(bc_script)],
                    capture_output=True, text=True, timeout=120,
                )
                bc_ok = r.returncode == 0
                results.append(f"BC scraper: {'OK' if bc_ok else 'FAILED'}")
                for line in r.stdout.splitlines():
                    if "SCRAPED" in line:
                        results.append(f"  {line.strip()}")
                    elif "Saved:" in line:
                        results.append(f"  {line.strip()}")
                if not bc_ok and r.stderr:
                    results.append(f"  Error: {r.stderr.strip()[:150]}")
            except subprocess.TimeoutExpired:
                results.append("BC scraper: TIMEOUT (>120s)")
            except Exception as e:
                results.append(f"BC scraper: ERROR — {e}")
        else:
            results.append("BC scraper: script not found")

        # --- Dedup after both scrapers have written ---
        dedup_removed = self.dedup_bids()

        # --- Compute diff ---
        after_bids = self._load_bids()
        after_count = len(after_bids)

        # Figure out new bids (names that weren't in before set)
        before_names = {b["project_name"].lower()[:40] for b in before_bids}
        new_bids = [
            b for b in after_bids
            if b["project_name"].lower()[:40] not in before_names
        ]

        # Expired (in before but not after)
        after_names = {b["project_name"].lower()[:40] for b in after_bids}
        expired = [
            b for b in before_bids
            if b["project_name"].lower()[:40] not in after_names
        ]

        bc = sum(1 for b in after_bids if b.get("source") == "buildingconnected")
        cc = sum(1 for b in after_bids if b.get("source") == "constructconnect")

        results.append(f"\nDuplicates removed: {dedup_removed}")
        if new_bids:
            results.append(f"New bids found: {len(new_bids)}")
            for nb in new_bids[:5]:
                src = "(CC)" if nb.get("source") == "constructconnect" else "(BC)"
                results.append(f"  + {nb['project_name'][:50]} {src}")
            if len(new_bids) > 5:
                results.append(f"  ... and {len(new_bids) - 5} more")
        if expired:
            results.append(f"Expired/removed: {len(expired)}")

        results.append(f"\nPipeline: {after_count} total ({bc} BC + {cc} CC)")

        return "SCRAPE RESULTS:\n" + "\n".join(results)

    # ==================================================================
    # DEDUPLICATION — remove duplicate bids from active_bids.json
    # ==================================================================

    def dedup_bids(self) -> int:
        """
        Remove duplicate bids from active_bids.json.
        Two bids are duplicates if:
        - Same GC (fuzzy) AND same project name (fuzzy match > 0.75)
        - OR exact same (name[:40], gc[:30]) key
        Keeps the bid with more data (size, contact, etc.)
        Returns count of removed duplicates.
        """
        bids = self._load_bids()
        if len(bids) < 2:
            return 0

        seen = {}  # key -> bid index
        remove_indices = set()

        for i, b in enumerate(bids):
            # Primary key: normalized name + gc
            name_norm = re.sub(r'[^a-z0-9]', '', b["project_name"].lower())[:40]
            gc_norm = re.sub(r'[^a-z0-9]', '', b.get("gc", "").lower())[:25]
            key = (name_norm, gc_norm)

            if key in seen:
                # Duplicate found — keep the one with more data
                existing_idx = seen[key]
                existing = bids[existing_idx]
                if self._bid_richness(b) > self._bid_richness(existing):
                    remove_indices.add(existing_idx)
                    seen[key] = i
                else:
                    remove_indices.add(i)
            else:
                # Check fuzzy match against all seen keys
                matched = False
                for skey, sidx in list(seen.items()):
                    s_name, s_gc = skey
                    # Same GC (fuzzy) AND similar project name
                    if (SequenceMatcher(None, gc_norm, s_gc).ratio() > 0.8 and
                            SequenceMatcher(None, name_norm, s_name).ratio() > 0.75):
                        existing = bids[sidx]
                        if self._bid_richness(b) > self._bid_richness(existing):
                            remove_indices.add(sidx)
                            seen[skey] = i
                        else:
                            remove_indices.add(i)
                        matched = True
                        break
                if not matched:
                    seen[key] = i

        if remove_indices:
            deduped = [b for i, b in enumerate(bids) if i not in remove_indices]
            self._save_bids(deduped)
            return len(remove_indices)
        return 0

    @staticmethod
    def _bid_richness(bid: dict) -> int:
        """Score how much data a bid has — used to pick winner in dedup."""
        score = 0
        if bid.get("size_sf"):
            score += 3
        if bid.get("gc_contact"):
            score += 2
        if bid.get("trade"):
            score += 1
        if bid.get("portal_url"):
            score += 1
        if bid.get("distance_miles") is not None:
            score += 1
        return score

    # ==================================================================
    # DAILY BRIEFING — full format with Today / Week / Upcoming + Top 3
    # ==================================================================

    def get_briefing(self, days_ahead: int = 0) -> str:
        """Generate a bid briefing. days_ahead: 0=today, 1=tomorrow, 7=this week."""
        bids = self._load_active_bids()
        today = date.today()

        if days_ahead == 0:
            return self._full_daily_briefing(bids, today)
        elif days_ahead == 1:
            target = today + timedelta(days=1)
            return self._bids_for_date(bids, target, "TOMORROW")
        else:
            return self._bids_for_week(bids, today)

    def get_daily_briefing(self) -> str:
        """Alias for get_briefing(0) — called by run_carol.py --daily-briefing."""
        return self.get_briefing(days_ahead=0)

    def _full_daily_briefing(self, bids: list, today: date) -> str:
        """Full morning briefing: Today + This Week + Upcoming + Top 3 Deep Dives."""
        today_str = f"{today.month}/{today.day}/{today.year}"

        # Categorize
        today_bids = []
        week_bids = []
        upcoming_bids = []

        week_dates = set()
        for i in range(1, 7):
            d = today + timedelta(days=i)
            week_dates.add(f"{d.month}/{d.day}/{d.year}")

        for b in bids:
            dd = b.get("due_date", "")
            if dd == today_str:
                today_bids.append(b)
            elif dd in week_dates:
                week_bids.append(b)
            else:
                upcoming_bids.append(b)

        # Sort each by distance
        today_bids.sort(key=lambda b: b.get("distance_miles") or 9999)
        week_bids.sort(key=lambda b: b.get("distance_miles") or 9999)
        upcoming_bids.sort(key=lambda b: b.get("distance_miles") or 9999)

        lines = [
            f"DAILY BID BRIEFING — {today.strftime('%A, %B %d, %Y')}",
            f"Carolina Commercial Finishes",
            "=" * 55,
            "",
        ]

        # --- DUE TODAY ---
        lines.append(f"DUE TODAY: {len(today_bids)} bids")
        lines.append("-" * 55)
        if today_bids:
            for i, b in enumerate(today_bids, 1):
                lines.append(self._format_bid_line(i, b))
        else:
            lines.append("  No bids due today.")
        lines.append("")

        # --- DUE THIS WEEK ---
        lines.append(f"DUE THIS WEEK: {len(week_bids)} bids")
        lines.append("-" * 55)
        if week_bids:
            for i, b in enumerate(week_bids, 1):
                lines.append(self._format_bid_line(i, b))
        else:
            lines.append("  No bids due this week.")
        lines.append("")

        # --- UPCOMING ---
        lines.append(f"UPCOMING: {len(upcoming_bids)} bids")
        lines.append("-" * 55)
        if upcoming_bids:
            for i, b in enumerate(upcoming_bids[:15], 1):
                lines.append(self._format_bid_line(i, b))
            if len(upcoming_bids) > 15:
                lines.append(f"  ... and {len(upcoming_bids) - 15} more")
        lines.append("")

        # --- TOP 3 DEEP DIVES (nearest due today) ---
        dive_pool = today_bids if today_bids else week_bids
        if dive_pool:
            # Score and sort by score descending
            scored = [(b, self.score_bid(b)) for b in dive_pool]
            scored.sort(key=lambda x: x[1]["score"], reverse=True)
            top3 = scored[:3]

            lines.append("=" * 55)
            lines.append("TOP RECOMMENDATIONS — CAROL'S PICKS")
            lines.append("=" * 55)

            for rank, (b, score) in enumerate(top3, 1):
                lines.append(self._format_deep_dive(rank, b, score))
            lines.append("")

        # --- Summary ---
        bc = sum(1 for b in bids if b.get("source") == "buildingconnected")
        cc = sum(1 for b in bids if b.get("source") == "constructconnect")
        lines.append(f"PIPELINE: {len(bids)} active ({bc} BC + {cc} CC)")
        lines.append("=" * 55)

        return "\n".join(lines)

    def _bids_for_date(self, bids: list, target: date, label: str) -> str:
        """Bids for a specific date."""
        target_str = f"{target.month}/{target.day}/{target.year}"
        matching = [b for b in bids if b.get("due_date", "") == target_str]
        matching.sort(key=lambda b: b.get("distance_miles") or 9999)

        if not matching:
            return f"No bids due {label} ({target.strftime('%A, %B %d')})."

        lines = [f"DUE {label} ({target.strftime('%A, %B %d')}): {len(matching)} bids\n"]
        for i, b in enumerate(matching, 1):
            lines.append(self._format_bid_line(i, b))

        bc = sum(1 for b in bids if b.get("source") == "buildingconnected")
        cc = sum(1 for b in bids if b.get("source") == "constructconnect")
        lines.append(f"\nPipeline: {len(bids)} total ({bc} BC + {cc} CC)")
        return "\n".join(lines)

    def _bids_for_week(self, bids: list, today: date) -> str:
        """All bids due in the next 7 days."""
        week_dates = set()
        for i in range(8):
            d = today + timedelta(days=i)
            week_dates.add(f"{d.month}/{d.day}/{d.year}")

        matching = [b for b in bids if b.get("due_date", "") in week_dates]
        matching.sort(key=lambda b: (b.get("due_date", ""), b.get("distance_miles") or 9999))

        if not matching:
            return f"No bids due this week ({today.strftime('%b %d')} - {(today + timedelta(days=7)).strftime('%b %d')})."

        lines = [
            f"DUE THIS WEEK ({today.strftime('%b %d')} - "
            f"{(today + timedelta(days=7)).strftime('%b %d')}): {len(matching)} bids\n"
        ]

        current_date = ""
        for i, b in enumerate(matching, 1):
            dd = b.get("due_date", "")
            if dd != current_date:
                current_date = dd
                try:
                    dt = datetime.strptime(dd, "%m/%d/%Y")
                    lines.append(f"\n  {dt.strftime('%A %m/%d')}:")
                except ValueError:
                    lines.append(f"\n  {dd}:")
            lines.append(self._format_bid_line(i, b))

        return "\n".join(lines)

    def _format_bid_line(self, num: int, bid: dict) -> str:
        """Format a single bid as a one-liner for briefings."""
        d = bid.get("distance_miles")
        dist = f"{d:.0f}mi" if d and d < 9000 else "?mi"
        source = "(CC)" if bid.get("source") == "constructconnect" else "(BC)"
        size = f" | {bid['size_sf']}SF" if bid.get("size_sf") else ""
        score = self.score_bid(bid)
        rec = score["recommendation"]
        gc = bid.get("gc", "?")[:25]

        return (
            f"  {num:>2d}. {bid['project_name'][:45]:<45s} {source}  "
            f"{dist:>6s}  {gc:<25s}{size}  [{rec}]"
        )

    def _format_deep_dive(self, rank: int, bid: dict, score: dict) -> str:
        """Format a deep-dive section for a top bid."""
        d = bid.get("distance_miles")
        dist = f"{d:.0f} mi" if d and d < 9000 else "? mi"
        source = "(CC)" if bid.get("source") == "constructconnect" else "(BC)"
        ftype = self._classify_facility(bid.get("project_name", ""))
        gc_name = bid.get("gc", "Unknown")
        gc_data = self._lookup_gc(gc_name)

        lines = [
            f"\n  {rank}. {bid['project_name'].upper()} {source}",
            f"     {bid.get('city', '')}, {bid.get('state', '')[:2]} | {dist} | Due: {bid.get('due_date', '?')}",
            f"     GC: {gc_name} | Contact: {bid.get('gc_contact', 'TBD')}",
        ]

        if bid.get("size_sf"):
            lines.append(f"     Size: {bid['size_sf']} SF")
        lines.append(f"     Type: {ftype.replace('_', ' ').title()}")

        # GC history
        if gc_data:
            wr = gc_data.get("win_rate", 0)
            lines.append(
                f"     GC History: {gc_data.get('projects_won', 0)}/"
                f"{gc_data.get('projects_bid', 0)} wins ({wr*100:.0f}%)"
            )
            if gc_data.get("pricing_notes"):
                lines.append(f"     Notes: {gc_data['pricing_notes'][:80]}")
        else:
            lines.append(f"     GC History: New GC — no prior data")

        # Documents available?
        has_docs = self._has_documents(bid)
        lines.append(f"     Docs: {'Available' if has_docs else 'Not downloaded'}")

        # Score & recommendation
        lines.append(
            f"     CAROL: {score['recommendation']} (score {score['score']}/51) "
            f"— {score['reason']}"
        )

        return "\n".join(lines)

    # ==================================================================
    # BID SCORING — multi-factor scoring per CLAUDE.md spec
    # ==================================================================

    def score_bid(self, bid: dict) -> dict:
        """
        Score a bid on multiple factors. Returns dict with score, recommendation, reason.

        Factors (max 51 pts):
        - Distance: <30mi=10, <60mi=8, <100mi=5, >100mi=2
        - GC win rate: >30%=10, >15%=7, >0%=5, unknown=3
        - Facility type: known=8, unknown=4
        - Avg contract value: >$50k=8, >$20k=5, <$20k=2
        - Due date: >7d=10, 3-7d=6, <3d=2
        - Has documents: yes=5, no=0
        """
        points = 0
        reasons = []

        # 1. Distance (max 10 pts)
        dist = bid.get("distance_miles") or 999
        if dist < 30:
            points += 10
            reasons.append(f"close ({dist:.0f}mi)")
        elif dist < 60:
            points += 8
            reasons.append(f"moderate ({dist:.0f}mi)")
        elif dist < 100:
            points += 5
        else:
            points += 2
            if dist > 200:
                reasons.append(f"far ({dist:.0f}mi)")

        # 2. GC win rate (max 10 pts)
        gc_data = self._lookup_gc(bid.get("gc", ""))
        if gc_data:
            wr = gc_data.get("win_rate", 0)
            bids_count = gc_data.get("projects_bid", 0)
            if wr >= 0.3:
                points += 10
                reasons.append(f"strong GC ({wr*100:.0f}% win, {bids_count} bids)")
            elif wr > 0.15:
                points += 7
                reasons.append(f"decent GC ({wr*100:.0f}% win)")
            elif wr > 0:
                points += 5
            elif bids_count > 2:
                points += 3
                reasons.append(f"0/{bids_count} wins with this GC")
            else:
                points += 3
        else:
            points += 3

        # 3. Facility type (max 8 pts)
        ftype = self._classify_facility(bid.get("project_name", ""))
        ft_data = self._lookup_facility_type(ftype)
        if ft_data:
            points += 8
        else:
            points += 4

        # 4. Avg contract value alignment (max 8 pts)
        if ft_data:
            avg = ft_data.get("avg_contract_value", 0)
            if avg > 50000:
                points += 8
                reasons.append(f"high-value type (avg ${avg:,.0f})")
            elif avg > 20000:
                points += 5
            else:
                points += 2
        else:
            # If bid has size, estimate value
            if bid.get("size_sf"):
                try:
                    sf = int(str(bid["size_sf"]).replace(",", ""))
                    if sf > 50000:
                        points += 6
                        reasons.append(f"large ({sf:,} SF)")
                    elif sf > 10000:
                        points += 4
                        reasons.append(f"{sf:,} SF")
                    else:
                        points += 2
                except ValueError:
                    points += 2
            else:
                points += 2

        # 5. Due date (max 10 pts)
        try:
            due = datetime.strptime(bid["due_date"], "%m/%d/%Y").date()
            days_left = (due - date.today()).days
            if days_left > 7:
                points += 10
            elif days_left >= 3:
                points += 6
                reasons.append(f"{days_left}d left")
            elif days_left >= 0:
                points += 2
                reasons.append(f"only {days_left}d left")
            else:
                points += 0
                reasons.append("EXPIRED")
        except (ValueError, KeyError):
            points += 5

        # 6. Has documents available (max 5 pts)
        if self._has_documents(bid):
            points += 5
        else:
            points += 0

        # --- Penalties / bonuses ---

        # Hotel penalty
        if ftype == "hotel":
            points -= 5
            reasons.append("hotel — lose at 2x market")

        # Government bonus for prevailing wage
        if ftype == "government_military" and dist < 150:
            points += 2
            reasons.append("govt/prevailing wage")

        # Multiple GCs bidding same project = more competitive
        similar = self._count_similar_projects(bid)
        if similar > 1:
            reasons.append(f"{similar} GCs bidding")

        # Recommendation
        if points >= 35:
            rec = "BID"
        elif points >= 20:
            rec = "CONSIDER"
        else:
            rec = "SKIP"

        reason = "; ".join(reasons) if reasons else "standard evaluation"

        return {
            "score": points,
            "recommendation": rec,
            "reason": reason,
            "facility_type": ftype,
        }

    def _has_documents(self, bid: dict) -> bool:
        """Check if we've already downloaded docs for this project."""
        name = bid.get("project_name", "")
        slug = re.sub(r'[^a-z0-9\s]', ' ', name.lower())
        slug = re.sub(r'\s+', '_', slug.strip())[:80]

        # Check if project dir exists with documents
        proj_dir = PROJECTS_DIR / slug
        if proj_dir.exists():
            docs_dir = proj_dir / "documents"
            if docs_dir.exists() and any(docs_dir.iterdir()):
                return True
            # Check for any PDFs in project dir
            if list(proj_dir.glob("**/*.pdf")):
                return True
        # Also check old-style folder names (with hyphens)
        slug2 = re.sub(r'[^a-z0-9]', '-', name.lower()).strip('-')[:80]
        proj_dir2 = PROJECTS_DIR / slug2
        if proj_dir2.exists() and list(proj_dir2.glob("**/*.pdf")):
            return True
        return False

    def _count_similar_projects(self, bid: dict) -> int:
        """Count how many GCs are bidding the same project."""
        bids = self._load_bids()
        name_lower = bid["project_name"].lower()
        count = 0
        for b in bids:
            if b is bid:
                continue
            other_name = b["project_name"].lower()
            # Fuzzy match project names (ignoring GC-specific prefixes)
            if SequenceMatcher(None, name_lower[:30], other_name[:30]).ratio() > 0.65:
                count += 1
        return count + 1  # include this bid

    # ==================================================================
    # QUERY HANDLERS — ranked views by various criteria
    # ==================================================================

    def get_ranked_bids(self, query: str) -> str:
        """Return bids ranked by size, distance, score, GC, or facility type."""
        bids = self._load_active_bids()

        if "largest" in query or "biggest" in query:
            return self._rank_by_size(bids)
        if "closest" in query or "nearest" in query:
            return self._rank_by_distance(bids)
        if "best" in query or "top" in query or "score" in query:
            return self._rank_by_score(bids)
        if "soonest" in query or "urgent" in query or "next" in query:
            return self._rank_by_due_date(bids)
        if "by gc" in query or "gc " in query or "contractor" in query:
            return self._group_by_gc(bids)
        if "type" in query or "facility" in query or "category" in query:
            return self._group_by_facility_type(bids)

        return (
            "RANKING OPTIONS:\n"
            "  \"which is largest\" — by square footage\n"
            "  \"which is closest\" — by distance\n"
            "  \"best bids\" / \"top bids\" — by Carol's score\n"
            "  \"soonest due\" — by due date\n"
            "  \"by gc\" — grouped by general contractor\n"
            "  \"by type\" — grouped by facility type"
        )

    def _rank_by_size(self, bids: list) -> str:
        with_size = [b for b in bids if b.get("size_sf")]
        with_size.sort(key=lambda b: int(str(b["size_sf"]).replace(",", "")), reverse=True)
        if not with_size:
            return "No bids have square footage data yet."
        lines = ["LARGEST BIDS (by SF):\n"]
        for i, b in enumerate(with_size[:10], 1):
            source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
            dist = f"{b['distance_miles']:.0f}mi" if b.get("distance_miles") else "?mi"
            score = self.score_bid(b)
            lines.append(
                f"  {i}. {b['project_name'][:42]} {source} — "
                f"{b['size_sf']} SF — {dist} — Due {b.get('due_date','?')} [{score['recommendation']}]"
            )
        return "\n".join(lines)

    def _rank_by_distance(self, bids: list) -> str:
        bids.sort(key=lambda b: b.get("distance_miles") or 9999)
        lines = ["CLOSEST BIDS:\n"]
        for i, b in enumerate(bids[:10], 1):
            source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
            dist = f"{b['distance_miles']:.0f}mi" if b.get("distance_miles") else "?mi"
            score = self.score_bid(b)
            lines.append(
                f"  {i}. {b['project_name'][:42]} {source} — {dist} — "
                f"Due {b.get('due_date','?')} — {b.get('gc','?')[:22]} [{score['recommendation']}]"
            )
        return "\n".join(lines)

    def _rank_by_score(self, bids: list) -> str:
        scored = [(b, self.score_bid(b)) for b in bids]
        scored.sort(key=lambda x: x[1]["score"], reverse=True)
        lines = ["BEST BIDS (by Carol's score):\n"]
        for i, (b, s) in enumerate(scored[:10], 1):
            source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
            dist = f"{b['distance_miles']:.0f}mi" if b.get("distance_miles") else "?mi"
            lines.append(
                f"  {i}. [{s['score']}/51] {b['project_name'][:40]} {source} — "
                f"{dist} — {b.get('gc','?')[:20]} [{s['recommendation']}]"
            )
        return "\n".join(lines)

    def _rank_by_due_date(self, bids: list) -> str:
        dated = []
        for b in bids:
            try:
                dt = datetime.strptime(b["due_date"], "%m/%d/%Y").date()
                dated.append((b, dt))
            except (ValueError, KeyError):
                pass
        dated.sort(key=lambda x: x[1])
        lines = ["SOONEST DUE:\n"]
        for i, (b, dt) in enumerate(dated[:10], 1):
            source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
            dist = f"{b['distance_miles']:.0f}mi" if b.get("distance_miles") else "?mi"
            days_left = (dt - date.today()).days
            lines.append(
                f"  {i}. {b['project_name'][:40]} {source} — Due {b['due_date']} "
                f"({days_left}d) — {dist} — {b.get('gc','?')[:20]}"
            )
        return "\n".join(lines)

    def _group_by_gc(self, bids: list) -> str:
        gc_groups = {}
        for b in bids:
            gc = b.get("gc", "Unknown")[:30]
            gc_groups.setdefault(gc, []).append(b)
        # Sort by count descending
        sorted_gcs = sorted(gc_groups.items(), key=lambda x: len(x[1]), reverse=True)
        lines = ["BIDS BY GENERAL CONTRACTOR:\n"]
        for gc, gc_bids in sorted_gcs[:15]:
            gc_data = self._lookup_gc(gc)
            wr_str = ""
            if gc_data:
                wr = gc_data.get("win_rate", 0)
                wr_str = f" — {wr*100:.0f}% win rate"
            lines.append(f"  {gc} ({len(gc_bids)} bids{wr_str})")
            for b in gc_bids[:3]:
                source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
                lines.append(f"    - {b['project_name'][:40]} {source} — Due {b.get('due_date','?')}")
            if len(gc_bids) > 3:
                lines.append(f"    ... and {len(gc_bids) - 3} more")
        return "\n".join(lines)

    def _group_by_facility_type(self, bids: list) -> str:
        type_groups = {}
        for b in bids:
            ftype = self._classify_facility(b.get("project_name", ""))
            type_groups.setdefault(ftype, []).append(b)
        sorted_types = sorted(type_groups.items(), key=lambda x: len(x[1]), reverse=True)
        lines = ["BIDS BY FACILITY TYPE:\n"]
        for ftype, ft_bids in sorted_types:
            ft_data = self._lookup_facility_type(ftype)
            avg_str = ""
            if ft_data:
                avg = ft_data.get("avg_contract_value", 0)
                if avg:
                    avg_str = f" — avg ${avg:,.0f}"
            lines.append(f"  {ftype.replace('_', ' ').title()} ({len(ft_bids)} bids{avg_str})")
            for b in ft_bids[:3]:
                source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
                dist = f"{b['distance_miles']:.0f}mi" if b.get("distance_miles") else "?mi"
                lines.append(f"    - {b['project_name'][:40]} {source} — {dist}")
            if len(ft_bids) > 3:
                lines.append(f"    ... and {len(ft_bids) - 3} more")
        return "\n".join(lines)

    # ==================================================================
    # MARK BID DECISIONS
    # ==================================================================

    def mark_bid_decision(self, project_name: str, decision: str) -> str:
        """Mark a bid as BID / SKIP / HOLD in active_bids.json."""
        bids = self._load_bids()
        names = [b["project_name"] for b in bids]
        matches = get_close_matches(
            project_name.lower(), [n.lower() for n in names], n=1, cutoff=0.4
        )
        if not matches:
            # Substring fallback
            for b in bids:
                if project_name.lower() in b["project_name"].lower():
                    b["decision"] = decision
                    source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
                    self._save_bids(bids)
                    return f"Marked {b['project_name']} {source} as {decision}."
            return f"Couldn't find a bid matching \"{project_name}\"."

        for b in bids:
            if b["project_name"].lower() == matches[0]:
                b["decision"] = decision
                source = "(CC)" if b.get("source") == "constructconnect" else "(BC)"
                self._save_bids(bids)
                return f"Marked {b['project_name']} {source} as {decision}."

        return f"Couldn't update bid for \"{project_name}\"."

    # ==================================================================
    # GMAIL INBOX WATCHER — check for bid invitations
    # ==================================================================

    def watch_email_inbox(self) -> list:
        """
        Check Gmail for new bid invitations from the past 24 hours.
        Searches for: subject contains "invitation" OR "bid request" OR "RFP"
        OR "subcontractor" OR "painting".
        Returns list of new bid-like email objects.
        """
        try:
            import imaplib
            import email as email_lib
            from email.header import decode_header
        except ImportError:
            return []

        gmail_user = "estimates@carolinacommercialfinishes.com"
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")

        found_bids = []

        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(gmail_user, gmail_pass)
            mail.select("inbox")

            # Search bid-related emails from past 7 days (was 1 — missed Thu-Sat sends)
            since_date = (date.today() - timedelta(days=7)).strftime("%d-%b-%Y")
            search_queries = [
                f'(SINCE "{since_date}" SUBJECT "invitation")',
                f'(SINCE "{since_date}" SUBJECT "bid request")',
                f'(SINCE "{since_date}" SUBJECT "RFP")',
                f'(SINCE "{since_date}" SUBJECT "ITB")',
                f'(SINCE "{since_date}" SUBJECT "painting")',
                f'(SINCE "{since_date}" SUBJECT "subcontractor")',
                f'(SINCE "{since_date}" SUBJECT "proposal")',
            ]

            seen_ids = set()
            for query in search_queries:
                try:
                    status, messages = mail.search(None, query)
                    if status != "OK":
                        continue
                    for msg_id in messages[0].split():
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)

                        status, msg_data = mail.fetch(msg_id, '(BODY.PEEK[])')
                        if status != "OK":
                            continue

                        msg = email_lib.message_from_bytes(msg_data[0][1])

                        # Decode subject
                        subject = ""
                        raw_subject = msg.get("Subject", "")
                        decoded = decode_header(raw_subject)
                        for part, encoding in decoded:
                            if isinstance(part, bytes):
                                subject += part.decode(encoding or "utf-8", errors="replace")
                            else:
                                subject += part

                        sender = msg.get("From", "")
                        msg_date = msg.get("Date", "")

                        # Extract body preview
                        body_preview = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    body_preview = part.get_payload(
                                        decode=True
                                    ).decode("utf-8", errors="replace")[:500]
                                    break
                        else:
                            body_preview = msg.get_payload(
                                decode=True
                            ).decode("utf-8", errors="replace")[:500]

                        found_bids.append({
                            "subject": subject,
                            "from": sender,
                            "date": msg_date,
                            "body_preview": body_preview,
                            "source": "email",
                        })
                except Exception:
                    continue

            mail.logout()
        except Exception as e:
            return [{"error": str(e)}]

        return found_bids

    def check_email_bids(self) -> str:
        """Check email for bid invitations and return formatted summary."""
        emails = self.watch_email_inbox()

        if not emails:
            return "No new bid invitations found in email (past 24 hours)."

        if emails and emails[0].get("error"):
            return f"Email check failed: {emails[0]['error']}"

        lines = [f"EMAIL BID INVITATIONS: {len(emails)} found\n"]
        for i, e in enumerate(emails, 1):
            lines.append(
                f"  {i}. {e['subject'][:60]}\n"
                f"     From: {e['from'][:50]}\n"
                f"     Date: {e['date'][:30]}\n"
            )

        return "\n".join(lines)

    # ==================================================================
    # HELPERS
    # ==================================================================

    def _load_bids(self) -> list:
        if not BIDS_FILE.exists():
            return []
        return json.loads(BIDS_FILE.read_text(encoding="utf-8"))

    def _save_bids(self, bids: list):
        BIDS_FILE.write_text(
            json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _load_active_bids(self) -> list:
        """Load bids and filter out expired ones."""
        bids = self._load_bids()
        today = date.today()
        active = []
        for b in bids:
            try:
                due = datetime.strptime(b["due_date"], "%m/%d/%Y").date()
                if due >= today:
                    active.append(b)
            except (ValueError, KeyError):
                active.append(b)
        return active

    def _load_gc_data(self):
        if self._gc_cache is not None:
            return self._gc_cache
        self._gc_cache = {}
        if GC_DIR.exists():
            for f in GC_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self._gc_cache[data.get("name", "").lower()] = data
                except (json.JSONDecodeError, KeyError):
                    pass
        return self._gc_cache

    def _lookup_gc(self, gc_name: str) -> dict | None:
        gcs = self._load_gc_data()
        gc_lower = gc_name.lower()
        for key, data in gcs.items():
            if gc_lower.startswith(key[:10]) or key.startswith(gc_lower[:10]):
                return data
        # Fuzzy fallback
        for key, data in gcs.items():
            if SequenceMatcher(None, gc_lower[:20], key[:20]).ratio() > 0.7:
                return data
        return None

    def _load_facility_data(self):
        if self._facility_cache is not None:
            return self._facility_cache
        self._facility_cache = {}
        if FACILITY_DIR.exists():
            for f in FACILITY_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self._facility_cache[data.get("type", "")] = data
                except (json.JSONDecodeError, KeyError):
                    pass
        return self._facility_cache

    def _lookup_facility_type(self, ftype: str) -> dict | None:
        return self._load_facility_data().get(ftype)

    @staticmethod
    def _classify_facility(name: str) -> str:
        nl = name.lower()
        if "food lion" in nl:
            return "grocery"
        if "whole food" in nl:
            return "grocery"
        if any(k in nl for k in ["dental", "heartland"]):
            return "dental"
        if any(k in nl for k in ["vamc", "va ", "ehrm", "camp butner", "fort bragg", "sof ",
                                   "f-35", "uscg", "national guard", "coast guard"]):
            return "government_military"
        if any(k in nl for k in ["postal", "usps", "dept of education", "courthouse"]):
            return "government_civic"
        if any(k in nl for k in ["hotel", "suites", "marriott", "hyatt", "home 2",
                                   "towneplace", "hampton", "hilton"]):
            return "hotel"
        if any(k in nl for k in ["bojangles", "eggs up", "dutch bros", "fuzzy",
                                   "sheetz", "pure green", "7-eleven", "seven eleven"]):
            return "restaurant_qsr"
        if any(k in nl for k in ["victoria", "cvs", "chase bank", "boot barn",
                                   "sally beauty", "savers", "dollar tree", "pnc"]):
            return "retail"
        if any(k in nl for k in ["church", "fellowship", "presbyterian", "baptist"]):
            return "religious"
        if any(k in nl for k in ["school", "elementary", "university", "college",
                                   "education", "kindercare", "media center"]):
            return "education"
        if any(k in nl for k in ["brewery", "seabird", "inn"]):
            return "hospitality"
        if any(k in nl for k in ["ems station", "fire station", "police"]):
            return "government_civic"
        if any(k in nl for k in ["park ", "parks ", "recreation"]):
            return "parks_recreation"
        if any(k in nl for k in ["generator", "maintenance", "elevator", "renovation"]):
            return "renovation"
        if any(k in nl for k in ["warehouse", "flex space", "industrial"]):
            return "industrial"
        if any(k in nl for k in ["residence hall", "dorm", "apartment"]):
            return "multifamily"
        return "commercial"
