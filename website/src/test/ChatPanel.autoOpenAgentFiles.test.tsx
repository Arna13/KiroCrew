/**
 * The Settings → Chat → Side Panel switch for dashboard.auto_open_agent_files:
 * off by default, and written through the dashboard config route alone.
 */
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const BASE_DASH = {
  restore_sessions: false,
  restore_window_minutes: 30,
  merge_queued_messages: false,
  widget_density: 'more' as const,
  verbosity: 'default' as const,
  quick_send: false,
  session_grid: false,
  tail_fork_enabled: false,
  link_previews: false,
}

const { dashboardConfigMock, updateDashboardConfigMock } = vi.hoisted(() => ({
  dashboardConfigMock: vi.fn(),
  updateDashboardConfigMock: vi.fn(() => Promise.resolve({})),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: dashboardConfigMock,
    voiceConfig: () => Promise.resolve({ enabled: false, voice: 'Ruth', engine: 'neural', rate: '100%', autoSpeak: false, aws_profile: '', region: '' }),
    sttConfig: () => Promise.resolve({ enabled: false, provider: '', model: '', available: false, streaming: false, transcribe_region: '', transcribe_profile: '', language_code: 'en-US', models: {}, language_codes: [] }),
    kirocrewConfig: () => Promise.resolve({ agent: { completion_keep: 'head', completion_keep_chars: 3000, model: 'auto', reasoning_effort: '' } }),
    models: () => Promise.resolve([{ model_name: 'auto', description: 'Default' }]),
    patchConfig: () => Promise.resolve({}),
    updateDashboardConfig: updateDashboardConfigMock,
    updateVoiceConfig: () => Promise.resolve({}),
    updateSttConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    // The panel reads the feature-video cache on mount. Downloads OFF here, so
    // the readout renders its policy line and no button -- these files measure
    // other settings, and a live control would put a stray button in their reach.
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1',
      cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

import { Provider } from 'react-redux'

// ChatPanel reads the active slot from redux to name the session on its
// feature-video calls, so these renders need a store. A FRESH one per file,
// not the app singleton: a shared store would carry `activeSlot` across suites.
import { createTestStore } from './helpers'

function wrap(ui: React.ReactElement, sub = 'transcript') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter initialEntries={[`/settings?tab=chat&sub=${sub}`]}><Provider store={createTestStore()}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider></MemoryRouter>)
}

describe('ChatPanel settings – Auto-Open Agent Files toggle', () => {
  const NAME = 'Auto-Open Agent Files in Side Panel'
  beforeEach(() => {
    dashboardConfigMock.mockReset()
    updateDashboardConfigMock.mockClear()
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, auto_open_agent_files: false })
  })

  it('renders off in the Side Panel section', async () => {
    wrap(<ChatPanel />, 'sidepanel')
    const toggle = await screen.findByRole('switch', { name: NAME })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
  })

  it('reads an absent key as off', async () => {
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH })
    wrap(<ChatPanel />, 'sidepanel')
    const toggle = await screen.findByRole('switch', { name: NAME })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
  })

  it('turns the setting on through updateDashboardConfig, sending only that key', async () => {
    wrap(<ChatPanel />, 'sidepanel')
    const toggle = await screen.findByRole('switch', { name: NAME })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'))
    fireEvent.click(toggle)
    await waitFor(() =>
      expect(updateDashboardConfigMock).toHaveBeenCalledWith({ auto_open_agent_files: true }),
    )
  })

  it('reflects auto_open_agent_files: true from the server', async () => {
    dashboardConfigMock.mockResolvedValue({ ...BASE_DASH, auto_open_agent_files: true })
    wrap(<ChatPanel />, 'sidepanel')
    const toggle = await screen.findByRole('switch', { name: NAME })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  })
})
