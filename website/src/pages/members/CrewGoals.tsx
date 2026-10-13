import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ChevronDown, ChevronRight, Goal, Pencil, Trash2, Undo2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type GoalKeyResult, type GoalSchedules, type MemberGoals } from '../../api/client'
import { Btn, IconButton, Input, Toggle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtRelative, fmtWeekday } from '../../i18n/format'
import { cn } from '../../lib/utils'

/** The query key every view of one crewmate's goal record shares. */
export const memberGoalsKey = (slug: string, member: string) => ['member-goals', slug, member] as const

export function useMemberGoals(slug: string, member: string, enabled = true) {
  return useQuery({
    queryKey: memberGoalsKey(slug, member),
    queryFn: () => api.memberGoals.get(slug, member),
    enabled: enabled && Boolean(slug && member),
  })
}

/** How far a reading is toward its target, 0..1, or null when it cannot be told.
 *  Numbers compare as numbers ("19" of "27"); a word target ("yes") is reached
 *  when the reading is that word. */
export function keyResultProgress(kr: Pick<GoalKeyResult, 'current' | 'target'>): number | null {
  if (!kr.current) return null
  const cur = parseFloat(kr.current)
  const tgt = parseFloat(kr.target)
  if (Number.isFinite(cur) && Number.isFinite(tgt) && tgt !== 0) return Math.max(0, Math.min(1, cur / tgt))
  return kr.current.trim().toLowerCase() === kr.target.trim().toLowerCase() ? 1 : 0
}

/** One card of the Goals tab, in the Profile tab's own language: a hairline
 *  rounded card with a small caps label. `flush` cards hold rows edge to edge. */
function Section({ title, children, testId, trailing, flush }: { title: string; children: ReactNode; testId: string; trailing?: ReactNode; flush?: boolean }) {
  return (
    <section className={cn('rounded-2xl border border-border bg-bg', flush ? 'overflow-hidden' : 'px-3.5 pt-3.5 pb-3')} data-testid={testId}>
      {/* The label sits 14px from the top and from the left alike: the row is
          exactly the label's own line, and a trailing control hangs into the
          padding instead of growing the row. */}
      <div className={cn('flex items-center gap-2 h-3', flush ? 'mx-3.5 mt-3.5 mb-2.5' : 'mb-2.5')}>
        <div className="flex-1 text-[11px] leading-none font-semibold tracking-wide uppercase text-muted">{title}</div>
        {trailing && <div className="-my-2 flex items-center">{trailing}</div>}
      </div>
      {children}
    </section>
  )
}

/** The icon tile every row starts with (the Profile tab's Row/Tile tile). */
function RowTile({ children, tone }: { children: ReactNode; tone: 'accent' | 'info' | 'ok' | 'warn' | 'quiet' }) {
  const cls = {
    accent: 'bg-accent-subtle text-accent',
    info: 'bg-info-subtle text-info',
    ok: 'bg-ok-subtle text-ok',
    warn: 'bg-warn-subtle text-warn',
    quiet: 'bg-bg-hover text-muted',
  }[tone]
  return <span className={cn('w-8 h-8 rounded-[9px] grid place-items-center shrink-0', cls)} aria-hidden="true">{children}</span>
}

/** One key result as a row: what it is, the crewmate's latest reading in its
 *  own words, the target. No bar: a reading is a sentence or a number, never a
 *  percentage the record does not hold. Opening it shows every reading. */
function KeyResultRow({ kr, open, onToggle, children }: { kr: GoalKeyResult; open: boolean; onToggle: () => void; children?: ReactNode }) {
  const { t } = useTranslation()
  const readings = [...(kr.readings ?? [])].reverse().slice(1)
  const panelId = useId()
  return (
    <li className="border-t border-border" data-testid="goal-key-result">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
        className="flex items-start gap-3 w-full px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer focus-ring"
      >
        <span className="flex-1 min-w-0">
          <span className="block text-[13px] font-semibold text-text leading-snug">{kr.text}</span>
          {kr.current
            ? <span className="block mt-0.5 text-[13px] text-text leading-snug whitespace-pre-wrap">{kr.current}</span>
            : <span className="block mt-0.5 text-[13px] text-muted">{t('pages.membersPage.goals_kr_not_read')}</span>}
          <span className="block mt-1 text-[12px] text-muted leading-snug">
            {t('pages.membersPage.goals_kr_target')} · {kr.target}
            {kr.updated_at ? <> · {fmtRelative(kr.updated_at * 1000)}</> : null}
          </span>
        </span>
        <ChevronDown size={15} aria-hidden="true" className={cn('mt-2 text-muted shrink-0 transition-transform duration-200 motion-reduce:transition-none', open && 'rotate-180')} />
      </button>
      <div id={panelId} hidden={!open} className="px-3.5 pb-3">
        {open && (
          <>
            {readings.length > 0 && (
              <>
                <div className="text-[11px] font-semibold tracking-wide uppercase text-muted mb-1.5" aria-hidden="true">{t('pages.membersPage.goals_kr_history')}</div>
                <ol className="relative flex flex-col gap-2 border-l border-border pl-3 ml-0.5" aria-label={t('pages.membersPage.goals_kr_history')}>
                  {readings.map((r, i) => (
                    <li key={r.at + ':' + i} className="relative">
                      <span className="absolute -left-[17px] top-1.5 w-2 h-2 rounded-full bg-border" aria-hidden="true" />
                      <span className="block text-[12.5px] text-text whitespace-pre-wrap">{r.value}</span>
                      <span className="block text-[11.5px] text-muted">{fmtRelative(r.at * 1000)}</span>
                    </li>
                  ))}
                </ol>
              </>
            )}
            {children}
          </>
        )}
      </div>
    </li>
  )
}

function Reports({ goals, limit }: { goals: MemberGoals; limit: number }) {
  const { t } = useTranslation()
  if (!goals.reports.length) return <p className="px-3.5 pb-3 text-[13px] text-muted">{t('pages.membersPage.goals_reports_none')}</p>
  return (
    <ul data-testid="goal-reports">
      {goals.reports.slice(0, limit).map(r => (
        <li key={r.slug} className="border-t border-border">
          <Link to={'/artifacts/' + encodeURIComponent(r.slug)} className="flex items-center gap-3 px-3.5 py-2.5 hover:bg-bg-hover transition-colors focus-ring">
            <span className="flex-1 min-w-0 leading-tight">
              <span className="block text-[13px] font-semibold text-text truncate">{r.title}</span>
              <span className="block text-[12px] text-muted">
                {t(r.kind === 'weekly' ? 'pages.membersPage.goals_report_weekly' : 'pages.membersPage.goals_report_monthly')} · {fmtRelative(r.at * 1000)}
              </span>
            </span>
            <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
          </Link>
        </li>
      ))}
    </ul>
  )
}

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0] as const
const MONTH_DAYS = Array.from({ length: 28 }, (_, i) => i + 1)

/** The crewmate's Goals tab: the long-term goal, its key results (shown as
 *  Milestones), the three check-in switches and the saved reports. What the
 *  crewmate is doing right now is its task list, not part of the goal. */
export default function CrewGoalsPane({ slug, member, name }: { slug: string; member: string; name: string }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const q = useMemberGoals(slug, member)
  const goals = q.data
  // Each draft remembers what it was started from: its save compares against
  // that (the routes' compare-and-set), never against whatever the list holds by
  // the time Save is pressed, so a change made meanwhile is refused, not overwritten.
  const [editing, setEditing] = useState<{ text: string; expected: string } | null>(null)
  const [krEdit, setKrEdit] = useState<{
    id: string
    text: string
    target: string
    /** The whole set as it read when editing began, ids included. */
    base: { id: string; text: string; target: string }[]
  } | null>(null)
  const [openKr, setOpenKr] = useState<string | null>(null)
  const timeId = useId()
  const save = useMutation({
    mutationFn: (fn: () => Promise<MemberGoals>) => fn(),
    onSuccess: data => { qc.setQueryData(memberGoalsKey(slug, member), data) },
  })
  // `onSaved` clears a draft only once the save landed: a failed save keeps
  // what was typed so the user can try again.
  const run = (fn: () => Promise<MemberGoals>, onSaved?: () => void) =>
    save.mutate(fn, { onSuccess: () => onSaved?.() })

  if (q.isError) return <ErrorNotice message={t('pages.membersPage.goals_load_failed')} askAgent testId="crew-goals-error" />
  if (!goals) return <p role="status" className="text-[13px] text-muted">{t('pages.membersPage.goals_loading')}</p>

  const hasGoal = Boolean(goals.long_term)
  const removedKr = goals.removed_key_result
  const setSchedule = (changes: Partial<GoalSchedules>) => run(() => api.memberGoals.setSchedules(slug, member, changes))

  const krActions = (kr: GoalKeyResult) => (
    <div className="flex flex-wrap gap-2 justify-end mt-2.5">
      <Btn type="button" onClick={() => setKrEdit({ id: kr.id, text: kr.text, target: kr.target, base: goals.key_results.map(k => ({ id: k.id, text: k.text, target: k.target })) })} className="h-7 px-2.5 text-[12px] whitespace-nowrap gap-1">
        <Pencil size={12} aria-hidden="true" />
        {t('pages.membersPage.goals_kr_edit')}
      </Btn>
      <Btn type="button" onClick={() => run(() => api.memberGoals.removeKeyResult(slug, member, kr.id))} className="h-7 px-2.5 text-[12px] whitespace-nowrap gap-1 text-danger" data-testid="crew-goals-kr-remove">
        <Trash2 size={12} aria-hidden="true" />
        {t('pages.membersPage.goals_kr_remove')}
      </Btn>
    </div>
  )
  const checkRow = (opts: { label: string; checked: boolean; onChange: (v: boolean) => void; extra?: ReactNode }) => (
    <div className="border-t border-border px-3.5 py-2.5">
      <div className="flex items-center gap-3">
        <span className={cn('flex-1 min-w-0 text-[13px]', hasGoal ? 'text-text' : 'text-muted')}>{opts.label}</span>
        <Toggle checked={opts.checked} disabled={!hasGoal} onChange={opts.onChange} label={opts.label} />
      </div>
      {opts.checked && opts.extra && <div className="mt-2">{opts.extra}</div>}
    </div>
  )
  const anyCheckIn = goals.schedules.daily || goals.schedules.weekly || goals.schedules.monthly

  return (
    <div className="flex flex-col gap-3" data-testid="crew-goals-pane">
      {/* No hand-off: the drafts on this tab (the long-term goal being edited,
          a key result being edited) stay in their fields after a failed save,
          so the user retries here. */}
      {save.isError && <ErrorNotice message={t('pages.membersPage.goals_save_failed')} testId="crew-goals-save-error" />}

      {editing === null && !hasGoal ? (
        <section className="rounded-2xl border border-dashed border-border px-4 py-5 flex flex-col items-center gap-2.5 text-center" data-testid="crew-goals-long-term">
          <RowTile tone="accent"><Goal size={17} /></RowTile>
          <p className="text-[13px] text-muted max-w-[30ch]">{t('pages.membersPage.goals_none', { name })}</p>
          <Btn type="button" primary onClick={() => setEditing({ text: '', expected: goals.long_term })} className="gap-1 h-8" data-testid="crew-goals-edit">
            <Pencil size={13} aria-hidden="true" />{t('pages.membersPage.goals_write_goal')}
          </Btn>
        </section>
      ) : (
        <Section
          title={t('pages.membersPage.goals_long_term')}
          testId="crew-goals-long-term"
          trailing={editing === null && (
            <IconButton variant="accent" onClick={() => setEditing({ text: goals.long_term, expected: goals.long_term })} className="w-7 h-7 -mr-1.5 grid place-items-center shrink-0" aria-label={t('pages.membersPage.goals_edit_long_term')} title={t('pages.membersPage.goals_edit_long_term')} data-testid="crew-goals-edit">
              <Pencil size={13} aria-hidden="true" />
            </IconButton>
          )}
        >
          {editing !== null ? (
            <div className="flex flex-col gap-2">
              <textarea
                value={editing.text}
                onChange={e => setEditing({ ...editing, text: e.target.value })}
                maxLength={goals.limits.long_term_max_chars}
                rows={3}
                aria-label={t('pages.membersPage.goals_long_term')}
                className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
                data-testid="crew-goals-long-term-input"
              />
              <div className="flex gap-2 justify-end">
                <Btn type="button" onClick={() => setEditing(null)}>{t('pages.membersPage.create_cancel')}</Btn>
                <Btn type="button" primary onClick={() => { const { text, expected } = editing; run(() => api.memberGoals.setLongTerm(slug, member, text, expected), () => setEditing(null)) }} data-testid="crew-goals-long-term-save">
                  {t('pages.membersPage.goals_save')}
                </Btn>
              </div>
            </div>
          ) : (
            <p className="text-[13px] leading-relaxed text-text whitespace-pre-wrap" data-testid="crew-goals-long-term-text">{goals.long_term}</p>
          )}
        </Section>
      )}

      <Section
        flush
        title={t('pages.membersPage.goals_key_results')}
        testId="crew-goals-key-results"
        trailing={goals.key_results.length > 0 ? <span className="text-[12px] text-muted tabular-nums">{goals.key_results.length}</span> : null}
      >
        {removedKr && (
          <div className="flex items-center gap-2 mx-3.5 mb-2 rounded-lg bg-bg-hover px-2.5 py-1.5 text-[12px] text-muted" data-testid="crew-goals-kr-undo-row">
            <span className="flex-1 min-w-0 truncate" title={removedKr.text}>{t('pages.membersPage.goals_kr_removed')}</span>
            <Btn type="button" onClick={() => run(() => api.memberGoals.restoreKeyResult(slug, member, removedKr.id))} className="gap-1 h-7 px-2" data-testid="crew-goals-kr-undo">
              <Undo2 size={12} aria-hidden="true" />{t('pages.membersPage.goals_undo')}
            </Btn>
          </div>
        )}
        {goals.key_results.length === 0 ? (
          <p className="px-3.5 pb-3 text-[13px] text-muted">{t('pages.membersPage.goals_kr_empty', { name })}</p>
        ) : (
          <ul>
            {goals.key_results.map(kr => krEdit?.id === kr.id ? (
              <li key={kr.id} className="border-t border-border px-3.5 py-3 flex flex-col gap-1.5">
                <label className="flex flex-col gap-1 text-[12px] text-muted">
                  {t('pages.membersPage.goals_kr_text')}
                  <Input value={krEdit.text} maxLength={goals.limits.key_result_text_max_chars} onChange={e => setKrEdit({ ...krEdit, text: e.target.value })} />
                </label>
                <label className="flex flex-col gap-1 text-[12px] text-muted">
                  {t('pages.membersPage.goals_kr_target')}
                  <Input value={krEdit.target} maxLength={goals.limits.key_result_value_max_chars} onChange={e => setKrEdit({ ...krEdit, target: e.target.value })} />
                </label>
                <div className="flex gap-2 justify-end">
                  <Btn type="button" onClick={() => setKrEdit(null)}>{t('pages.membersPage.create_cancel')}</Btn>
                  <Btn type="button" primary onClick={() => {
                    const { base } = krEdit
                    // Each milestone keeps its id, so an edit to its wording keeps its readings.
                    const items = base.map(k => (k.id === krEdit.id ? { id: k.id, text: krEdit.text, target: krEdit.target } : { id: k.id, text: k.text, target: k.target }))
                    const expected = base.map(k => ({ text: k.text, target: k.target }))
                    run(() => api.memberGoals.setKeyResults(slug, member, items, expected), () => setKrEdit(null))
                  }}>{t('pages.membersPage.goals_save')}</Btn>
                </div>
              </li>
            ) : (
              <KeyResultRow key={kr.id} kr={kr} open={openKr === kr.id} onToggle={() => setOpenKr(v => (v === kr.id ? null : kr.id))}>
                {krActions(kr)}
              </KeyResultRow>
            ))}
          </ul>
        )}
      </Section>

      <Section flush title={t('pages.membersPage.goals_checkins')} testId="crew-goals-checkins">
        {!hasGoal && <p className="px-3.5 pb-2 text-[12px] text-muted">{t('pages.membersPage.goals_checkins_need_goal')}</p>}
        {checkRow({
          label: t('pages.membersPage.goals_daily'),
          checked: goals.schedules.daily, onChange: v => setSchedule({ daily: v }),
        })}
        {checkRow({
          label: t('pages.membersPage.goals_weekly'),
          checked: goals.schedules.weekly, onChange: v => setSchedule({ weekly: v }),
          extra: (
            <SimpleSelect
              options={WEEKDAYS.map(String)}
              optionLabels={WEEKDAYS.map(d => fmtWeekday(d === 0 ? 7 : d, 'long'))}
              value={String(goals.schedules.weekday)}
              onChange={v => setSchedule({ weekday: Number(v) })}
              disabled={!hasGoal || !goals.schedules.weekly}
              aria-label={t('pages.membersPage.goals_weekday')}
              className="h-8 w-44"
            />
          ),
        })}
        {checkRow({
          label: t('pages.membersPage.goals_monthly'),
          checked: goals.schedules.monthly, onChange: v => setSchedule({ monthly: v }),
          extra: (
            <SimpleSelect
              options={MONTH_DAYS.map(String)}
              optionLabels={MONTH_DAYS.map(day => t('pages.membersPage.goals_month_day_option', { day }))}
              value={String(goals.schedules.month_day)}
              onChange={v => setSchedule({ month_day: Number(v) })}
              disabled={!hasGoal || !goals.schedules.monthly}
              aria-label={t('pages.membersPage.goals_month_day')}
              className="h-8 w-44"
            />
          ),
        })}
        {anyCheckIn && (
          <label htmlFor={timeId} className="flex items-center gap-3 border-t border-border px-3.5 py-2.5 text-[13px] text-text">
            <span className="flex-1">{t('pages.membersPage.goals_time')}</span>
            <input
              id={timeId}
              type="time"
              aria-label={t('pages.membersPage.goals_time')}
              value={goals.schedules.time}
              onChange={e => { if (e.target.value) setSchedule({ time: e.target.value }) }}
              className="h-8 rounded-lg border border-border bg-bg px-2 text-[13px] text-text focus-ring"
              data-testid="crew-goals-time"
            />
          </label>
        )}
      </Section>

      <Section flush title={t('pages.membersPage.goals_reports')} testId="crew-goals-reports">
        <Reports goals={goals} limit={6} />
      </Section>
    </div>
  )
}
