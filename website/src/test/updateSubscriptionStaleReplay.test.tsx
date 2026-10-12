// A staged-update replay must not re-light the "Restart to update" pill for a
// build that is already installed.
//
// The Electron main process keeps the update lifecycle's lastState in memory,
// so a renderer reload that is NOT a fresh process — the gateway exec'ing
// itself on a restart — hands useUpdateSubscription a 'downloaded' seed from
// before the restart. If that restart landed on the staged version, the stage
// is already running; replaying it sets desktopUpdateAvailable=true and the
// top-bar pill shows "Restart to update" for a build with nothing left to
// install. That is the banner that "stays after the app restarts".
//
// Contract under test (useUpdateSubscription's getInfo replay path):
// - a 'downloaded' seed whose version is NEWER than the running build still
//   restores (a genuinely pending stage survives a reload)
// - a seed whose version EQUALS the running build does NOT restore (the
//   restart installed it)
// - a seed whose version is OLDER than the running build does NOT restore
// - a seed with no version, or an unorderable version, does NOT restore
// - a LIVE 'downloaded' event still sets the flag regardless of version (the
//   live path is unchanged; only the on-mount replay is gated)
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

import { store } from '../store'
import dashboardReducer, { setDesktopUpdateAvailable } from '../store/dashboardSlice'
import { useUpdateSubscription, type UpdateState } from '../hooks/useUpdateSubscription'

type AnyRecord = Record<string, unknown>

function Subscriber() {
  useUpdateSubscription()
  return null
}

/** Mount the hook with a bridge whose getInfo reports `runningVersion` and the
 *  given `lastState` seed. `onState` is left inert unless a test drives it. */
function mountReplay({
  runningVersion,
  lastState,
  onState = (() => () => {}) as (cb: (p: UpdateState) => void) => () => void,
}: {
  runningVersion: string
  lastState: AnyRecord | null
  onState?: (cb: (p: UpdateState) => void) => () => void
}) {
  const getInfo = vi.fn().mockResolvedValue({
    version: runningVersion,
    channel: 'stable',
    packaged: true,
    lastState,
  })
  ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
    onState,
    check: vi.fn().mockResolvedValue({ ok: true }),
    getInfo,
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <Subscriber />
      </QueryClientProvider>
    </Provider>,
  )
  return { getInfo }
}

/** Let the getInfo replay promise chain settle. */
async function settleReplay() {
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

describe('useUpdateSubscription staged-replay version gate', () => {
  afterEach(() => {
    cleanup()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    store.dispatch(setDesktopUpdateAvailable(false))
  })

  it('restores a staged build that is still newer than the running one', async () => {
    mountReplay({ runningVersion: '0.8.0', lastState: { state: 'downloaded', version: '0.8.1' } })
    await settleReplay()
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(true)
  })

  it('does NOT restore a stage equal to the running build (restart installed it)', async () => {
    mountReplay({ runningVersion: '0.8.0', lastState: { state: 'downloaded', version: '0.8.0' } })
    await settleReplay()
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
  })

  it('does NOT restore a stage older than the running build', async () => {
    mountReplay({ runningVersion: '0.8.1', lastState: { state: 'downloaded', version: '0.8.0' } })
    await settleReplay()
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
  })

  it('treats an insider-vs-release equal core correctly (prerelease < release)', async () => {
    // Running the 0.8.0 release; the staged 0.8.0-insider.4 is OLDER, so the
    // banner must not relight. (compareVersions: a release outranks its
    // prereleases.)
    mountReplay({ runningVersion: '0.8.0', lastState: { state: 'downloaded', version: '0.8.0-insider.4' } })
    await settleReplay()
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
  })

  it('does NOT restore when the seed carries no version', async () => {
    mountReplay({ runningVersion: '0.8.0', lastState: { state: 'downloaded' } })
    await settleReplay()
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
  })

  it('does NOT restore when the versions are unorderable', async () => {
    mountReplay({ runningVersion: 'unknown', lastState: { state: 'downloaded', version: '0.8.1' } })
    await settleReplay()
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
  })

  it('a LIVE downloaded event still sets the flag regardless of version', async () => {
    let push: ((p: UpdateState) => void) | null = null
    mountReplay({
      runningVersion: '0.8.0',
      lastState: { state: 'downloaded', version: '0.8.0' }, // replay would NOT restore
      onState: (cb) => { push = cb; return () => {} },
    })
    await settleReplay()
    // The gated replay left it off…
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
    // …but a live event is the unchanged path and still lights it.
    act(() => { push!({ state: 'downloaded', version: '0.8.0' }) })
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(true)
  })
})

describe('setDesktopUpdateAvailable reducer', () => {
  it('toggles the flag', () => {
    const initial = dashboardReducer(undefined, { type: '@@INIT' })
    expect(initial.desktopUpdateAvailable).toBe(false)
    const on = dashboardReducer(initial, setDesktopUpdateAvailable(true))
    expect(on.desktopUpdateAvailable).toBe(true)
    expect(dashboardReducer(on, setDesktopUpdateAvailable(false)).desktopUpdateAvailable).toBe(false)
  })
})
