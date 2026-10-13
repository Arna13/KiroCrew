import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import type { MemberGoals } from '../../api/client'

const memberGoals = vi.hoisted(() => ({
  get: vi.fn(),
  setLongTerm: vi.fn(),
  setKeyResults: vi.fn(),
  removeKeyResult: vi.fn(),
  restoreKeyResult: vi.fn(),
  setSchedules: vi.fn(),
}))

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return { ...actual, api: { memberGoals } }
})

import CrewGoalsPane, { keyResultProgress } from './CrewGoals'

const NOW = 1_800_000_000

const record = (over: Partial<MemberGoals> = {}): MemberGoals => ({
  slug: 'scout',
  member: 'scout',
  long_term: 'Keep the release notes current',
  long_term_updated_at: NOW,
  long_term_updated_by: 'user',
  key_results: [
    {
      id: 'k00000001',
      text: 'PRs with a description',
      target: '27',
      current: '19, the rest wait on the template change',
      updated_at: NOW - 3600,
      readings: [{ value: '12', at: NOW - 7200 }, { value: '19, the rest wait on the template change', at: NOW - 3600 }],
    },
    { id: 'k00000002', text: 'Notes published', target: 'yes', current: '', updated_at: 0, readings: [] },
  ],
  schedules: { time: '09:00', timezone: '', daily: false, weekly: false, weekday: 1, monthly: false, month_day: 1 },
  reports: [{ kind: 'weekly', slug: 'week-41', title: 'Week 41', at: NOW - 86400 }],
  removed_key_result: null,
  limits: {
    long_term_max_chars: 2000,
    max_key_results: 3,
    key_result_text_max_chars: 200,
    key_result_value_max_chars: 40,
    key_result_reading_max_chars: 120,
  },
  ...over,
})

beforeEach(() => {
  for (const fn of Object.values(memberGoals)) fn.mockReset()
})

describe('keyResultProgress', () => {
  it('reads numbers as a fraction of the target and a word target as reached or not', () => {
    expect(keyResultProgress({ current: '19', target: '27' })).toBeCloseTo(19 / 27)
    expect(keyResultProgress({ current: '40', target: '27' })).toBe(1)
    expect(keyResultProgress({ current: 'yes', target: 'Yes' })).toBe(1)
    expect(keyResultProgress({ current: 'no', target: 'yes' })).toBe(0)
    expect(keyResultProgress({ current: '', target: '27' })).toBeNull()
  })
})

describe('CrewGoalsPane', () => {
  it('shows the goal, each milestone with its latest reading in words, and the reports', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByTestId('crew-goals-long-term-text')).toHaveTextContent('Keep the release notes current')
    const section = screen.getByTestId('crew-goals-key-results')
    expect(within(section).getByText('Milestones')).toBeInTheDocument()
    expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(2)
    // A reading is free text, never a bar the record does not hold.
    expect(within(section).getByText('19, the rest wait on the template change')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(within(screen.getByTestId('crew-goals-reports')).getByRole('link', { name: /^Week 41/ })).toHaveAttribute('href', '/artifacts/week-41')
  })

  it('shows no short-term goals: the work ledger says what the crewmate is doing now', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findByTestId('crew-goals-key-results')
    expect(screen.queryByTestId('crew-goals-short-term')).toBeNull()
    expect(screen.queryByTestId('crew-goals-undo')).toBeNull()
  })

  it('opens a milestone to show its earlier readings and its actions', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    const row = within(section).getAllByTestId('goal-key-result')[0]
    const toggle = within(row).getAllByRole('button')[0]
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(row).queryByRole('button', { name: 'Edit milestone' })).toBeNull()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const history = within(row).getByRole('list', { name: 'Earlier readings' })
    expect(within(history).getByText('12')).toBeInTheDocument()
    // The list carries a visible heading, not only an accessible name.
    expect(within(row).getByText('Earlier readings', { selector: 'div' })).toBeInTheDocument()
    // The actions are labelled text buttons, not bare icons.
    expect(within(row).getByRole('button', { name: 'Edit milestone' })).toHaveTextContent('Edit milestone')
    expect(within(row).getByTestId('crew-goals-kr-remove')).toHaveTextContent('Remove milestone')
  })

  it('removes a key result and offers an Undo that restores it', async () => {
    const full = record()
    const removed = record({
      key_results: full.key_results.slice(1),
      removed_key_result: { id: 'k00000001', text: 'PRs with a description', at: NOW },
    })
    memberGoals.get.mockResolvedValue(full)
    memberGoals.removeKeyResult.mockResolvedValue(removed)
    memberGoals.restoreKeyResult.mockResolvedValue(full)
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    expect(within(section).queryByTestId('crew-goals-kr-undo')).toBeNull()
    fireEvent.click(within(within(section).getAllByTestId('goal-key-result')[0]).getAllByRole('button')[0])
    fireEvent.click(within(section).getAllByTestId('crew-goals-kr-remove')[0])
    await waitFor(() => expect(memberGoals.removeKeyResult).toHaveBeenCalledWith('scout', 'scout', 'k00000001'))
    expect(memberGoals.setKeyResults).not.toHaveBeenCalled()
    expect(await within(section).findByText('Milestone removed')).toBeInTheDocument()
    fireEvent.click(within(section).getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(memberGoals.restoreKeyResult).toHaveBeenCalledWith('scout', 'scout', 'k00000001'))
    await waitFor(() => expect(within(section).queryByTestId('crew-goals-kr-undo')).toBeNull())
    expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(2)
  })

  it('says a milestone has no reading yet', async () => {
    memberGoals.get.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    expect(within(section).getByText('No reading yet')).toBeInTheDocument()
  })

  it('keeps every check-in switch disabled while there is no goal', async () => {
    memberGoals.get.mockResolvedValue(record({ long_term: '', key_results: [] }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-checkins')
    const switches = within(section).getAllByRole('switch')
    expect(switches).toHaveLength(3)
    for (const sw of switches) expect(sw).toHaveAttribute('aria-disabled', 'true')
  })

  it('shows the weekday and day-of-month pickers only under their switch while it is on', async () => {
    const off = record()
    memberGoals.get.mockResolvedValue(off)
    const { unmount } = renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    await screen.findByTestId('crew-goals-checkins')
    expect(screen.queryByRole('combobox', { name: 'Day of the week' })).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Day of the month' })).toBeNull()
    unmount()
    memberGoals.get.mockResolvedValue(record({ schedules: { ...off.schedules, weekly: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeEnabled()
    expect(screen.queryByRole('combobox', { name: 'Day of the month' })).toBeNull()
  })

  it('disables both pickers when there is no goal, whatever the switches say', async () => {
    const base = record()
    memberGoals.get.mockResolvedValue(record({ long_term: '', schedules: { ...base.schedules, weekly: true, monthly: true } }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    expect(await screen.findByRole('combobox', { name: 'Day of the week' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Day of the month' })).toBeDisabled()
  })

  it('keeps every draft when its save fails and clears it once a save lands', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setLongTerm.mockRejectedValueOnce(new Error('boom'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-edit'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'A new goal' } })
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    expect(await screen.findByTestId('crew-goals-save-error')).toBeInTheDocument()
    expect(screen.getByTestId('crew-goals-long-term-input')).toHaveValue('A new goal')
    memberGoals.setLongTerm.mockResolvedValueOnce(record({ long_term: 'A new goal' }))
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    await waitFor(() => expect(screen.queryByTestId('crew-goals-long-term-input')).toBeNull())
  })

  it('saves a key result edit against the set it started from, keyed by id', async () => {
    const a = { id: 'k0000000a', text: 'A', target: '1', current: '', updated_at: 0, readings: [] }
    const b = { id: 'k0000000b', text: 'B', target: '2', current: '', updated_at: 0, readings: [] }
    const c = { id: 'k0000000c', text: 'C', target: '3', current: '', updated_at: 0, readings: [] }
    memberGoals.get.mockResolvedValue(record({ key_results: [a, b, c] }))
    memberGoals.removeKeyResult.mockResolvedValue(record({ key_results: [b, c] }))
    memberGoals.setKeyResults.mockRejectedValue(new Error('changed_meanwhile'))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-key-results')
    fireEvent.click(within(within(section).getAllByTestId('goal-key-result')[1]).getAllByRole('button')[0])
    fireEvent.click(within(section).getByRole('button', { name: 'Edit milestone' }))
    fireEvent.change(within(section).getByDisplayValue('B'), { target: { value: 'B2' } })
    // A goes away underneath the open editor.
    fireEvent.click(within(within(section).getAllByTestId('goal-key-result')[0]).getAllByRole('button')[0])
    fireEvent.click(within(section).getByTestId('crew-goals-kr-remove'))
    await waitFor(() => expect(within(section).getAllByTestId('goal-key-result')).toHaveLength(1))
    // The editor stayed on B; C is still shown as it was.
    expect(within(section).getByDisplayValue('B2')).toBeInTheDocument()
    expect(within(section).getByText('C')).toBeInTheDocument()
    fireEvent.click(within(section).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(memberGoals.setKeyResults).toHaveBeenCalledTimes(1))
    const [, , items, expected] = memberGoals.setKeyResults.mock.calls[0]
    // Each item names its milestone by id, so the reworded B keeps its readings.
    expect(items).toEqual([
      { id: 'k0000000a', text: 'A', target: '1' },
      { id: 'k0000000b', text: 'B2', target: '2' },
      { id: 'k0000000c', text: 'C', target: '3' },
    ])
    expect(expected).toEqual([{ text: 'A', target: '1' }, { text: 'B', target: '2' }, { text: 'C', target: '3' }])
  })

  it('saves the long-term goal against the text it started editing from', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setSchedules.mockResolvedValue(record({ long_term: 'Changed meanwhile' }))
    memberGoals.setLongTerm.mockResolvedValue(record({ long_term: 'Mine' }))
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    fireEvent.click(await screen.findByTestId('crew-goals-edit'))
    fireEvent.change(screen.getByTestId('crew-goals-long-term-input'), { target: { value: 'Mine' } })
    // Another save lands meanwhile and the record now reads differently.
    fireEvent.click(within(screen.getByTestId('crew-goals-checkins')).getByRole('switch', { name: 'Weekly report' }))
    await waitFor(() => expect(memberGoals.setSchedules).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTestId('crew-goals-long-term-save'))
    await waitFor(() => expect(memberGoals.setLongTerm).toHaveBeenCalledWith('scout', 'scout', 'Mine', 'Keep the release notes current'))
  })

  it('turns on one check-in at a time', async () => {
    memberGoals.get.mockResolvedValue(record())
    memberGoals.setSchedules.mockResolvedValue(record())
    renderWithProviders(<CrewGoalsPane slug="scout" member="scout" name="Scout" />)
    const section = await screen.findByTestId('crew-goals-checkins')
    fireEvent.click(within(section).getByRole('switch', { name: 'Weekly report' }))
    await waitFor(() => expect(memberGoals.setSchedules).toHaveBeenCalledWith('scout', 'scout', { weekly: true }))
  })
})
