# Evidence for PR #19060 — page host + subscribe

Media only. Nothing here is built or imported; it exists so the pull request's
description can link stills and a recording without committing them to a code branch.

Each iframe in these captures holds exactly what
`member_dashboard_fields.render_package_page` composes for the stored gallery package
`builtin_skills/dashboard/gallery/dispatch-tree/package.json`, with its values built by
`dashboard_frame.read_payload` the way the route builds them. The chrome around the frame
(the crewmate's tab strip) is the harness's, and so is the parent script that posts the
patch in the recording — that half stands in for `CrewDynamicDashboard.tsx`, which posts
the same `kirocrew-dashboard:data` message when the `dashboard_fields` WS frame arrives.

Every capture asserted real content before it was saved: the number of rows the page's own
script drew inside the frame. A blank frame and a working one are indistinguishable from
outside, so a shot taken without that check would ship as evidence for nothing.

| File | Requirement | What it proves |
|---|---|---|
| `r1-populated.png` | R1 a page its owner wrote, rendered | Five boards of a fleet mid-run: who dispatched whom, each board's goal, its entry count. The root's spine is bright, a child hangs off an elbow. 5 rows asserted. |
| `r2-empty.png` | R2 the empty state | No board has dispatched another yet. The page says so in a sentence instead of drawing an empty frame. 0 rows asserted. |
| `r3-cycles.png` | R3 a broken tree | Two boards citing each other are each marked "on a cycle" in the warning colour, and the header counts 2 on a cycle, 1 unresolved. 4 rows asserted. |
| `r4-stale.png` | R4 a stale read | The host's own band, above the page and not over it, naming the field that did not resolve: "work_items is from an earlier read. Everything else on this page is current." The page cannot suppress it. |
| `r5-tree-updates-live.mp4` | R5 the live redraw | One `dashboard_fields` patch arrives and the tree changes in place — a sixth board appears and two counts move — with no reload and no refetch. |
| `r5-live-after.png` | R5 | The last frame of the recording, for a reader who cannot play video. |

The recording's own assertion is the point of it: the capture fails unless the row count
rises across the push (5 → 6). A video of a page that redrew nothing would otherwise look
the same.

## The version sequence in the recording is the real one

`LivePage._full_read` mints a version for every full read, so a first paint carries 1 and
the first patch is 2. The recording uses exactly that, which also demonstrates the gap
check accepting a consecutive patch under the host's own numbering. An earlier take
started the island at 0; it looked identical and recorded a sequence the product does not
send, so it was replaced.


## Re-recorded against the fold's real row shape

An earlier take built each board row as `slot_key` / `on_cycle` / `entries`. The
`worktree` fold emits `board` / `cycle` / `items`, so the reference page read three keys
that do not exist, and every row of a copied page would have shown "a board" with a count
of zero and no cycle flag. The page is fixed and these captures use the renderer's own key
names, so what is on screen is what the product produces.

A test now pins this: the row keys are read out of `_worktree_render`'s own source and
every `board.<key>` the page reads must be one of them.
