/**
 * ResearchLabPage — a campaign a chat agent filed.
 *
 * The agent files the campaign NOT started and hands the user a
 * `/auto-research?campaign=<id>` link. What the page owes that user: the link
 * opens that campaign, the campaign says an agent filed it, and the Start
 * control is on the page, because starting it is the user's click.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import ResearchLabPage from './ResearchLabPage'
import { api } from '../../api/client'

vi.mock('../../api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      researchCampaigns: vi.fn(),
      researchCampaign: vi.fn(),
      researchAction: vi.fn(),
      researchKnowledgeStatus: vi.fn(() => Promise.resolve({ in_library: false })),
      researchReportStatus: vi.fn(() => Promise.resolve({ slug: null })),
    },
  }
})

// `CampaignDetail` opens a live SSE connection on mount; no push is needed here.
class FakeEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close() {}
}

const k = (key: string, opts?: Record<string, unknown>) => i18nT(`apps.autoResearch.researchLabPage.${key}`, opts)

const filed = {
  id: 'abcd1234', name: 'Vector deletes', question: 'How do vector databases handle deletes?',
  sub_questions: '[]', sources: '[]', max_cycles: 30, idle_secs: 120, total_cycles: 0,
  findings: [], status: 'ready', created_by: 'agent', model: '', model_withheld: null, served_model: '',
}
const own = { ...filed, id: 'eeee5555', name: 'Own', question: 'A question the user filed here', status: 'complete', created_by: null }

describe('ResearchLabPage agent-filed campaigns', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('EventSource', FakeEventSource)
    vi.mocked(api.researchCampaigns).mockResolvedValue([filed, own])
    vi.mocked(api.researchCampaign).mockImplementation((id: string) =>
      Promise.resolve(id === filed.id ? filed : own))
    vi.mocked(api.researchAction).mockResolvedValue({ status: 'running' })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('the ?campaign= link opens that campaign with its Start control', async () => {
    renderWithProviders(<ResearchLabPage />, { route: `/auto-research?campaign=${filed.id}` })
    expect(await screen.findByRole('heading', { name: filed.name })).toBeInTheDocument()
    expect(api.researchCampaign).toHaveBeenCalledWith(filed.id)
    expect(screen.getByText(k('ready_not_started_hint'))).toBeInTheDocument()
    expect(screen.getByText(k('created_by_agent'))).toBeInTheDocument()
    // Nothing has run, so the page does not claim a cycle is in progress.
    expect(screen.queryByText(k('first_cycle_in_progress'))).not.toBeInTheDocument()
  })

  it('starting stays the user\'s click', async () => {
    renderWithProviders(<ResearchLabPage />, { route: `/auto-research?campaign=${filed.id}` })
    const start = await screen.findByRole('button', { name: new RegExp(k('start_campaign')) })
    expect(api.researchAction).not.toHaveBeenCalled()
    fireEvent.click(start)
    await waitFor(() => expect(api.researchAction).toHaveBeenCalledWith(filed.id, 'start'))
  })

  it('the list marks only the agent-filed campaign', async () => {
    renderWithProviders(<ResearchLabPage />)
    expect(await screen.findByText(filed.question)).toBeInTheDocument()
    expect(screen.getByText(own.question)).toBeInTheDocument()
    expect(screen.getAllByText(k('created_by_agent'))).toHaveLength(1)
  })

  it('a campaign the user filed shows no agent marker and no Start control once run', async () => {
    renderWithProviders(<ResearchLabPage />, { route: `/auto-research?campaign=${own.id}` })
    expect(await screen.findByRole('heading', { name: own.name })).toBeInTheDocument()
    expect(screen.queryByText(k('created_by_agent'))).not.toBeInTheDocument()
    expect(screen.queryByText(k('ready_not_started_hint'))).not.toBeInTheDocument()
  })

  it('a ?campaign= value that is not a campaign id is ignored', async () => {
    renderWithProviders(<ResearchLabPage />, { route: '/auto-research?campaign=../etc' })
    expect(await screen.findByText(filed.question)).toBeInTheDocument()
    expect(api.researchCampaign).not.toHaveBeenCalled()
  })
})
