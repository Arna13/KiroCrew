import { describe, it, expect } from 'vitest'

import { detectOpenFileMarker, isOpenableMarkerPath, OPEN_FILE_MARKER_MAX_PATH } from '../utils/detectOpenFileMarker'
import type { ChatMessage } from '../types'

const msg = (role: ChatMessage['role'], content: string): ChatMessage =>
  ({ role, content } as ChatMessage)
const marker = (path: string) => `<!-- kirocrew:open-file path="${path}" -->`

describe('detectOpenFileMarker', () => {
  it('returns the absolute path a marker in the newest assistant message names', () => {
    expect(detectOpenFileMarker([
      msg('user', 'write me a note'),
      msg('assistant', `Wrote the note.\n${marker('/home/u/notes/today.md')}\n`),
    ])).toBe('/home/u/notes/today.md')
  })

  it('accepts Windows drive-qualified paths', () => {
    expect(detectOpenFileMarker([msg('assistant', marker('C:\\Users\\u\\notes.md'))])).toBe('C:\\Users\\u\\notes.md')
    expect(detectOpenFileMarker([msg('assistant', marker('D:/work/notes.md'))])).toBe('D:/work/notes.md')
  })

  it('tolerates whitespace inside the comment', () => {
    expect(detectOpenFileMarker([msg('assistant', '<!--   kirocrew:open-file   path="/a/b.md"   -->')])).toBe('/a/b.md')
  })

  it('returns null without a marker, and ignores the preview marker', () => {
    expect(detectOpenFileMarker([msg('assistant', 'see /home/u/notes.md')])).toBeNull()
    expect(detectOpenFileMarker([msg('assistant', '<!-- kirocrew:preview url="http://127.0.0.1:8080" -->')])).toBeNull()
    expect(detectOpenFileMarker([])).toBeNull()
  })

  it('ignores a marker in user text', () => {
    expect(detectOpenFileMarker([
      msg('assistant', 'hello'),
      msg('user', marker('/etc/hosts')),
    ])).toBeNull()
    expect(detectOpenFileMarker([msg('user', marker('/etc/hosts'))])).toBeNull()
  })

  it('ignores a marker in a tool output row', () => {
    expect(detectOpenFileMarker([
      msg('assistant', 'running the tool'),
      msg('tool', marker('/home/u/secret.md')),
    ])).toBeNull()
  })

  it('reads only the newest assistant message', () => {
    expect(detectOpenFileMarker([
      msg('assistant', marker('/home/u/old.md')),
      msg('user', 'thanks'),
      msg('assistant', 'nothing to open this time'),
    ])).toBeNull()
  })

  it('ignores a marker the newest assistant message shows in code', () => {
    expect(detectOpenFileMarker([msg('assistant', `Emit this:\n\`\`\`\n${marker('/home/u/a.md')}\n\`\`\`\n`)])).toBeNull()
    expect(detectOpenFileMarker([msg('assistant', `Emit this:\n~~~html\n${marker('/home/u/a.md')}\n~~~`)])).toBeNull()
    expect(detectOpenFileMarker([msg('assistant', `Use \`${marker('/home/u/a.md')}\` to open a file.`)])).toBeNull()
    // A real marker after the fenced example still counts.
    expect(detectOpenFileMarker([msg('assistant', `\`\`\`\n${marker('/x.md')}\n\`\`\`\n${marker('/home/u/real.md')}`)])).toBe('/home/u/real.md')
  })

  it('lets the last marker in the message win', () => {
    expect(detectOpenFileMarker([msg('assistant', `${marker('/a.md')} then ${marker('/b.md')}`)])).toBe('/b.md')
  })

  it('refuses relative, ~, empty and network-share paths before any read', () => {
    for (const bad of ['notes.md', './notes.md', '../etc/passwd', 'sub/dir/notes.md', '~', '~/notes.md', '~root/x', '',
      '//host/share/x', '\\\\host\\share\\x', 'C:notes.md', 'C:\\\\host\\x']) {
      expect(detectOpenFileMarker([msg('assistant', marker(bad))]), bad).toBeNull()
    }
  })

  it('refuses every root that can reach another host, in any separator mix', () => {
    for (const bad of ['/\\attacker.example\\share\\note.md', '\\/attacker.example/share/note.md',
      '\\\\attacker.example\\share\\note.md', '//attacker.example/share/note.md', 'C:\\/host/share/x.md',
      'C:/\\host\\share\\x.md', 'C://host/share/x.md', '\\\\?\\C:\\notes.md', '\\\\.\\pipe\\x', '/??/UNC/host/share/x.md',
      '/net/attacker.example/export/note.md', '/NET/host/x.md', '/afs/cell.example/x.md', '/home/u/*.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
      expect(detectOpenFileMarker([msg('assistant', marker(bad))]), bad).toBeNull()
    }
  })

  it('refuses a path that is not in normal form, so no segment trick reaches an automount root', () => {
    for (const bad of ['/./net/attacker.example/export/note.md', '/srv/../net/host/x.md', '/srv/./notes.md',
      '/srv//notes.md', '/srv/notes/', 'C:\\Users\\.\\u\\notes.md', 'C:\\Users\\\\u\\notes.md', '/.\\net\\host\\x.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
    }
  })

  it('refuses a colon anywhere but a drive letter', () => {
    for (const bad of ['C:\\notes.md:hidden', '/home/u/notes.md:stream', 'C:\\a\\D:\\b.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
    }
  })

  it('keeps ordinary absolute paths that only look like the refused shapes', () => {
    for (const good of ['/home/u/notes.md', '/network/notes.md', '/srv/net/notes.md', '/srv/afs-notes/a.md',
      'C:\\Users\\u\\net\\notes.md', 'D:/work/notes.md']) {
      expect(isOpenableMarkerPath(good), good).toBe(true)
    }
  })

  it('refuses dot-dot segments, control characters, padding and over-long paths', () => {
    for (const bad of ['/home/u/../../etc/shadow', 'C:\\a\\..\\b', '/a\u0000b', '/a\nb', ' /a.md', '/a.md ',
      '/' + 'a'.repeat(OPEN_FILE_MARKER_MAX_PATH)]) {
      expect(isOpenableMarkerPath(bad), JSON.stringify(bad.slice(0, 40))).toBe(false)
    }
    expect(isOpenableMarkerPath('/home/u/..notes/a..md')).toBe(true)
  })

  it('refuses the whole marker when the last one names a bad path', () => {
    expect(detectOpenFileMarker([msg('assistant', `${marker('/good.md')} ${marker('relative.md')}`)])).toBeNull()
  })
})
