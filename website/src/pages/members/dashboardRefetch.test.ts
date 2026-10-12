import { describe, expect, it } from 'vitest'
import {
  CONDUCTOR_PANEL_TEMPLATE,
  DASHBOARD_FALLBACK_REFETCH_MS,
  panelFallbackRefetch,
} from './dashboardRefetch'

describe('panelFallbackRefetch', () => {
  it('polls the conductor board on the dashboard floor', () => {
    expect(panelFallbackRefetch('lead', CONDUCTOR_PANEL_TEMPLATE)).toBe(DASHBOARD_FALLBACK_REFETCH_MS)
  })

  it('polls the conductor crew before any board exists', () => {
    expect(panelFallbackRefetch(CONDUCTOR_PANEL_TEMPLATE, undefined)).toBe(DASHBOARD_FALLBACK_REFETCH_MS)
  })

  it('leaves every other crewmate on push-only refresh', () => {
    expect(panelFallbackRefetch('security-worker', 'kirocrew-default')).toBe(false)
    expect(panelFallbackRefetch('security-worker', null)).toBe(false)
  })
})
