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
