"""A deliberately busy synthetic Herdr snapshot, for demos and recordings.

``tests/fixtures.py`` is small on purpose -- it is there to make assertions
readable. This one is the opposite: three workspaces, twenty-odd rows, every
status the bar knows how to draw, and enough agents that the list scrolls and
the spinners never stop. It is what a real afternoon looks like, and it is the
session ``scripts/record_demo.py`` films.

Everything here is invented -- the projects, the branches, the agents and the
pane output. No real repository, host or session is referenced.
"""

from __future__ import annotations

from typing import Any, Dict, List

HOME = "/Users/dev"

# workspace label -> (id, number, status, branch)
_SPACES = [
    ("atlas", "w1", 1, "working", None),
    ("harbor", "w2", 2, "blocked", "feat/retry-queue"),
    ("lumen", "w3", 3, "working", "feat/preview-tail"),
]

# (workspace, tab number, label, status, agent, agent name, cwd suffix)
_ROWS = [
    # atlas -- the app being worked on
    ("w1", 1, "server", "none", None, None, ""),
    ("w1", 2, "Redesign the invoice list empty state", "working", "claude", None, ""),
    ("w1", 3, "Check the invoice email copy", "working", "claude", None, ""),
    ("w1", 4, "checkout copy unclear", "blocked", "codex", None, "/web"),
    ("w1", 5, "search index rebuild", "done", "claude", "indexer", "/.worktrees/search"),
    ("w1", 6, "logs", "none", None, None, ""),
    ("w1", 7, "Tighten rate limit buckets", "working", "codex", None, ""),
    ("w1", 8, "Fix stale invoice cache", "blocked", "claude", None, "/api"),
    ("w1", 9, "tests", "none", None, None, ""),
    # harbor -- a second checkout, mid-review
    ("w2", 1, "retry queue backoff", "blocked", "codex", None, ""),
    ("w2", 2, "migrations", "idle", "gemini", None, ""),
    ("w2", 3, "Drain the export queue", "working", "claude", "backfill", ""),
    ("w2", 4, "psql", "none", None, None, ""),
    ("w2", 5, "Rewrite webhook dedupe", "done", "codex", None, ""),
    # lumen -- a terminal UI, hence the socket and decoder work
    ("w3", 1, "Tail preview through the socket", "working", "claude", None, ""),
    ("w3", 2, "flaky decoder test", "blocked", "claude", None, ""),
    ("w3", 3, "lint", "none", None, None, ""),
    ("w3", 4, "Document the keyboard table", "working", "gemini", None, "/docs"),
]

FOCUSED_TAB = "w1:t3"
FOCUSED_PANE = "w1:p3"


def snapshot() -> Dict[str, Any]:
    workspaces: List[Dict[str, Any]] = []
    for label, workspace_id, number, status, branch in _SPACES:
        tabs_here = [row for row in _ROWS if row[0] == workspace_id]
        space: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "label": label,
            "number": number,
            "focused": workspace_id == "w1",
            "active_tab_id": "%s:t1" % workspace_id,
            "agent_status": status,
            "tab_count": len(tabs_here),
            "pane_count": len(tabs_here) + 1,
        }
        if branch:
            space["worktree"] = {"branch": branch}
        workspaces.append(space)

    tabs: List[Dict[str, Any]] = []
    agents: List[Dict[str, Any]] = []
    panes: List[Dict[str, Any]] = []

    labels = {space[1]: space[0] for space in _SPACES}
    for workspace_id, number, label, status, agent, agent_name, suffix in _ROWS:
        tab_id = "%s:t%d" % (workspace_id, number)
        pane_id = "%s:p%d" % (workspace_id, number)
        cwd = "%s/workspace/%s%s" % (HOME, labels[workspace_id], suffix)
        tabs.append(
            {
                "tab_id": tab_id,
                "workspace_id": workspace_id,
                "label": label,
                "number": number,
                "agent_status": status,
                "pane_count": 1,
                "focused": tab_id == FOCUSED_TAB,
            }
        )
        record = {
            "pane_id": pane_id,
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "cwd": cwd,
            "focused": pane_id == FOCUSED_PANE,
        }
        if agent:
            entry = dict(record)
            entry.update(
                {
                    "agent": agent,
                    "agent_status": status,
                    "terminal_title_stripped": label,
                }
            )
            if agent_name:
                entry["name"] = agent_name
            agents.append(entry)
            panes.append(dict(record, agent=agent, agent_status=status))
        else:
            panes.append(record)

    return {
        "version": "0.7.5",
        "protocol": 18,
        "focused_workspace_id": "w1",
        "focused_tab_id": FOCUSED_TAB,
        "focused_pane_id": FOCUSED_PANE,
        "workspaces": workspaces,
        "tabs": tabs,
        "panes": panes,
        "agents": agents,
        "layouts": [],
    }


# Where the demo has "been" before the bar opened. Recents float to the top of
# the resting list, which is the behaviour worth showing on frame one.
RECENTS = ["w2:p1", "w1:p4", "w3:p2"]


# What the preview column tails, per pane. Written so the interesting line is
# last: render_preview() keeps the tail, exactly like the real pane does.
PREVIEWS = {
    "w1:p1": """▲ dev server · http://localhost:3000
  ✓ ready in 1.4s

  ○ compiling /invoices/[id] ...
  ✓ compiled /invoices/[id] in 812ms (1918 modules)
  GET /invoices/inv_8842 200 in 94ms
""",
    "w1:p2": """● Working · atlas
  Capacity: 3/32 · 4m 12s

> the empty state flashes before the rows land

⏺ Editing src/components/InvoiceSkeleton.tsx
  Holding the skeleton until the query settles.
""",
    "w1:p3": """● Working · atlas
  Capacity: 3/32 · 40s

> does the receipt email still say "no charges"?

⏺ Reading src/email/ReceiptSummary.tsx …
  Two branches render the empty copy. Checking both.
""",
    "w1:p4": """◉ Needs you · atlas/web
  Waiting on input · 2m 08s

⏺ The checkout summary is assembled in three places.

  Do you want one shared formatter, or copy tuned
  per surface? (1) shared  (2) per surface
""",
    "w1:p5": """● Done · search
  Finished · 6m 51s

⏺ Rebuilt the index with the new analyzer chain.
  12 files changed, 318 insertions(+), 44 deletions(-)

  All 84 tests pass. Ready for review.
""",
    "w1:p6": """14:02:11  INFO   sync.pull   invoices=18 window=7d
14:02:11  INFO   cache.get   miss key=inv:2026-07
14:02:12  WARN   db.query    slow query 812ms
14:02:12  INFO   billing     totals recomputed account=1
14:02:13  INFO   http        GET /api/invoices 200 94ms
""",
    "w1:p7": """● Working · atlas
  Capacity: 3/32 · 1m 33s

> the per-key limit is too low for batch importers

⏺ Rewriting the bucket table in lib/ratelimit.ts
  Adding a burst allowance behind a flag.
""",
    "w1:p8": """◉ Needs you · atlas/api
  Waiting on input · 8m 44s

⏺ The invoice cache key ignores the timezone, so a
  Sunday-evening request serves Saturday's totals.

  Fix the key, or invalidate on tz change?
""",
    "w1:p9": """  PASS  src/lib/ratelimit.test.ts
  PASS  src/lib/invoice.test.ts
  FAIL  src/components/InvoiceList.test.tsx

  ● renders the summary › with no invoices
    expected "nothing due" to be in the document
""",
    "w2:p1": """◉ Needs you · harbor
  Waiting on input · 12m 03s

⏺ Backoff is exponential but unbounded -- a dead
  webhook retries for 14 hours.

  Cap at 30 minutes, or drop after 8 attempts?
""",
    "w2:p2": """✓ Idle · harbor
  Ready · last ran 21m ago

  alembic upgrade head
  INFO  [alembic.runtime.migration] Running upgrade
        4f21a0 -> 9c1b7e, add dedupe index
""",
    "w2:p3": """● Working · harbor
  Capacity: 1/32 · 3m 07s

> drain the exports we dropped last Tuesday

⏺ Streaming 4,812 records in pages of 200 …
  page 12/25 · 2,400 written · 0 errors
""",
    "w2:p4": """harbor=# select count(*) from webhook_delivery
harbor-#   where status = 'pending';
 count
-------
  1284
(1 row)
""",
    "w2:p5": """● Done · harbor
  Finished · 22m 18s

⏺ Dedupe now keys on (source, external_id) with a
  partial unique index, so replays are free.

  6 files changed. Migration 9c1b7e added.
""",
    "w3:p1": """● Working · lumen
  Capacity: 2/32 · 55s

> the preview should tail, not snapshot

⏺ Editing src/lumen/panel.py
  Debouncing reads at 70ms and caching for 1.5s.
""",
    "w3:p2": """◉ Needs you · lumen
  Waiting on input · 4m 30s

⏺ test_keys.py::test_paste_bracket fails only when
  the decoder is fed one byte at a time.

  Is a split paste marker worth supporting?
""",
    "w3:p3": """$ lint src tests
All checks passed!

$ python3 -m pytest -q
84 passed in 1.09s
""",
    "w3:p4": """● Working · lumen/docs
  Capacity: 2/32 · 2m 41s

> the keyboard table is missing the mouse row

⏺ Editing docs/keyboard.md
  Adding wheel, click-to-select and click-to-jump.
""",
}

FALLBACK_PREVIEW = """$ herdr status
  3 workspaces · 18 tabs · 21 panes
"""

# What a pane tails once it changes state mid-recording. The recorder flips
# w1:p7 from working to blocked to show the list re-sorting under you.
PREVIEWS_AFTER = {
    ("w1:p7", "blocked"): """◉ Needs you · atlas
  Waiting on input · 1m 58s

⏺ A burst allowance needs a plan tier, and the
  free tier has no ceiling today.

  Cap the free tier, or leave it unlimited?
""",
}
