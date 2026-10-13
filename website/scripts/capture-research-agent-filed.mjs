/**
 * Screenshot harness for a Research Lab campaign a chat agent filed.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures, so no gateway or kiro-cli is needed.
 *   01 dark  → list: the agent-filed, not-started campaign carries the
 *              "Created by an agent" marker; the user's own campaign does not
 *   02 light → /auto-research?campaign=<id> opens that campaign: the marker and
 *              the not-started hint with its Start campaign control
 *   03 dark  → the same detail in the dark theme
 *
 * Usage: node scripts/capture-research-agent-filed.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/research-agent-filed'

mkdirSync(OUT, { recursive: true })

const base = { sub_questions: '[]', sources: '[]', max_cycles: 30, idle_secs: 120, findings: [] }
const FILED = {
  ...base, id: 'cafe0003', name: 'Vector index deletes',
  question: 'How do vector databases handle deletes without rebuilding the index?',
  status: 'ready', total_cycles: 0, created_by: 'agent',
  model: '', model_withheld: null, served_model: '',
}
const OWN = {
  ...base, id: 'cafe0001', name: 'Rate limiting research',
  question: 'How do other teams handle API rate limiting effectively?',
  status: 'complete', total_cycles: 8, created_by: null,
  model: '', model_withheld: null, served_model: '',
}

const extra = (path, route) => {
  if (path === '/api/apps/auto-research/campaigns') return json(route, [FILED, OWN]), true
  if (path === `/api/apps/auto-research/campaigns/${FILED.id}`) return json(route, FILED), true
  if (path === `/api/apps/auto-research/campaigns/${OWN.id}`) return json(route, OWN), true
  if (path.endsWith('/knowledge-status')) return json(route, { in_library: false }), true
  if (path.endsWith('/report-status')) return json(route, { slug: null }), true
  return false
}

async function open(browser, theme, path) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, { theme, extra })
  logPageProblems(page)
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' })
  return { context, page }
}

async function shot(page, name) {
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

let BASE
async function main() {
  const { srv, base: url } = await serveDist()
  BASE = url
  const browser = await chromium.launch()

  let { context, page } = await open(browser, 'dark', '/auto-research')
  await page.waitForSelector(`text=${OWN.question}`, { timeout: 15000 })
  await page.waitForSelector('text=Created by an agent', { timeout: 10000 })
  await shot(page, '01-list-agent-filed-dark')
  await context.close()

  for (const [theme, name] of [['light', '02-detail-ready-light'], ['dark', '03-detail-ready-dark']]) {
    ;({ context, page } = await open(browser, theme, `/auto-research?campaign=${FILED.id}`))
    await page.waitForSelector('button:has-text("Start Campaign")', { timeout: 15000 })
    await shot(page, name)
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
