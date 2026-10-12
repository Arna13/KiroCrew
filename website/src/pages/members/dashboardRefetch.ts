/**
 * How long an open Dashboard tab may go without re-reading, when no WebSocket
 * frame has told it to.
 *
 * The tab's liveness is the two frames `handleDashboardMoved` listens for -- a
 * crewmate's own write, and a fold advancing. This number is only the floor under
 * them, for a frame that never arrived because the socket dropped or the tab was
 * closed when it was sent.
 *
 * Kept in this leaf module so the eagerly loaded crew drawer can share it without
 * pulling the lazily loaded Dashboard tab into its chunk.
 *
 * Exported so a test can assert it is FINITE: a missing or infinite interval looks
 * identical to a working one in every case where a frame does arrive, which is
 * every case anybody writes.
 */
export const DASHBOARD_FALLBACK_REFETCH_MS = 60_000

/** The goal conductor's drawer template, the server's `BOARD_TEMPLATE_ID`. */
export const CONDUCTOR_PANEL_TEMPLATE = 'kirocrew-conductor'

/**
 * The drawer's re-read floor: only the conductor's board polls.
 *
 * Its board is derived from the work ledger on every read, and a ledger entry
 * sends no frame that names the panel query, so the read has to come round on its
 * own. Every other template keeps the push-only refresh (`panel_published`).
 *
 * The crew's slug counts as well as the record's template: the server picks a
 * crew's default template by its slug, so the conductor's first ledger item must
 * appear in a drawer that opened before any board existed.
 */
export function panelFallbackRefetch(
  slug: string,
  template: string | null | undefined,
): number | false {
  return slug === CONDUCTOR_PANEL_TEMPLATE || template === CONDUCTOR_PANEL_TEMPLATE
    ? DASHBOARD_FALLBACK_REFETCH_MS
    : false
}
