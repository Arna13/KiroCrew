/**
 * The agent open-file bridge (useAgentOpenFileBridge in
 * pages/chat/page/eventBridges.ts), driven together with the page's real
 * file-open handler (usePanelDocumentActions), so a marker reaches the panel
 * only through the same `/api/file-read` path a path-chip click uses.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

const fileRead = vi.hoisted(() => ({ fetch: vi.fn() }))
vi.mock('../utils/fileReadQuery', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/fileReadQuery')>()),
  fetchFileRead: fileRead.fetch,
}))

import { api } from '../api/client'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import { EXACT_REFUSED_STATUS } from '../utils/fileReadQuery'
import {
  AGENT_OPEN_FILE_APPLIED_PREFIX, AGENT_OPEN_FILE_APPLIED_MAX, agentOpenFilesEnabled, useAgentOpenFileBridge,
} from '../pages/chat/page/eventBridges'
import type { ChatMessage } from '../types'

const msg = (role: ChatMessage['role'], content: string): ChatMessage =>
  ({ role, content } as ChatMessage)
const marker = (path: string) => `<!-- kirocrew:open-file path="${path}" -->`
const okRead = (text: string) => ({ text, ok: true, status: 200, binary: false, truncated: false, redacted: false, lossy: false })
const refusedRead = (status: number) => ({ text: '', ok: false, status, binary: false, truncated: false, redacted: false, lossy: false })

interface Props { activeSlot: string | null; messages: ChatMessage[]; enabled: boolean | null }

function harness(initial: Partial<Props> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tabsCtl = { openFile: vi.fn() }
  const showActionError = vi.fn()
  const onOpened = vi.fn()
  const slotRef = { current: initial.activeSlot ?? 'slot-a' }
  const props: Props = { activeSlot: 'slot-a', messages: [], enabled: true, ...initial }
  const hook = renderHook((p: Props) => {
    const { openFile } = usePanelDocumentActions({
      tabsCtl: tabsCtl as never, slotRef, queryClient, showActionError, onOpened,
    })
    useAgentOpenFileBridge({ ...p, openFile })
  }, { initialProps: props })
  return { hook, tabsCtl, showActionError, onOpened, props }
}

beforeEach(() => {
  localStorage.clear()
  fileRead.fetch.mockReset()
  vi.spyOn(api, 'fileDiff').mockResolvedValue({} as never)
  delete (window as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles
})

describe('useAgentOpenFileBridge', () => {
  it('opens the marker path in the Files tab once per (slot, path)', async () => {
    fileRead.fetch.mockResolvedValue(okRead('# notes'))
    const messages = [msg('user', 'write a note'), msg('assistant', `done ${marker('/home/u/notes.md')}`)]
    const { hook, tabsCtl, onOpened, props } = harness({ messages })
    await waitFor(() => expect(tabsCtl.openFile).toHaveBeenCalledTimes(1))
    // The read and the diff both ask the gateway for an exact open.
    expect(fileRead.fetch).toHaveBeenCalledWith('/home/u/notes.md', undefined, { exact: true })
    expect(api.fileDiff).toHaveBeenCalledWith('/home/u/notes.md', { exact: true })
    expect(tabsCtl.openFile.mock.calls[0].slice(0, 3)).toEqual(['/home/u/notes.md', '# notes', 'slot-a'])
    // Held in its source view (the tab's `sourceOnly`) until the user picks Preview.
    expect(tabsCtl.openFile.mock.calls[0][3]).toMatchObject({ sourceOnly: true })
    expect(onOpened).toHaveBeenCalledTimes(1)
    expect(JSON.parse(localStorage.getItem(AGENT_OPEN_FILE_APPLIED_PREFIX + 'slot-a')!)).toEqual(['/home/u/notes.md'])
    // A new messages array carrying the same marker (a re-render, a streamed
    // row update) does not reopen it.
    hook.rerender({ ...props, messages: [...messages, msg('user', 'thanks')] })
    await act(async () => { await Promise.resolve() })
    expect(fileRead.fetch).toHaveBeenCalledTimes(1)
  })

  it('does not reopen after a reload, and opens a new path', async () => {
    fileRead.fetch.mockResolvedValue(okRead('body'))
    const first = harness({ messages: [msg('assistant', marker('/home/u/a.md'))] })
    await waitFor(() => expect(first.tabsCtl.openFile).toHaveBeenCalledTimes(1))
    first.hook.unmount()
    // Remount (a reload or a route change): the persisted applied key holds.
    const second = harness({ messages: [msg('assistant', marker('/home/u/a.md'))] })
    await act(async () => { await Promise.resolve() })
    expect(second.tabsCtl.openFile).not.toHaveBeenCalled()
    expect(fileRead.fetch).toHaveBeenCalledTimes(1)
    second.hook.rerender({ ...second.props, messages: [msg('assistant', marker('/home/u/a.md')), msg('assistant', marker('/home/u/b.md'))] })
    await waitFor(() => expect(second.tabsCtl.openFile).toHaveBeenCalledTimes(1))
    expect(second.tabsCtl.openFile.mock.calls[0][0]).toBe('/home/u/b.md')
  })

  it('keys the applied paths per slot', async () => {
    fileRead.fetch.mockResolvedValue(okRead('body'))
    const messages = [msg('assistant', marker('/home/u/a.md'))]
    const { hook, tabsCtl, props } = harness({ messages })
    await waitFor(() => expect(tabsCtl.openFile).toHaveBeenCalledTimes(1))
    hook.rerender({ ...props, activeSlot: 'slot-b', messages })
    await waitFor(() => expect(tabsCtl.openFile).toHaveBeenCalledTimes(2))
    expect(tabsCtl.openFile.mock.calls[1][2]).toBe('slot-b')
  })

  it('refuses relative and ~ paths before any read', async () => {
    for (const bad of ['notes.md', './notes.md', '~/notes.md', '~']) {
      const { hook, tabsCtl } = harness({ messages: [msg('assistant', marker(bad))] })
      await act(async () => { await Promise.resolve() })
      expect(tabsCtl.openFile, bad).not.toHaveBeenCalled()
      hook.unmount()
    }
    expect(fileRead.fetch).not.toHaveBeenCalled()
    expect(localStorage.getItem(AGENT_OPEN_FILE_APPLIED_PREFIX + 'slot-a')).toBeNull()
  })

  it('does nothing when the gateway does not open the path automatically', async () => {
    fileRead.fetch.mockResolvedValue(refusedRead(EXACT_REFUSED_STATUS))
    const handOff = vi.fn()
    window.addEventListener('kirocrew-file-open', handOff)
    try {
      const { tabsCtl, showActionError } = harness({ messages: [msg('assistant', marker('/home/u/notes.md'))] })
      await waitFor(() => expect(fileRead.fetch).toHaveBeenCalledTimes(1))
      await act(async () => { await Promise.resolve() })
      expect(tabsCtl.openFile).not.toHaveBeenCalled()
      expect(showActionError).not.toHaveBeenCalled()
      // Not handed to an IDE either, which would open the path by name.
      expect(handOff).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('kirocrew-file-open', handOff)
    }
  })

  it('opens only file kinds whose viewer makes no network request of its own', async () => {
    fileRead.fetch.mockResolvedValue(okRead('body'))
    // An HTML preview can load images and styles from other hosts as it
    // renders, and the other refused kinds are heavy viewers; each stays a click.
    for (const refused of ['/home/u/page.html', '/home/u/page.HTM', '/home/u/doc.pdf', '/home/u/deck.pptx',
      '/home/u/book.xlsx', '/home/u/clip.mp4', '/home/u/song.mp3', '/home/u/scene.excalidraw']) {
      const { hook, tabsCtl } = harness({ messages: [msg('assistant', marker(refused))] })
      await act(async () => { await Promise.resolve() })
      expect(tabsCtl.openFile, refused).not.toHaveBeenCalled()
      hook.unmount()
    }
    expect(fileRead.fetch).not.toHaveBeenCalled()
    expect(localStorage.getItem(AGENT_OPEN_FILE_APPLIED_PREFIX + 'slot-a')).toBeNull()
    for (const opened of ['/home/u/notes.md', '/home/u/notes.txt', '/home/u/main.py', '/home/u/data.json',
      '/home/u/rows.csv', '/home/u/chart.png', '/home/u/logo.svg']) {
      const { hook, tabsCtl } = harness({ messages: [msg('assistant', marker(opened))] })
      await waitFor(() => expect(tabsCtl.openFile, opened).toHaveBeenCalledTimes(1))
      hook.unmount()
    }
  })

  it('ignores markers in user text and tool output', async () => {
    const { tabsCtl } = harness({
      messages: [msg('assistant', 'ok'), msg('tool', marker('/home/u/t.md')), msg('user', marker('/home/u/u.md'))],
    })
    await act(async () => { await Promise.resolve() })
    expect(tabsCtl.openFile).not.toHaveBeenCalled()
    expect(fileRead.fetch).not.toHaveBeenCalled()
  })

  it('shows the existing read error for a refused path and does not retry', async () => {
    fileRead.fetch.mockResolvedValue(refusedRead(400))
    const messages = [msg('assistant', marker('/home/u/.ssh/id_rsa'))]
    const { hook, tabsCtl, showActionError, props } = harness({ messages })
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(showActionError.mock.calls[0][0]).toContain('/home/u/.ssh/id_rsa')
    expect(tabsCtl.openFile).not.toHaveBeenCalled()
    hook.rerender({ ...props, messages: [...messages] })
    hook.unmount()
    const again = harness({ messages })
    await act(async () => { await Promise.resolve() })
    expect(fileRead.fetch).toHaveBeenCalledTimes(1)
    expect(again.showActionError).not.toHaveBeenCalled()
  })

  it('does nothing while the setting is off or not yet known', async () => {
    fileRead.fetch.mockResolvedValue(okRead('body'))
    const messages = [msg('assistant', marker('/home/u/a.md'))]
    const off = harness({ messages, enabled: false })
    await act(async () => { await Promise.resolve() })
    expect(off.tabsCtl.openFile).not.toHaveBeenCalled()
    off.hook.unmount()
    const pending = harness({ messages, enabled: null })
    await act(async () => { await Promise.resolve() })
    expect(pending.tabsCtl.openFile).not.toHaveBeenCalled()
    expect(fileRead.fetch).not.toHaveBeenCalled()
    // Once the value settles on, the marker opens.
    pending.hook.rerender({ ...pending.props, enabled: true })
    await waitFor(() => expect(pending.tabsCtl.openFile).toHaveBeenCalledTimes(1))
  })

  it('opens nothing when the applied record cannot be written', async () => {
    fileRead.fetch.mockResolvedValue(okRead('body'))
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const { tabsCtl } = harness({ messages: [msg('assistant', marker('/home/u/a.md'))] })
      await act(async () => { await Promise.resolve() })
      expect(tabsCtl.openFile).not.toHaveBeenCalled()
      expect(fileRead.fetch).not.toHaveBeenCalled()
    } finally {
      setItem.mockRestore()
      warn.mockRestore()
    }
  })

  it('caps the remembered paths per slot', async () => {
    const key = AGENT_OPEN_FILE_APPLIED_PREFIX + 'slot-a'
    localStorage.setItem(key, JSON.stringify(Array.from({ length: AGENT_OPEN_FILE_APPLIED_MAX }, (_, i) => `/p/${i}.md`)))
    fileRead.fetch.mockResolvedValue(okRead('body'))
    const { tabsCtl } = harness({ messages: [msg('assistant', marker('/p/new.md'))] })
    await waitFor(() => expect(tabsCtl.openFile).toHaveBeenCalledTimes(1))
    const stored: string[] = JSON.parse(localStorage.getItem(key)!)
    expect(stored).toHaveLength(AGENT_OPEN_FILE_APPLIED_MAX)
    expect(stored[stored.length - 1]).toBe('/p/new.md')
    expect(stored[0]).toBe('/p/1.md')
  })
})

describe('agentOpenFilesEnabled', () => {
  it('is unknown while the config read is in flight', () => {
    expect(agentOpenFilesEnabled(undefined, false)).toBeNull()
  })
  it('follows the stored value, and reads an absent key as on', () => {
    expect(agentOpenFilesEnabled({ auto_open_agent_files: false }, false)).toBe(false)
    expect(agentOpenFilesEnabled({ auto_open_agent_files: true }, false)).toBe(true)
    expect(agentOpenFilesEnabled({}, false)).toBe(true)
  })
  it('resolves a failed read to the default', () => {
    expect(agentOpenFilesEnabled(undefined, true)).toBe(true)
  })
})
