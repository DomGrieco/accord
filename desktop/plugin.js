import {
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  haptic,
  host
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'accord'
const LEGACY_PLUGIN_ID = 'human-gate'
const PLUGIN_API_IDS = [PLUGIN_ID, LEGACY_PLUGIN_ID]
const PLUGIN_DISPLAY_NAME = 'Accord'
const PLUGIN_ROUTE = '/accord'
const ACTIVE_STATES = 'pending,approved,changes_requested,denied,cancelled,claimed,executed,failed,uncertain'
const ACTIVE_CANCELLED_RESUME_STATES = new Set(['pending', 'dispatching', 'failed'])
const PROFILE_PROBE_PATH = '/requests?state=pending&limit=1'
let lastFocusedOwner = null
let lastFocusedSessionId = ''
let resolvedPluginScope = null
let pluginScopeResolution = null

function randomOwnerToken() {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
}

function text(value, fallback = '') {
  return String(value ?? fallback).trim()
}

export function rememberFocusedOwner(owner) {
  const profile = text(owner?.profile)
  if (!profile) return
  lastFocusedOwner = {
    connectionId: text(owner?.connectionId),
    profile
  }
}

export function rememberFocusedSession(sessionId) {
  const id = text(sessionId)
  if (id) lastFocusedSessionId = id
}

function resolveFocusedRuntimeId() {
  const live = text(host.state?.focusedSessionId?.get?.())
  rememberFocusedSession(live)
  return live || lastFocusedSessionId
}

function focusedApiScope() {
  const focusedOwner = host.state?.focusedSessionOwner?.get?.()
  rememberFocusedOwner(focusedOwner)
  const observedFocusedProfile = text(host.state?.focusedSessionProfile?.get?.())
  const activeProfile = text(host.state?.profile?.get?.(), 'default') || 'default'
  const activeConnectionId = text(host.state?.connectionId?.get?.())
  const rememberedOwner = lastFocusedOwner
  const rememberedMatchesFocus =
    rememberedOwner &&
    (!observedFocusedProfile || rememberedOwner.profile === observedFocusedProfile)
  const scopedOwner = focusedOwner?.profile
    ? focusedOwner
    : (rememberedMatchesFocus ? rememberedOwner : null)
  const focusedProfile = text(
    focusedOwner?.profile,
    observedFocusedProfile || scopedOwner?.profile || activeProfile
  ) || activeProfile
  const focusedConnectionId = text(scopedOwner?.connectionId)

  if (
    host.state?.focusedSessionOwner &&
    host.state?.focusedSessionProfile &&
    focusedProfile !== activeProfile &&
    !scopedOwner?.profile
  ) {
    throw new Error('The focused session owner could not be resolved.')
  }

  return {
    activeConnectionId,
    activeProfile,
    connectionId: focusedConnectionId || activeConnectionId,
    profile: focusedProfile
  }
}

function pluginApiSuffix(path) {
  const suffix = String(path || '').startsWith('/') ? String(path) : `/${String(path || '')}`
  const pathname = suffix.split(/[?#]/, 1)[0]
  if (pathname.split('/').includes('..')) {
    throw new Error(`${PLUGIN_DISPLAY_NAME} API path traversal rejected: ${path}`)
  }
  return suffix
}

async function restAtPlugin(scope, path, options, pluginId) {
  const request = {
    path: `/api/plugins/${pluginId}${pluginApiSuffix(path)}`,
    profile: scope.profile
  }
  if (scope.connectionId) request.connectionId = scope.connectionId
  if (options.method !== undefined) request.method = options.method
  if (options.body !== undefined) request.body = options.body
  if (options.upload !== undefined) request.upload = options.upload
  if (options.timeoutMs !== undefined) request.timeoutMs = options.timeoutMs
  return window.hermesDesktop.api(request)
}

async function restAtScope(ctx, scope, path, options = {}) {
  const connectionMatches =
    !scope.connectionId ||
    scope.connectionId === scope.activeConnectionId ||
    (scope.connectionId === 'local' && !scope.activeConnectionId)

  if (scope.profile === scope.activeProfile && connectionMatches) {
    try {
      return await ctx.rest(path, options)
    } catch (cause) {
      if (!pluginNotFound(cause)) throw cause
    }
  }

  if (!window.hermesDesktop?.api) {
    throw new Error('Hermes Desktop API bridge unavailable for the focused profile.')
  }

  let lastError
  for (const pluginId of PLUGIN_API_IDS) {
    try {
      return await restAtPlugin(scope, path, options, pluginId)
    } catch (cause) {
      lastError = cause
      if (pluginNotFound(cause)) continue
      throw cause
    }
  }
  throw lastError
}

function pluginNotFound(cause) {
  const message = cause instanceof Error ? cause.message : String(cause)
  return message.includes('404') && message.toLowerCase().includes('plugin not found')
}

function httpNotFound(cause) {
  const message = cause instanceof Error ? cause.message : String(cause)
  return /(^|\D)404(\D|$)/.test(message)
}

function sameApiScope(left, right) {
  return left.profile === right.profile && left.connectionId === right.connectionId
}

async function discoverPluginScope(ctx, rejectedScope) {
  if (resolvedPluginScope) return resolvedPluginScope
  if (pluginScopeResolution) return pluginScopeResolution

  pluginScopeResolution = (async () => {
    if (typeof host.profileRoutes !== 'function') {
      throw new Error('Accord could not inspect Hermes profile routes.')
    }

    const routes = await host.profileRoutes()
    if (!Array.isArray(routes)) {
      throw new Error('Hermes returned an invalid profile route list for Accord.')
    }

    const seen = new Set()
    const candidates = []
    for (const route of routes) {
      const profile = text(route?.targetProfile, route?.profile)
      if (!profile) continue
      const candidate = {
        activeConnectionId: rejectedScope.activeConnectionId,
        activeProfile: rejectedScope.activeProfile,
        connectionId: text(route?.connectionId),
        profile
      }
      const key = `${candidate.connectionId}\u0000${candidate.profile}`
      if (seen.has(key) || sameApiScope(candidate, rejectedScope)) continue
      seen.add(key)
      candidates.push(candidate)
    }

    const matches = []
    for (const candidate of candidates) {
      try {
        await restAtScope(ctx, candidate, PROFILE_PROBE_PATH)
        matches.push(candidate)
      } catch (cause) {
        if (!httpNotFound(cause)) throw cause
      }
    }

    if (matches.length !== 1) {
      throw new Error(
        matches.length === 0
          ? 'Accord is not enabled in any available Hermes profile.'
          : 'Accord is enabled in more than one Hermes profile. Select its owning profile first.'
      )
    }
    resolvedPluginScope = matches[0]
    return resolvedPluginScope
  })()

  try {
    return await pluginScopeResolution
  } finally {
    pluginScopeResolution = null
  }
}

export async function profileRest(ctx, path, options = {}) {
  const scope = focusedApiScope()
  try {
    return await restAtScope(ctx, scope, path, options)
  } catch (cause) {
    if (!pluginNotFound(cause)) throw cause
  }

  const pluginScope = await discoverPluginScope(ctx, scope)
  return restAtScope(ctx, pluginScope, path, options)
}

function trackFocusedOwner(ctx) {
  const ownerAtom = host.state?.focusedSessionOwner
  rememberFocusedOwner(ownerAtom?.get?.())
  const stopOwner = ownerAtom?.listen?.(rememberFocusedOwner)
  const sessionAtom = host.state?.focusedSessionId
  rememberFocusedSession(sessionAtom?.get?.())
  const stopSession = sessionAtom?.listen?.(rememberFocusedSession)
  if (typeof ctx.onDispose === 'function') {
    if (typeof stopOwner === 'function') ctx.onDispose(stopOwner)
    if (typeof stopSession === 'function') ctx.onDispose(stopSession)
  }
}

async function ensureOwner(ctx) {
  let token = ctx.storage.get('ownerToken', '')
  if (!token) {
    token = randomOwnerToken()
    ctx.storage.set('ownerToken', token)
  }
  await profileRest(ctx, '/owner/register', {
    method: 'POST',
    body: { token }
  }).catch(cause => {
    const message = cause instanceof Error ? cause.message : String(cause)
    if (/owner already registered/i.test(message)) {
      throw new Error(
        'Accord could not bind this Desktop window to the existing inbox owner token. The Human Gate owner binding is still in the approval database.'
      )
    }
    throw cause
  })
  return token
}

function useRequests(ctx, states = ACTIVE_STATES) {
  const [requests, setRequests] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const result = await profileRest(ctx, `/requests?state=${encodeURIComponent(states)}&limit=200`)
      const fetched = Array.isArray(result?.requests) ? result.requests : []
      setRequests(states === ACTIVE_STATES ? activeInboxRequests(fetched) : fetched)
      setError('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [ctx, states])

  useEffect(() => {
    let active = true
    let timer
    const schedule = () => {
      if (!active) return
      timer = setTimeout(() => {
        void refresh().finally(schedule)
      }, 4000)
    }
    void refresh().finally(schedule)
    const dispose = host.onEvent('*', event => {
      const type = String(event?.type || '')
      if (type === 'tool.end' || type === 'session.updated' || type === 'gateway.ready') {
        void refresh()
      }
    })
    return () => {
      active = false
      clearTimeout(timer)
      dispose()
    }
  }, [refresh])

  return { requests, loading, error, refresh }
}

function stateClass(state) {
  if (state === 'pending') return 'bg-(--ui-warning-bg) text-(--ui-warning)'
  if (state === 'approved' || state === 'executed') return 'bg-(--ui-success-bg) text-(--ui-success)'
  if (state === 'denied' || state === 'failed' || state === 'uncertain') {
    return 'bg-(--ui-danger-bg) text-(--ui-danger)'
  }
  return 'bg-(--ui-surface-secondary) text-(--ui-text-secondary)'
}

function StateBadge({ state }) {
  return jsx('span', {
    className: `rounded px-1.5 py-0.5 text-[0.6875rem] font-medium ${stateClass(state)}`,
    children: state.replaceAll('_', ' ')
  })
}

export function approvalCancellationLabel(state) {
  if (state === 'approved') return 'Revoke approval'
  if (state === 'changes_requested') return 'Cancel request'
  return ''
}

export function activeInboxRequests(requests) {
  if (!Array.isArray(requests)) return []
  return requests.filter(request =>
    request?.state !== 'cancelled' || ACTIVE_CANCELLED_RESUME_STATES.has(request?.resume_state)
  )
}

function approvalSearchText(request) {
  const display = Object.values(request?.display || {})
    .map(value => typeof value === 'string' ? value : JSON.stringify(value))
    .join(' ')
  return [
    request?.id,
    request?.tool_name,
    request?.profile,
    request?.stored_session_id,
    request?.session_lineage,
    request?.effect_kind,
    request?.state,
    request?.resume_state,
    display
  ].map(value => String(value || '').toLowerCase()).join(' ')
}

export function filterAndSortRequests(requests, options = {}) {
  if (!Array.isArray(requests)) return []
  const query = String(options.query || '').trim().toLowerCase()
  const state = String(options.state || 'all')
  const effect = String(options.effect || 'all')
  const sort = String(options.sort || 'newest')
  const filtered = requests.filter(request =>
    (!query || approvalSearchText(request).includes(query)) &&
    (state === 'all' || request?.state === state) &&
    (effect === 'all' || request?.effect_kind === effect)
  )
  return filtered.slice().sort((left, right) => {
    if (sort === 'oldest') {
      return String(left?.created_at || '').localeCompare(String(right?.created_at || ''))
    }
    if (sort === 'tool') {
      return String(left?.tool_name || '').localeCompare(String(right?.tool_name || '')) ||
        String(right?.created_at || '').localeCompare(String(left?.created_at || ''))
    }
    if (sort === 'state') {
      return String(left?.state || '').localeCompare(String(right?.state || '')) ||
        String(right?.created_at || '').localeCompare(String(left?.created_at || ''))
    }
    return String(right?.created_at || '').localeCompare(String(left?.created_at || ''))
  })
}

export function retrySessionWakeLabel(request) {
  return request?.resume_state === 'failed' ? 'Retry session wake' : ''
}

function DisplayProjection({ display }) {
  const entries = Object.entries(display || {})
  if (!entries.length) {
    return jsx('div', {
      className: 'text-xs text-(--ui-text-tertiary)',
      children: 'No display metadata was stored.'
    })
  }
  return jsx('dl', {
    className: 'grid gap-2',
    children: entries.map(([key, value]) =>
      jsxs('div', {
        className: 'grid gap-0.5',
        children: [
          jsx('dt', {
            className: 'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)',
            children: key.replaceAll('_', ' ')
          }),
          jsx('dd', {
            className: 'whitespace-pre-wrap break-words text-sm text-(--ui-text-primary)',
            children: typeof value === 'string' ? value : JSON.stringify(value, null, 2)
          })
        ]
      }, key)
    )
  })
}

async function profileRoute(profile) {
  const routes = await host.profileRoutes()
  const matches = (Array.isArray(routes) ? routes : []).filter(route =>
    String(route?.targetProfile || route?.profile || '') === profile
  )
  if (matches.length !== 1) {
    throw new Error(`Expected one route for profile ${profile}, found ${matches.length}.`)
  }
  return matches[0]
}

function isSessionLimitRejection(cause) {
  const seen = new Set()
  let current = cause
  for (let depth = 0; current && depth < 6 && !seen.has(current); depth += 1) {
    if (typeof current === 'object') seen.add(current)
    const code = Number(current?.code ?? current?.data?.code)
    const message = current instanceof Error ? current.message : String(current?.message ?? current)
    if (code === 4090 || /active session limit\s*\(\s*\d+\s*\/\s*\d+\s*\)/i.test(message)) {
      return true
    }
    current = current?.cause ?? current?.data?.cause
  }
  return false
}

export async function submitResume(ctx, resume, token) {
  let release
  let wakeAttempted = false
  let resumeResponded = false
  let resumeAccepted = false
  try {
    const prompt = typeof resume?.prompt === 'string' ? resume.prompt.trim() : ''
    if (!prompt) {
      throw new Error('Accord refused to deliver an empty decision prompt; a nonempty decision prompt is required.')
    }
    const route = await profileRoute(resume.profile)
    release = await host.retainProfile(route)
    wakeAttempted = true
    const resumed = await host.requestProfile(route, 'session.resume', {
      session_id: resume.stored_session_id,
      profile: route.targetProfile,
      omit_messages: true
    })
    resumeResponded = true
    const runtimeSessionId = String(resumed?.session_id || '').trim()
    const storedSessionId = String(resumed?.session_key || '').trim()
    const resolvedStoredSessionId = String(resumed?.resumed || '').trim()
    const requestedStoredSessionId = String(resumed?.requested_session_id || '').trim()
    const expectedStoredSessionId = String(resume?.stored_session_id || '').trim()
    if (!runtimeSessionId) {
      throw new Error('The stored session resumed without an active runtime.')
    }
    if (!storedSessionId) {
      throw new Error('The resumed runtime did not prove the stored session lineage.')
    }
    if (!resolvedStoredSessionId) {
      throw new Error('The resumed runtime did not identify its resolved stored session.')
    }
    if (resolvedStoredSessionId !== storedSessionId) {
      throw new Error('The resumed runtime returned conflicting stored session identities.')
    }
    if (!requestedStoredSessionId) {
      throw new Error('The resumed runtime did not identify the requested stored session.')
    }
    if (!expectedStoredSessionId || requestedStoredSessionId !== expectedStoredSessionId) {
      throw new Error('Hermes resumed a different stored session than requested.')
    }
    await profileRest(ctx, `/requests/${resume.request_id}/resume-target`, {
      method: 'POST',
      body: { token, record_version: resume.record_version, session_id: storedSessionId }
    })
    resumeAccepted = true
    await host.requestProfile(route, 'prompt.submit', {
      session_id: runtimeSessionId,
      text: prompt,
      display_kind: 'hidden'
    })
    await profileRest(ctx, `/requests/${resume.request_id}/resume-ack`, {
      method: 'POST',
      body: { token, record_version: resume.record_version }
    })
  } catch (cause) {
    const causeMessage = cause instanceof Error ? cause.message : String(cause)
    const preAcceptFailure =
      !wakeAttempted || (resumeResponded && !resumeAccepted) || isSessionLimitRejection(cause)
    if (preAcceptFailure) {
      try {
        await profileRest(ctx, `/requests/${resume.request_id}/resume-failed`, {
          method: 'POST',
          body: {
            token,
            record_version: resume.record_version,
            error: causeMessage || 'Hermes rejected the resume before accepting the decision prompt.'
          }
        })
      } catch (recordCause) {
        const message = recordCause instanceof Error ? recordCause.message : String(recordCause)
        throw new Error(`Session wake did not start, but retry state could not be recorded. ${message}`, {
          cause
        })
      }
    }
    throw cause
  } finally {
    if (release) release()
  }
}

export async function submitDecisionAndResume({ ctx, request, decision, comment = '', ownerToken }) {
  const result = await profileRest(ctx, `/requests/${request.id}/decision`, {
    method: 'POST',
    body: {
      token: ownerToken,
      decision,
      comment,
      digest: request.call_digest,
      record_version: request.record_version
    }
  })
  const instruction = await profileRest(ctx, `/requests/${request.id}/resume-instruction`, {
    method: 'POST',
    body: { token: ownerToken }
  })
  await submitResume(ctx, instruction.resume, ownerToken)
  return result
}

export function selectRuntimeToClose(active, storedSessionId) {
  if (!active || !Array.isArray(active.sessions)) {
    throw new Error('Hermes returned no valid sessions array.')
  }
  for (const row of active.sessions) {
    if (
      !row ||
      typeof row.id !== 'string' ||
      !row.id ||
      typeof row.session_key !== 'string' ||
      !row.session_key
    ) {
      throw new Error('Hermes returned a session row without a valid id and session_key.')
    }
  }
  const matches = active.sessions.filter(row => row.session_key === storedSessionId)
  if (matches.length > 1) {
    throw new Error('More than one live runtime matched the stored session lineage.')
  }
  if (!matches.length) return null
  const runtimeSessionId = String(matches[0]?.id || '')
  if (!runtimeSessionId) {
    throw new Error('The matching live runtime had no session id.')
  }
  return runtimeSessionId
}

export const DEMO_FIXTURE_PROMPT = [
  'Call the local Accord demo effect once with message "Accord desktop fixture. No external effect."',
  'Use accord_demo_effect if that tool exists, otherwise human_gate_demo_effect.',
  'Do not call any other tool. Do not post to X.'
].join(' ')

export async function queueFocusedDemo() {
  const focusedRuntimeId = resolveFocusedRuntimeId()
  if (!focusedRuntimeId) {
    throw new Error('Focus a Life chat first. Accord will not queue a demo into an unknown session.')
  }
  const scope = focusedApiScope()
  const route = await profileRoute(scope.profile)
  const release = await host.retainProfile(route)
  try {
    const active = await host.requestProfile(route, 'session.active_list', {})
    if (!active || !Array.isArray(active.sessions)) {
      throw new Error('Hermes returned no valid sessions array.')
    }
    const matches = active.sessions.filter(row => row && String(row.id || '') === focusedRuntimeId)
    if (matches.length !== 1) {
      throw new Error('The focused chat is not a live Life session Accord can target.')
    }
    const storedSessionId = text(matches[0]?.session_key)
    if (!storedSessionId) {
      throw new Error('The focused chat has no stored session identity.')
    }
    await host.requestProfile(route, 'prompt.submit', {
      session_id: focusedRuntimeId,
      text: DEMO_FIXTURE_PROMPT
    })
    return {
      runtime_id: focusedRuntimeId,
      session_key: storedSessionId,
      profile: scope.profile
    }
  } finally {
    if (release) release()
  }
}

export async function terminateSession(terminate) {
  const route = await profileRoute(terminate.profile)
  const release = await host.retainProfile(route)
  try {
    const active = await host.requestProfile(route, 'session.active_list', {})
    const runtimeSessionId = selectRuntimeToClose(active, terminate.stored_session_id)
    if (!runtimeSessionId) return false
    const result = await host.requestProfile(route, 'session.close', {
      session_id: runtimeSessionId
    })
    if (result?.closed !== true) {
      throw new Error('Hermes did not confirm that the originating session stopped.')
    }
    return true
  } finally {
    release()
  }
}

function AuditHistory({ audit }) {
  const events = Array.isArray(audit) ? audit : []
  if (!events.length) return null
  return jsxs('section', {
    'aria-label': 'Audit history',
    className: 'grid gap-2 border-t border-(--ui-stroke-secondary) pt-3',
    children: [
      jsx('h3', { className: 'text-xs font-semibold', children: 'Audit history' }),
      jsx('ol', {
        className: 'grid gap-2',
        children: events.map(event => jsxs('li', {
          className: 'grid gap-1 rounded bg-(--ui-surface-secondary) px-2 py-1.5 text-xs',
          children: [
            jsx('span', {
              className: 'font-medium',
              children: event.event_type === 'decision'
                ? `${event.decision} by ${event.actor_kind}`
                : `effect ${event.outcome}`
            }),
            event.comment && jsx('p', {
              className: 'whitespace-pre-wrap text-(--ui-text-secondary)',
              children: event.comment
            }),
            event.event_type === 'receipt' && jsx('span', {
              className: 'break-all text-[0.6875rem] text-(--ui-text-tertiary)',
              children: `Result digest ${event.result_digest}`
            }),
            jsx('time', {
              className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
              children: event.created_at
            })
          ]
        }, event.id))
      })
    ]
  })
}

function ApprovalCard({ ctx, request, ownerToken, onChanged }) {
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const decide = useCallback(async decision => {
    if (decision === 'comment' && !comment.trim()) {
      setError('Add a comment before sending changes back.')
      return
    }
    setBusy(decision)
    setError('')
    try {
      await submitDecisionAndResume({ ctx, request, decision, comment, ownerToken })
      haptic(decision === 'approve' ? 'success' : 'tap')
      host.notify({
        kind: decision === 'approve' ? 'success' : 'info',
        message: decision === 'approve'
          ? `Approved ${request.tool_name}; the originating session is resuming.`
          : decision === 'deny'
            ? `Denied ${request.tool_name}; the originating session is resuming with the decision.`
            : decision === 'cancel'
              ? `Cancelled ${request.tool_name}; the originating session is resuming with the decision.`
              : `Decision sent to the originating session.`
      })
      setComment('')
      await onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      await onChanged()
    } finally {
      setBusy('')
    }
  }, [
    comment,
    ctx,
    onChanged,
    ownerToken,
    request.call_digest,
    request.id,
    request.record_version,
    request.tool_name
  ])

  const retryResume = useCallback(async () => {
    setBusy('resume')
    setError('')
    try {
      const result = await profileRest(ctx, `/requests/${request.id}/resume-instruction`, {
        method: 'POST',
        body: { token: ownerToken }
      })
      await submitResume(ctx, result.resume, ownerToken)
      host.notify({ kind: 'success', message: 'The originating session is resuming.' })
      await onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      await onChanged()
    } finally {
      setBusy('')
    }
  }, [ctx, onChanged, ownerToken, request.id])


  const pending = request.state === 'pending'
  const cancellationLabel = approvalCancellationLabel(request.state)
  const retryLabel = retrySessionWakeLabel(request)
  return jsxs('article', {
    className: 'grid gap-3 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-primary) p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'grid gap-0.5',
            children: [
              jsx('h2', { className: 'text-sm font-semibold', children: request.tool_name }),
              jsx('div', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: `${request.profile} · ${request.effect_kind} · ${request.id.slice(0, 10)}`
              })
            ]
          }),
          jsx(StateBadge, { state: request.state })
        ]
      }),
      jsx(DisplayProjection, { display: request.display }),
      jsxs('div', {
        className: 'grid gap-1 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: `Digest ${request.call_digest}` }),
          jsx('span', { children: `Resume ${request.resume_state}` }),
          jsx('span', { children: `Session ${request.stored_session_id || 'unknown'}` }),
          request.session_lineage &&
            request.session_lineage !== request.stored_session_id &&
            jsx('span', { children: `Lineage ${request.session_lineage}` }),
          jsx('span', { children: request.created_at })
        ]
      }),
      jsx(AuditHistory, { audit: request.audit }),
      pending && jsxs('div', {
        className: 'grid gap-2 border-t border-(--ui-stroke-secondary) pt-3',
        children: [
          jsx('textarea', {
            'aria-label': `Comment on ${request.tool_name}`,
            className: 'min-h-20 resize-y rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm outline-none focus:border-(--ui-accent)',
            value: comment,
            onChange: event => setComment(event.target.value),
            placeholder: 'Optional for deny. Required when requesting changes.'
          }),
          jsxs('div', {
            className: 'flex flex-wrap gap-2',
            children: [
              jsx('button', {
                type: 'button',
                className: 'rounded bg-(--ui-accent) px-3 py-1.5 text-sm font-medium text-(--ui-accent-foreground) disabled:opacity-50',
                disabled: Boolean(busy),
                onClick: () => void decide('approve'),
                children: busy === 'approve' ? 'Approving…' : 'Approve and resume'
              }),
              jsx('button', {
                type: 'button',
                className: 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
                disabled: Boolean(busy),
                onClick: () => void decide('comment'),
                children: busy === 'comment' ? 'Sending…' : 'Request changes'
              }),
              jsx('button', {
                type: 'button',
                className: 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm text-(--ui-text-secondary) disabled:opacity-50',
                disabled: Boolean(busy),
                onClick: () => void decide('cancel'),
                children: busy === 'cancel' ? 'Cancelling…' : 'Cancel request'
              }),
              jsx('button', {
                type: 'button',
                className: 'rounded border border-(--ui-danger) px-3 py-1.5 text-sm text-(--ui-danger) disabled:opacity-50',
                disabled: Boolean(busy),
                onClick: () => void decide('deny'),
                children: busy === 'deny' ? 'Denying…' : 'Deny'
              })
            ]
          })
        ]
      }),
      cancellationLabel && jsx('button', {
        type: 'button',
        className: 'w-fit rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm text-(--ui-text-secondary) disabled:opacity-50',
        disabled: Boolean(busy) || !ownerToken,
        onClick: () => void decide('cancel'),
        children: busy === 'cancel' ? 'Cancelling…' : cancellationLabel
      }),
      retryLabel && jsx('button', {
        type: 'button',
        className: 'w-fit rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
        disabled: Boolean(busy),
        onClick: () => void retryResume(),
        children: busy === 'resume' ? 'Waking session…' : retryLabel
      }),
      request.resume_state === 'dispatching' && jsx('p', {
        className: 'text-xs text-(--ui-warning)',
        children: 'Session wake is in progress or uncertain. Accord will not retry it automatically.'
      }),

      error && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) px-2 py-1.5 text-xs text-(--ui-danger)',
        children: error
      })
    ]
  })
}

function requestPreview(request) {
  const entries = Object.entries(request?.display || {})
  if (!entries.length) return 'No display metadata'
  const [key, value] = entries[0]
  const rendered = typeof value === 'string' ? value : JSON.stringify(value)
  const clipped = rendered.length > 120 ? `${rendered.slice(0, 117)}…` : rendered
  return `${key.replaceAll('_', ' ')}: ${clipped}`
}

function ApprovalTableEntry({ ctx, request, ownerToken, onChanged, expanded, onToggle }) {
  return jsxs('tbody', {
    className: 'border-t border-(--ui-stroke-secondary)',
    children: [
      jsxs('tr', {
        className: 'align-middle hover:bg-(--ui-surface-secondary)',
        children: [
          jsx('td', { className: 'whitespace-nowrap px-3 py-2', children: jsx(StateBadge, { state: request.state }) }),
          jsxs('td', {
            className: 'min-w-40 px-3 py-2',
            children: [
              jsx('div', { className: 'text-sm font-medium', children: request.tool_name }),
              jsx('div', { className: 'font-mono text-[0.6875rem] text-(--ui-text-tertiary)', children: request.id.slice(0, 12) })
            ]
          }),
          jsxs('td', {
            className: 'whitespace-nowrap px-3 py-2 text-xs text-(--ui-text-secondary)',
            children: [
              jsx('div', { children: request.profile }),
              jsx('div', { className: 'text-(--ui-text-tertiary)', children: request.effect_kind })
            ]
          }),
          jsx('td', {
            className: 'max-w-xl px-3 py-2 text-xs text-(--ui-text-secondary)',
            children: jsx('div', { className: 'truncate', title: requestPreview(request), children: requestPreview(request) })
          }),
          jsx('td', {
            className: 'whitespace-nowrap px-3 py-2 text-xs text-(--ui-text-tertiary)',
            children: new Date(request.created_at).toLocaleString()
          }),
          jsx('td', {
            className: 'px-3 py-2 text-right',
            children: jsx('button', {
              type: 'button',
              className: 'rounded border border-(--ui-stroke-secondary) px-2 py-1 text-xs',
              onClick: onToggle,
              'aria-expanded': expanded,
              children: expanded ? 'Close' : 'Review'
            })
          })
        ]
      }),
      expanded && jsx('tr', {
        children: jsx('td', {
          colSpan: 6,
          className: 'bg-(--ui-surface-secondary) p-3',
          children: jsx(ApprovalCard, { ctx, request, ownerToken, onChanged })
        })
      })
    ]
  })
}

function listText(value) {
  return Array.isArray(value) ? value.join(', ') : ''
}

function parseListText(value) {
  return String(value || '')
    .split(/[\n,]/)
    .map(item => item.trim())
    .filter(Boolean)
}

function globCharacterClass(pattern, index, character) {
  let end = index + 1
  if (end < pattern.length && pattern[end] === '!') end += 1
  if (end < pattern.length && pattern[end] === ']') end += 1
  while (end < pattern.length && pattern[end] !== ']') end += 1
  if (end >= pattern.length) return null

  let memberStart = index + 1
  let stuff
  if (!pattern.slice(memberStart, end).includes('-')) {
    stuff = pattern.slice(memberStart, end).join('').replaceAll('\\', '\\\\')
  } else {
    const chunks = []
    let search = pattern[memberStart] === '!' ? memberStart + 2 : memberStart + 1
    while (true) {
      let hyphen = -1
      for (let cursor = search; cursor < end; cursor += 1) {
        if (pattern[cursor] === '-') {
          hyphen = cursor
          break
        }
      }
      if (hyphen < 0) break
      chunks.push(pattern.slice(memberStart, hyphen).join(''))
      memberStart = hyphen + 1
      search = hyphen + 3
    }
    const finalChunk = pattern.slice(memberStart, end).join('')
    if (finalChunk) chunks.push(finalChunk)
    else chunks[chunks.length - 1] += '-'

    for (let cursor = chunks.length - 1; cursor > 0; cursor -= 1) {
      const left = Array.from(chunks[cursor - 1])
      const right = Array.from(chunks[cursor])
      if (left.at(-1).codePointAt(0) > right[0].codePointAt(0)) {
        chunks[cursor - 1] = left.slice(0, -1).join('') + right.slice(1).join('')
        chunks.splice(cursor, 1)
      }
    }
    stuff = chunks
      .map(chunk => chunk.replaceAll('\\', '\\\\').replaceAll('-', '\\-'))
      .join('-')
  }

  if (!stuff) return { matches: false, next: end + 1 }
  if (stuff === '!') return { matches: true, next: end + 1 }
  let negated = false
  if (stuff[0] === '!') {
    negated = true
    stuff = stuff.slice(1)
  }
  if (stuff[0] === '^') {
    stuff = `\\${stuff}`
  }
  stuff = stuff.replaceAll('[', '\\[').replaceAll(']', '\\]')
  try {
    const included = new RegExp(`^[${stuff}]$`, 'u').test(character)
    return { matches: negated ? !included : included, next: end + 1 }
  } catch {
    return { matches: false, next: end + 1 }
  }
}

function fnmatchCase(value, pattern) {
  const input = Array.from(String(value))
  const glob = Array.from(String(pattern))
  const memo = new Map()
  const match = (inputIndex, patternIndex) => {
    const key = `${inputIndex}:${patternIndex}`
    if (memo.has(key)) return memo.get(key)
    let result = false
    if (patternIndex === glob.length) {
      result = inputIndex === input.length
    } else if (glob[patternIndex] === '*') {
      let nextPattern = patternIndex + 1
      while (glob[nextPattern] === '*') nextPattern += 1
      for (let cursor = inputIndex; cursor <= input.length && !result; cursor += 1) {
        result = match(cursor, nextPattern)
      }
    } else if (inputIndex < input.length && glob[patternIndex] === '?') {
      result = match(inputIndex + 1, patternIndex + 1)
    } else if (inputIndex < input.length && glob[patternIndex] === '[') {
      const characterClass = globCharacterClass(glob, patternIndex, input[inputIndex])
      result = characterClass
        ? characterClass.matches && match(inputIndex + 1, characterClass.next)
        : input[inputIndex] === '[' && match(inputIndex + 1, patternIndex + 1)
    } else if (inputIndex < input.length && glob[patternIndex] === input[inputIndex]) {
      result = match(inputIndex + 1, patternIndex + 1)
    }
    memo.set(key, result)
    return result
  }
  return match(0, 0)
}

export function matchingToolOptions(tools, selector, selectorType) {
  const candidates = Array.isArray(tools) ? tools : []
  const value = String(selector ?? '')
  if (!value) return []
  return candidates
    .filter(tool => {
      const name = String(tool?.name ?? '')
      return selectorType === 'glob' ? fnmatchCase(name, value) : name === value
    })
    .sort((left, right) => String(left?.name ?? '').localeCompare(String(right?.name ?? '')))
}

export function policyFieldOptions(tools, selector, selectorType) {
  const matched = matchingToolOptions(tools, selector, selectorType)
  if (!matched.length) return []
  const fields = new Set((Array.isArray(matched[0]?.fields) ? matched[0].fields : []).map(text).filter(Boolean))
  if (selectorType !== 'glob') return Array.from(fields).sort()
  for (const tool of matched.slice(1)) {
    const toolFields = new Set((Array.isArray(tool?.fields) ? tool.fields : []).map(text).filter(Boolean))
    for (const field of fields) {
      if (!toolFields.has(field)) fields.delete(field)
    }
  }
  return Array.from(fields).sort()
}

export function availableReplayFields(displayFields, replayFields) {
  const selected = new Set(Array.isArray(replayFields) ? replayFields : [])
  return (Array.isArray(displayFields) ? displayFields : [])
    .filter(field => text(field) && !selected.has(field))
}

function FieldPicker({ id, label, selected, options, onChange, allowCustom = true, help }) {
  const [query, setQuery] = useState('')
  const values = Array.isArray(selected) ? selected : []
  const available = (Array.isArray(options) ? options : []).filter(field => !values.includes(field))
  const candidate = text(query)
  const canAdd = Boolean(candidate) && !values.includes(candidate) && (allowCustom || available.includes(candidate))
  const add = () => {
    if (!canAdd) return
    onChange([...values, candidate])
    setQuery('')
  }
  return jsxs('div', {
    className: 'grid gap-1.5',
    children: [
      jsx('label', {
        htmlFor: `${id}-input`,
        className: 'text-xs text-(--ui-text-secondary)',
        children: label
      }),
      Boolean(values.length) && jsx('div', {
        className: 'flex flex-wrap gap-1.5',
        children: values.map(field => jsxs('span', {
          className: 'inline-flex items-center gap-1 rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1 font-mono text-xs',
          children: [
            field,
            jsx('button', {
              type: 'button',
              className: 'text-(--ui-text-tertiary) hover:text-(--ui-danger)',
              'aria-label': `Remove ${field} from ${label.toLowerCase()}`,
              onClick: () => onChange(values.filter(value => value !== field)),
              children: '×'
            })
          ]
        }, field))
      }),
      jsxs('div', {
        className: 'flex gap-2',
        children: [
          jsx('input', {
            id: `${id}-input`,
            type: 'search',
            list: `${id}-options`,
            className: 'min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm outline-none focus:border-(--ui-accent)',
            value: query,
            onChange: event => setQuery(event.target.value),
            onKeyDown: event => {
              if (event.key !== 'Enter') return
              event.preventDefault()
              add()
            },
            placeholder: 'Search or type a field name'
          }),
          jsx('button', {
            type: 'button',
            className: 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-xs disabled:opacity-50',
            disabled: !canAdd,
            onClick: add,
            children: 'Add field'
          })
        ]
      }),
      jsx('datalist', {
        id: `${id}-options`,
        children: available.map(field => jsx('option', { value: field }, field))
      }),
      help && jsx('p', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: help })
    ]
  })
}

function duplicateItems(value) {
  const seen = new Set()
  const duplicates = new Set()
  for (const item of Array.isArray(value) ? value : []) {
    if (seen.has(item)) duplicates.add(item)
    seen.add(item)
  }
  return Array.from(duplicates)
}

function PolicyEditor({ policy, index, onChange, onRemove, options }) {
  const selectorType = Object.hasOwn(policy, 'tool_glob') ? 'glob' : 'exact'
  const selector = selectorType === 'glob' ? policy.tool_glob : policy.tool_name
  const terminalPolicy = selector === 'terminal'
  const tools = Array.isArray(options?.tools) ? options.tools : []
  const effectKinds = Array.isArray(options?.effect_kinds) ? options.effect_kinds : []
  const matchedTools = matchingToolOptions(tools, selector, selectorType)
  const schemaFields = policyFieldOptions(tools, selector, selectorType)
  const displayFields = Array.isArray(policy.display_fields) ? policy.display_fields : []
  const replayFields = Array.isArray(policy.replay_fields) ? policy.replay_fields : []
  const replayOptions = availableReplayFields(displayFields, replayFields)
  const exactTool = selectorType === 'exact' ? matchedTools[0] : null
  const toolListId = `policy-${index}-tools`
  const effectListId = `policy-${index}-effects`
  const inputClass = 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm outline-none focus:border-(--ui-accent)'
  const updateSelectorType = event => {
    const next = { ...policy }
    delete next.tool_name
    delete next.tool_glob
    next[event.target.value === 'glob' ? 'tool_glob' : 'tool_name'] = ''
    delete next.command_exact
    delete next.command_glob
    onChange(next)
  }
  const updateSelector = event => {
    const next = { ...policy, [selectorType === 'glob' ? 'tool_glob' : 'tool_name']: event.target.value }
    if (event.target.value !== 'terminal') {
      delete next.command_exact
      delete next.command_glob
    }
    onChange(next)
  }
  const updateList = key => event => onChange({ ...policy, [key]: parseListText(event.target.value) })
  const updateDisplayFields = fields => onChange({
    ...policy,
    display_fields: fields,
    replay_fields: replayFields.filter(field => fields.includes(field))
  })
  const exactCommandDuplicates = duplicateItems(policy.command_exact)
  const commandGlobDuplicates = duplicateItems(policy.command_glob)
  return jsxs('article', {
    className: 'grid gap-3 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-primary) p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsx('h3', { className: 'text-sm font-semibold', children: `Policy ${index + 1}` }),
          jsx('button', {
            type: 'button',
            className: 'rounded border border-(--ui-danger) px-2 py-1 text-xs text-(--ui-danger)',
            onClick: onRemove,
            children: 'Remove'
          })
        ]
      }),
      jsxs('div', {
        className: 'grid gap-3 md:grid-cols-3',
        children: [
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              'Selector type',
              jsxs('select', {
                className: inputClass,
                value: selectorType,
                onChange: updateSelectorType,
                children: [
                  jsx('option', { value: 'exact', children: 'Exact tool' }),
                  jsx('option', { value: 'glob', children: 'Tool glob' })
                ]
              })
            ]
          }),
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              selectorType === 'glob' ? 'Tool glob' : 'Tool name',
              jsx('input', {
                type: 'search',
                list: selectorType === 'exact' ? toolListId : undefined,
                'aria-label': selectorType === 'exact' ? 'Search registered tools' : 'Tool glob',
                className: inputClass,
                value: selector || '',
                onChange: updateSelector,
                placeholder: selectorType === 'glob' ? 'records_*' : 'terminal'
              }),
              selectorType === 'exact' && jsx('datalist', {
                id: toolListId,
                children: tools.map(tool => jsx('option', {
                  value: tool.name,
                  label: [tool.toolset, tool.description].filter(Boolean).join(' · ')
                }, tool.name))
              })
            ]
          }),
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              'Effect kind',
              jsx('input', {
                type: 'search',
                list: effectListId,
                className: inputClass,
                value: policy.effect_kind || '',
                onChange: event => onChange({ ...policy, effect_kind: event.target.value }),
                placeholder: 'consequential_write'
              }),
              jsx('datalist', {
                id: effectListId,
                children: effectKinds.map(effect => jsx('option', { value: effect }, effect))
              }),
              jsx('span', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: 'Choose a common effect or enter a custom label.'
              })
            ]
          })
        ]
      }),
      selectorType === 'exact' && jsx('div', {
        className: 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
        children: exactTool
          ? jsxs('div', {
              className: 'grid gap-0.5',
              children: [
                jsxs('div', {
                  children: [
                    jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: exactTool.name }),
                    exactTool.toolset && ` · ${exactTool.toolset}`
                  ]
                }),
                exactTool.description && jsx('p', { children: exactTool.description }),
                jsx('p', {
                  className: 'text-(--ui-text-tertiary)',
                  children: `${schemaFields.length} safe top-level field${schemaFields.length === 1 ? '' : 's'} available.`
                })
              ]
            })
          : (selector
              ? 'Custom or unavailable tool. Field names can still be added manually.'
              : 'Choose a registered tool or type a custom tool name.')
      }),
      selectorType === 'glob' && jsxs('div', {
        className: 'grid gap-2 rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
        children: [
          jsx('div', {
            className: 'font-medium text-(--ui-text-primary)',
            children: 'Matching registered tools'
          }),
          !selector && jsx('p', { children: 'Use * and ? to preview registered tool matches.' }),
          selector && !matchedTools.length && jsx('p', {
            children: 'No registered tools match. This policy can still target a tool loaded later.'
          }),
          Boolean(matchedTools.length) && jsxs('div', {
            className: 'flex flex-wrap gap-1.5',
            children: [
              ...matchedTools.slice(0, 8).map(tool => jsx('span', {
                className: 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-primary) px-2 py-1 font-mono text-[0.6875rem]',
                title: tool.description || tool.name,
                children: tool.name
              }, tool.name)),
              matchedTools.length > 8 && jsx('span', {
                className: 'px-1 py-1 text-(--ui-text-tertiary)',
                children: `+${matchedTools.length - 8} more`
              })
            ]
          }),
          Boolean(matchedTools.length) && jsx('p', {
            className: 'text-(--ui-text-tertiary)',
            children: `${schemaFields.length} safe fields shared by all ${matchedTools.length} matching tools.`
          })
        ]
      }),
      jsxs('div', {
        className: 'grid gap-3 md:grid-cols-2',
        children: [
          jsx(FieldPicker, {
            id: `policy-${index}-display-fields`,
            label: 'Display fields',
            selected: displayFields,
            options: schemaFields,
            onChange: updateDisplayFields,
            help: schemaFields.length
              ? (selectorType === 'glob'
                  ? 'Suggestions are safe top-level fields shared by every matching tool schema. Custom fields remain available.'
                  : 'Suggestions come from safe top-level fields in the matching tool schema. Custom fields remain available.')
              : 'No matching schema fields are available. Add a field name manually if the tool is unloaded or deferred.'
          }),
          jsx(FieldPicker, {
            id: `policy-${index}-replay-fields`,
            label: 'Replay fields',
            selected: replayFields,
            options: replayOptions,
            allowCustom: false,
            onChange: fields => onChange({ ...policy, replay_fields: fields }),
            help: 'Only displayed fields can be replayed.'
          })
        ]
      }),
      terminalPolicy && jsxs('div', {
        className: 'grid gap-3 md:grid-cols-2',
        children: [
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              'Exact commands, one per line',
              jsx('textarea', {
                className: `${inputClass} min-h-20 resize-y font-mono text-xs`,
                value: Array.isArray(policy.command_exact) ? policy.command_exact.join('\n') : '',
                onChange: updateList('command_exact')
              }),
              Boolean(exactCommandDuplicates.length) && jsx('span', {
                className: 'text-[0.6875rem] text-(--ui-danger)',
                children: `Remove duplicate commands: ${exactCommandDuplicates.join(', ')}`
              })
            ]
          }),
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              'Command globs, one per line',
              jsx('textarea', {
                className: `${inputClass} min-h-20 resize-y font-mono text-xs`,
                value: Array.isArray(policy.command_glob) ? policy.command_glob.join('\n') : '',
                onChange: updateList('command_glob')
              }),
              Boolean(commandGlobDuplicates.length) && jsx('span', {
                className: 'text-[0.6875rem] text-(--ui-danger)',
                children: `Remove duplicate command globs: ${commandGlobDuplicates.join(', ')}`
              })
            ]
          })
        ]
      })
    ]
  })
}

export function policyEditorKey(index, _policy) {
  return `policy-${index}`
}

function PolicySettings({ ctx, ownerToken }) {
  const [policies, setPolicies] = useState([])
  const [digest, setDigest] = useState('')
  const [options, setOptions] = useState({ effect_kinds: [], tools: [] })
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [optionsError, setOptionsError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    if (!ownerToken) return
    setLoading(true)
    setError('')
    setOptionsError('')
    setOptions({ effect_kinds: [], tools: [] })
    try {
      const [settingsResult, optionsResult] = await Promise.allSettled([
        profileRest(ctx, '/settings/read', {
          method: 'POST',
          body: { token: ownerToken }
        }),
        profileRest(ctx, '/settings/options', {
          method: 'POST',
          body: { token: ownerToken }
        })
      ])
      if (settingsResult.status === 'rejected') throw settingsResult.reason
      const result = settingsResult.value
      setPolicies(Array.isArray(result.policies) ? result.policies : [])
      setDigest(result.digest || '')
      if (optionsResult.status === 'fulfilled') {
        setOptions({
          effect_kinds: Array.isArray(optionsResult.value.effect_kinds) ? optionsResult.value.effect_kinds : [],
          tools: Array.isArray(optionsResult.value.tools) ? optionsResult.value.tools : []
        })
      } else {
        const reason = optionsResult.reason instanceof Error ? optionsResult.reason.message : String(optionsResult.reason)
        setOptionsError(`Tool options unavailable. You can still enter values manually. ${reason}`)
      }
      setNotice('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }, [ctx, ownerToken])

  useEffect(() => { void load() }, [load])

  const save = useCallback(async () => {
    setSaving(true)
    setError('')
    setNotice('')
    try {
      const result = await profileRest(ctx, '/settings/policies', {
        method: 'PUT',
        body: { token: ownerToken, expected_digest: digest, policies }
      })
      setPolicies(Array.isArray(result.policies) ? result.policies : [])
      setDigest(result.digest || '')
      setNotice('Policies saved. Restart the Hermes agent process before relying on the new rules.')
      host.notify({ kind: 'success', message: 'Accord policies saved.' })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setSaving(false)
    }
  }, [ctx, digest, ownerToken, policies])

  const update = (index, policy) => setPolicies(current => current.map((item, itemIndex) => itemIndex === index ? policy : item))
  const remove = index => setPolicies(current => current.filter((_, itemIndex) => itemIndex !== index))
  const add = () => setPolicies(current => [...current, {
    tool_name: '',
    effect_kind: 'consequential_write',
    display_fields: [],
    replay_fields: []
  }])

  return jsxs('section', {
    className: 'grid gap-3',
    children: [
      jsxs('div', {
        className: 'rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3 text-xs text-(--ui-text-secondary)',
        children: [
          jsx('h2', { className: 'mb-1 text-sm font-semibold text-(--ui-text-primary)', children: 'Policy settings' }),
          jsx('p', { children: 'This editor manages explicit tool and terminal approval policies only. Built-in owned effects remain gated. Provider accounts and credentials are never shown or changed here.' }),
          jsx('p', { className: 'mt-1 text-(--ui-warning)', children: 'Saved policy changes require an agent process restart before they take effect.' })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap gap-2',
        children: [
          jsx('button', {
            type: 'button',
            className: 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm',
            onClick: add,
            children: 'Add policy'
          }),
          jsx('button', {
            type: 'button',
            className: 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm',
            disabled: loading,
            onClick: () => void load(),
            children: loading ? 'Reloading…' : 'Reload policies'
          }),
          jsx('button', {
            type: 'button',
            className: 'rounded bg-(--ui-accent) px-3 py-1.5 text-sm font-medium text-(--ui-accent-foreground) disabled:opacity-50',
            disabled: saving || loading || !ownerToken || !digest,
            onClick: () => void save(),
            children: saving ? 'Saving…' : 'Save policies'
          })
        ]
      }),
      loading && jsx('p', { className: 'text-sm text-(--ui-text-tertiary)', children: 'Loading policies and tool options…' }),
      !loading && policies.map((policy, index) => jsx(PolicyEditor, {
        policy,
        index,
        options,
        onChange: next => update(index, next),
        onRemove: () => remove(index)
      }, policyEditorKey(index, policy))),
      !loading && !policies.length && jsx('div', {
        className: 'rounded-lg border border-dashed border-(--ui-stroke-secondary) p-6 text-sm text-(--ui-text-tertiary)',
        children: 'No explicit policies. Built-in Accord effects are still protected.'
      }),
      notice && jsx('div', { className: 'rounded bg-(--ui-success-bg) p-3 text-sm text-(--ui-success)', children: notice }),
      optionsError && jsx('div', {
        role: 'status',
        className: 'rounded bg-(--ui-warning-bg) p-3 text-sm text-(--ui-warning)',
        children: optionsError
      }),
      error && jsx('div', { role: 'alert', className: 'rounded bg-(--ui-danger-bg) p-3 text-sm text-(--ui-danger)', children: error })
    ]
  })
}

function AccordPage({ ctx }) {
  const [ownerToken, setOwnerToken] = useState('')
  const [ownerError, setOwnerError] = useState('')
  const [view, setView] = useState('approvals')
  const [query, setQuery] = useState('')
  const [stateFilter, setStateFilter] = useState('all')
  const [effectFilter, setEffectFilter] = useState('all')
  const [sort, setSort] = useState('newest')
  const [expandedId, setExpandedId] = useState('')
  const [demoBusy, setDemoBusy] = useState(false)
  const [demoError, setDemoError] = useState('')
  const [demoNotice, setDemoNotice] = useState('')
  const { requests, loading, error, refresh } = useRequests(ctx)
  const pendingCount = useMemo(
    () => requests.filter(request => request.state === 'pending').length,
    [requests]
  )
  const effects = useMemo(
    () => Array.from(new Set(requests.map(request => request.effect_kind).filter(Boolean))).sort(),
    [requests]
  )
  const shown = useMemo(
    () => filterAndSortRequests(requests, {
      query,
      state: stateFilter,
      effect: effectFilter,
      sort
    }),
    [effectFilter, query, requests, sort, stateFilter]
  )

  useEffect(() => {
    let active = true
    void ensureOwner(ctx)
      .then(token => {
        if (active) setOwnerToken(token)
      })
      .catch(cause => {
        if (active) setOwnerError(cause instanceof Error ? cause.message : String(cause))
      })
    return () => { active = false }
  }, [ctx])

  return jsxs('main', {
    className: 'grid h-full content-start gap-4 overflow-y-auto p-4 text-(--ui-text-primary)',
    children: [
      jsxs('header', {
        className: 'flex flex-wrap items-start justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'grid gap-1',
            children: [
              jsx('h1', { className: 'text-lg font-semibold', children: PLUGIN_DISPLAY_NAME }),
              jsx('p', {
                className: 'max-w-2xl text-sm text-(--ui-text-secondary)',
                children: 'Consequential tool calls stay blocked until you approve the exact call. Cards do not expire. Focus a different Life chat, then queue a demo. Do not use this plugin-work thread.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex flex-wrap gap-2',
            children: [
              jsx('button', {
                type: 'button',
                className: view === 'approvals'
                  ? 'rounded bg-(--ui-accent) px-3 py-1.5 text-xs font-medium text-(--ui-accent-foreground)'
                  : 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-xs',
                onClick: () => setView('approvals'),
                children: 'Approvals'
              }),
              jsx('button', {
                type: 'button',
                className: view === 'settings'
                  ? 'rounded bg-(--ui-accent) px-3 py-1.5 text-xs font-medium text-(--ui-accent-foreground)'
                  : 'rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-xs',
                onClick: () => setView('settings'),
                children: 'Edit configuration'
              }),
              view === 'approvals' && jsx('button', {
                type: 'button',
                className: 'rounded border border-(--ui-stroke-secondary) px-2.5 py-1 text-xs',
                disabled: demoBusy,
                onClick: () => {
                  setDemoBusy(true)
                  setDemoError('')
                  setDemoNotice('')
                  void queueFocusedDemo()
                    .then(result => {
                      setQuery(result.session_key)
                      setStateFilter('pending')
                      setDemoNotice(
                        `Demo queued. Resume session ${result.session_key}. Runtime ${result.runtime_id}. Search the new pending card. Ignore 336fc722.`
                      )
                      host.notify({ kind: 'success', message: 'Accord demo queued in the focused chat.' })
                      return refresh()
                    })
                    .catch(cause => {
                      setDemoError(cause instanceof Error ? cause.message : String(cause))
                    })
                    .finally(() => setDemoBusy(false))
                },
                children: demoBusy ? 'Queuing demo…' : 'Queue demo in focused chat'
              }),
              view === 'approvals' && jsx('button', {
                type: 'button',
                className: 'rounded border border-(--ui-stroke-secondary) px-2.5 py-1 text-xs',
                onClick: () => void refresh(),
                children: 'Refresh'
              })
            ]
          })
        ]
      }),
      ownerError && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) p-3 text-sm text-(--ui-danger)',
        children: `Owner control could not initialize. ${ownerError}`
      }),
      demoError && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) p-3 text-sm text-(--ui-danger)',
        children: demoError
      }),
      demoNotice && jsx('div', {
        className: 'rounded bg-(--ui-success-bg) p-3 text-sm text-(--ui-success)',
        children: demoNotice
      }),
      view === 'approvals' && error && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) p-3 text-sm text-(--ui-danger)',
        children: error
      }),
      view === 'approvals' && jsxs('section', {
        className: 'grid gap-3',
        children: [
          jsxs('div', {
            className: 'grid gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-primary) p-3 md:grid-cols-[minmax(14rem,1fr)_repeat(3,minmax(9rem,auto))]',
            children: [
              jsx('input', {
                type: 'search',
                'aria-label': 'Search approvals',
                placeholder: 'Search approvals',
                className: 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm outline-none focus:border-(--ui-accent)',
                value: query,
                onChange: event => setQuery(event.target.value)
              }),
              jsxs('select', {
                'aria-label': 'Filter by state',
                className: 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm',
                value: stateFilter,
                onChange: event => setStateFilter(event.target.value),
                children: [
                  jsx('option', { value: 'all', children: 'All states' }),
                  ...['pending', 'approved', 'changes_requested', 'denied', 'cancelled', 'claimed', 'executed', 'failed', 'uncertain'].map(state =>
                    jsx('option', { value: state, children: state.replaceAll('_', ' ') }, state)
                  )
                ]
              }),
              jsxs('select', {
                'aria-label': 'Filter by effect',
                className: 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm',
                value: effectFilter,
                onChange: event => setEffectFilter(event.target.value),
                children: [
                  jsx('option', { value: 'all', children: 'All effects' }),
                  ...effects.map(effect => jsx('option', { value: effect, children: effect }, effect))
                ]
              }),
              jsxs('select', {
                'aria-label': 'Sort approvals',
                className: 'rounded border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) px-2 py-1.5 text-sm',
                value: sort,
                onChange: event => setSort(event.target.value),
                children: [
                  jsx('option', { value: 'newest', children: 'Newest first' }),
                  jsx('option', { value: 'oldest', children: 'Oldest first' }),
                  jsx('option', { value: 'tool', children: 'Tool name' }),
                  jsx('option', { value: 'state', children: 'State' })
                ]
              })
            ]
          }),
          jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: `${pendingCount} pending · ${shown.length} of ${requests.length} shown`
          }),
          loading && jsx('div', {
            className: 'text-sm text-(--ui-text-tertiary)',
            children: 'Loading approvals…'
          }),
          !loading && !shown.length && jsx('div', {
            className: 'rounded-lg border border-dashed border-(--ui-stroke-secondary) p-6 text-sm text-(--ui-text-tertiary)',
            children: requests.length ? 'No approvals match these filters.' : 'No approval requests yet.'
          }),
          !loading && Boolean(shown.length) && jsx('div', {
            className: 'overflow-x-auto rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-primary)',
            children: jsxs('table', {
              className: 'w-full min-w-[54rem] border-collapse text-left',
              children: [
                jsx('thead', {
                  className: 'bg-(--ui-surface-secondary) text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                  children: jsxs('tr', {
                    children: ['State', 'Request', 'Scope', 'Preview', 'Created', ''].map(label =>
                      jsx('th', { className: 'px-3 py-2 font-medium', children: label }, label || 'actions')
                    )
                  })
                }),
                ...shown.map(request => jsx(ApprovalTableEntry, {
                  ctx,
                  request,
                  ownerToken,
                  onChanged: refresh,
                  expanded: expandedId === request.id,
                  onToggle: () => setExpandedId(current => current === request.id ? '' : request.id)
                }, request.id))
              ]
            })
          })
        ]
      }),
      view === 'settings' && jsx(PolicySettings, { ctx, ownerToken })
    ]
  })
}

function PendingStatus({ ctx }) {
  const { requests } = useRequests(ctx, 'pending')
  const count = requests.length
  return jsx('button', {
    type: 'button',
    className: count
      ? 'rounded px-1.5 text-[0.6875rem] font-medium text-(--ui-warning)'
      : 'rounded px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    onClick: () => host.navigate(PLUGIN_ROUTE),
    'aria-label': `${count} pending ${PLUGIN_DISPLAY_NAME} approvals`,
    children: `accord ${count}`
  })
}

export default {
  id: PLUGIN_ID,
  name: PLUGIN_DISPLAY_NAME,
  defaultEnabled: true,
  register(ctx) {
    trackFocusedOwner(ctx)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PLUGIN_ROUTE },
        render: () => jsx(AccordPage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PLUGIN_ROUTE, label: PLUGIN_DISPLAY_NAME, codicon: 'shield' }
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 110,
        render: () => jsx(PendingStatus, { ctx })
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.open`,
          label: `Open ${PLUGIN_DISPLAY_NAME} approvals`,
          keywords: ['approval', 'accord', 'gate', 'human'],
          run: () => host.navigate(PLUGIN_ROUTE)
        }
      },
      {
        id: 'demo',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.demo`,
          label: `Queue ${PLUGIN_DISPLAY_NAME} demo in focused chat`,
          keywords: ['approval', 'accord', 'demo', 'fixture'],
          run: () => queueFocusedDemo().then(result => {
            host.notify({
              kind: 'success',
              message: `Accord demo queued in ${result.session_key}.`
            })
            host.navigate(PLUGIN_ROUTE)
          }).catch(cause => {
            host.notify({
              kind: 'error',
              message: cause instanceof Error ? cause.message : String(cause)
            })
          })
        }
      }
    ])
  }
}
