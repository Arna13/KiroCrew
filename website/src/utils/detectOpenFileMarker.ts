import type { ChatMessage } from '../types'

/**
 * Detect the file the agent asked the side panel's Files tab to open.
 *
 * The signal is a hidden HTML comment the agent emits after it writes a file
 * the user should see: `<!-- kirocrew:open-file path="/abs/path/notes.md" -->`.
 * rehypeRaw drops HTML comments from the rendered message, so it is invisible,
 * like the `kirocrew:preview` marker it sits beside (see detectPreviewUrl).
 *
 * Deliberately narrow, because the result opens a panel without a click:
 *  - only the NEWEST assistant message is read. User text, tool output rows
 *    and every older assistant message are ignored, so neither a pasted marker
 *    nor a marker scrolled far up the thread can open anything;
 *  - a marker inside a fenced code block or an inline code span is text the
 *    user can SEE (quoted output, documentation), not a hidden directive, so it
 *    is ignored too;
 *  - the path must be absolute. A relative path would be resolved against the
 *    gateway's current project directory and `~` against the gateway's home, so
 *    both are refused here, before any read is attempted. So are shapes that
 *    can reach another host (a UNC or device root in any separator mix, the
 *    `/net` and `/afs` automount roots), `..` segments, control characters and
 *    over-long values.
 *
 * With several markers in the newest message, the last one wins. Returns the
 * path, or null when the newest assistant message carries no usable marker.
 * Whether the file may be read at all stays the read endpoint's decision: the
 * caller opens it through the same handler a click on a path chip uses.
 */

// `<!-- kirocrew:open-file path="..." -->`, tolerant of surrounding whitespace.
const MARKER_RE = /<!--\s*kirocrew:open-file\s+path="([^"]*)"\s*-->/gi

/** Longest path a marker may name; anything longer is refused, not truncated. */
export const OPEN_FILE_MARKER_MAX_PATH = 4096

/** A POSIX absolute path (`/x`), or a Windows drive-qualified one (`C:\x`, `C:/x`). */
const ABSOLUTE_RE = /^(?:\/|[A-Za-z]:[\\/])/
/** Two separators in a row at the root, in any mix: a UNC share or device path on Windows. */
const NETWORK_ROOT_RE = /^(?:[A-Za-z]:)?[\\/][\\/]/
const CONTROL_RE = /[\u0000-\u001f\u007f]/
/** Wildcards and the `?` of a Windows device or NT-namespace prefix (`\\?\`, `/??/`). */
const DEVICE_CHAR_RE = /[?*]/
/** Automount roots that reach an arbitrary host by name (`/net/<host>/…`, `/afs/<cell>/…`). */
const AUTOMOUNT_ROOTS = new Set(['net', 'afs'])

/**
 * Is `raw` a path an open-file marker may name? Absolute and in normal form
 * only: relative paths, `~` paths, a root of two separators in any mix
 * (`//host`, `/\host`, `\\host`, `C://x`), `?` and `*`, a `:` anywhere but a
 * drive letter's, any empty, `.` or `..` segment, the `/net` and `/afs`
 * automount roots, control characters, empty and over-long values are all
 * refused. Every refused shape either resolves against a directory the agent
 * did not name or can reach another host.
 */
export function isOpenableMarkerPath(raw: string): boolean {
  if (!raw || raw.length > OPEN_FILE_MARKER_MAX_PATH) return false
  if (raw !== raw.trim()) return false
  if (CONTROL_RE.test(raw) || DEVICE_CHAR_RE.test(raw)) return false
  if (!ABSOLUTE_RE.test(raw) || NETWORK_ROOT_RE.test(raw)) return false
  const drive = /^[A-Za-z]:/.test(raw)
  if (raw.indexOf(':', drive ? 2 : 0) !== -1) return false
  const segments = raw.slice(drive ? 3 : 1).split(/[\\/]/)
  // Normal form only: no empty, `.` or `..` segment, so the first segment IS the
  // directory under the root and the automount check below cannot be stepped
  // around (`/./net/…`, `/a/../net/…`, `//net/…`).
  if (segments.some(seg => seg === '' || seg === '.' || seg === '..')) return false
  if (!drive && AUTOMOUNT_ROOTS.has(segments[0].toLowerCase())) return false
  return true
}

/** `text` with fenced code blocks and inline code spans removed. */
function stripCode(text: string): string {
  const kept: string[] = []
  let fence: { ch: string; len: number } | null = null
  for (const line of text.split('\n')) {
    const m = /^ {0,3}(`{3,}|~{3,})/.exec(line)
    if (fence) {
      if (m && m[1][0] === fence.ch && m[1].length >= fence.len && line.trim() === m[1]) fence = null
      continue
    }
    if (m) { fence = { ch: m[1][0], len: m[1].length }; continue }
    kept.push(line)
  }
  return kept.join('\n').replace(/(`+)[\s\S]*?\1/g, '')
}

export function detectOpenFileMarker(messages: ChatMessage[]): string | null {
  let newest: ChatMessage | undefined
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') { newest = messages[i]; break }
  }
  if (!newest) return null
  const text = stripCode(newest.content || '')
  MARKER_RE.lastIndex = 0
  let last: string | null = null
  let m: RegExpExecArray | null
  while ((m = MARKER_RE.exec(text)) !== null) last = m[1]
  if (last === null) return null
  return isOpenableMarkerPath(last) ? last : null
}
