/**
 * The source-only hold on a marker-opened Markdown file (the tab's
 * `sourceOnly`, set by the open-file bridge).
 *
 * The rendered preview runs a file's interactive widgets, so a file the agent
 * opened must not reach it until the user picks Preview: not on open, and not
 * on a remount (a reload restores the tab with the hold still set). The last
 * case proves the probe would see a widget: the same file without the hold
 * renders it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { forwardRef, useImperativeHandle } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PierreEditorHandle } from '../pierre'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreEditor: forwardRef<PierreEditorHandle, { file: { contents: string } }>(
    function PierreEditorStub({ file }, ref) {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }), [])
      return <div data-testid="pierre-editor" data-value={file.contents} />
    },
  ),
  PierreCode: ({ file }: { file: { contents: string } }) => (
    <div data-testid="pierre-code" data-value={file.contents} />
  ),
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))

// The widget frame itself needs blob URLs jsdom lacks; a stand-in records that
// the preview chose to mount one.
vi.mock('../components/WidgetFrame', () => ({ default: () => <div data-testid="widget-frame" /> }))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({}),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter><QueryClientProvider client={qc}>{children}</QueryClientProvider></MemoryRouter>
)

const NOTE = '# Notes\n\n<mcwidget title="Chart">\n<div id="c">chart</div>\n</mcwidget>\n\nDone.\n'

function mount(sourceOnly?: boolean, onSourceOnlyCleared?: () => void) {
  return render(
    <MarkdownPanel
      embedded
      filePath="/home/u/notes.md"
      content={NOTE}
      onContentChange={() => {}}
      onSave={async () => {}}
      onClose={() => {}}
      sourceOnly={sourceOnly}
      onSourceOnlyCleared={onSourceOnlyCleared}
    />,
    { wrapper },
  )
}

beforeEach(() => {
  qc.clear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null }, json: async () => ({}), text: async () => '',
  })))
})

describe('MarkdownPanel — a source-only Markdown file', () => {
  it('opens in the source editor, so no widget frame mounts', async () => {
    const cleared = vi.fn()
    mount(true, cleared)
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('widget-frame')).toBeNull()
    expect(cleared).not.toHaveBeenCalled()
  })

  it('stays in the source editor when it mounts again with the hold set', async () => {
    const first = mount(true)
    await screen.findByTestId('pierre-editor')
    first.unmount()
    mount(true)
    expect(await screen.findByTestId('pierre-editor')).toBeInTheDocument()
    expect(screen.queryByTestId('widget-frame')).toBeNull()
  })

  it('renders the preview, widget included, once the user picks Preview, and ends the hold', async () => {
    const cleared = vi.fn()
    mount(true, cleared)
    await screen.findByTestId('pierre-editor')
    fireEvent.click(screen.getAllByRole('button', { name: 'Preview' })[0])
    expect(await screen.findByTestId('widget-frame')).toBeInTheDocument()
    expect(cleared).toHaveBeenCalled()
  })

  it('renders the widget when the same file opens without the hold', async () => {
    mount()
    expect(await screen.findByTestId('widget-frame')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
  })

  it('switches an open preview to the source editor in the same render when the hold arrives', async () => {
    const cleared = vi.fn()
    const view = mount(false, cleared)
    expect(await screen.findByTestId('widget-frame')).toBeInTheDocument()
    view.rerender(
      <MarkdownPanel
        embedded
        filePath="/home/u/notes.md"
        content={NOTE}
        onContentChange={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
        sourceOnly
        onSourceOnlyCleared={cleared}
      />,
    )
    // Synchronous: the commit that carries the hold already shows the editor.
    expect(screen.queryByTestId('widget-frame')).toBeNull()
    expect(screen.getByTestId('pierre-editor')).toBeInTheDocument()
    expect(cleared).not.toHaveBeenCalled()
    // The hold still ends only on the user's Preview click.
    fireEvent.click(screen.getAllByRole('button', { name: 'Preview' })[0])
    expect(await screen.findByTestId('widget-frame')).toBeInTheDocument()
    expect(cleared).toHaveBeenCalledTimes(1)
  })
})
