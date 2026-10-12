/**
 * Screenshot harness for PR #18923 (show applied template even with preview off).
 *
 * Captures the three new states in MembersPage the PR adds, plus the control
 * (default page, no adoption) using the real members-page capture fixture with
 * mocked API responses. Serves the existing crew-webview-doc for the ADOPTED
 * state (the page a crewmate applied), so the screenshot shows the iframe.
 *
 * Frames:
 *   01-decided-adopted-dark  adopted crewmate -> CrewDashboardTab (preview off)
 *   02-loading-dark          pending read -> "Loading dashboard…" line
 *   03-error-dark            failed read -> ErrorNotice + Try again
 *   04-default-dark          not adopted -> panel (unchanged behaviour)
 *
 * Usage (from website/): CHROMIUM_PATH=<chrome> node capture/shoot-dashboard-tab-template.mjs [outDir]
 */
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/dashboard-tab-template'
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5178, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log('vite up on 5178')

const BASE = 'http://127.0.0.1:5178'
const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

const MEMBER = { name: 'radar', slug: 'radar', display_name: '', slot_key: '', running: false, last_active_ts: 0 }
const PANEL_HTML = '<html><head><meta charset="utf-8"></head><body style="font-family:sans-serif;padding:24px;background:#fff;color:#111"><h2 style="margin:0 0 8px">Radar — Project Report</h2><p style="color:#555;margin:0">Applied dashboard page (instance_version: 2, preview OFF).</p></body></html>'

async function shootMembersPage(name, mockDashboard) {
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 700 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()

  // Don't load the preview flag from localStorage
  await page.addInitScript(() => { localStorage.removeItem('mc-preview-PREVIEW_DASHBOARD') })

  await page.route('**/api/members*', async (route) => {
    const url = route.request().url()
    if (url.includes('/dashboard')) {
      await mockDashboard(route)
    } else if (url.includes('/panel')) {
      await route.fulfill({ status: 404 })
    } else {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ members: [MEMBER] })
      })
    }
  })
  await page.route('**/api/**', async (route) => route.continue().catch(() => {}))

  await page.goto(`${BASE}/capture/members-page.html?theme=dark&route=/members?member=radar`)
  await page.waitForTimeout(3000)
  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('shot', name)
  await ctx.close()
}

// 01 — adopted: memberDashboard returns instance_version > 0
await shootMembersPage('01-decided-adopted-dark', (route) =>
  route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ instance_version: 2, template: { id: 'project-report', version: 1 }, html: PANEL_HTML, manifest: {} })
  })
)

// 02 — loading: memberDashboard delays (use a hang to show pending state)
// Actually simulate 408 which useAppliedDashboard treats as retryable 'error',
// so instead hang the request long enough to catch the 'pending' render
async function pendingShot() {
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 700 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.addInitScript(() => { localStorage.removeItem('mc-preview-PREVIEW_DASHBOARD') })

  // Never fulfill to stay pending
  await page.route('**/api/members*', async (route) => {
    const url = route.request().url()
    if (url.includes('/dashboard')) {
      // Don't respond — stays pending
      setTimeout(() => {}, 30000)
    } else if (url.includes('/panel')) {
      await route.fulfill({ status: 404 })
    } else {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ members: [MEMBER] })
      })
    }
  })
  await page.route('**/api/**', async (route) => route.continue().catch(() => {}))
  await page.goto(`${BASE}/capture/members-page.html?theme=dark&route=/members?member=radar`)
  await page.waitForTimeout(2500)
  await page.screenshot({ path: join(OUT, '02-loading-dark.png') })
  console.log('shot 02-loading-dark')
  await ctx.close()
}
await pendingShot()

// 03 — error: memberDashboard returns 500
await shootMembersPage('03-error-dark', (route) =>
  route.fulfill({ status: 500, body: 'Internal Server Error' })
)

// 04 — default: memberDashboard returns instance_version 0 (no adoption)
await shootMembersPage('04-default-dark', (route) =>
  route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ instance_version: 0, template: { id: '', version: 0 }, html: '', manifest: {} })
  })
)

await browser.close()
await server.close()
console.log('done')
