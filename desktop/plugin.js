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

const ACTIVE_STATES = 'pending,approved,changes_requested,denied,cancelled,claimed,executed,failed,uncertain'
const ACTIVE_CANCELLED_RESUME_STATES = new Set(['pending', 'dispatching', 'failed'])
const PROFILE_PROBE_PATH = '/requests?state=pending&limit=1'
let lastFocusedOwner = null
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
    throw new Error(`Human Gate API path traversal rejected: ${path}`)
  }
  return suffix
}

async function restAtScope(ctx, scope, path, options = {}) {
  const connectionMatches =
    !scope.connectionId ||
    scope.connectionId === scope.activeConnectionId ||
    (scope.connectionId === 'local' && !scope.activeConnectionId)

  if (scope.profile === scope.activeProfile && connectionMatches) {
    return ctx.rest(path, options)
  }

  if (!window.hermesDesktop?.api) {
    throw new Error('Hermes Desktop API bridge unavailable for the focused profile.')
  }

  const request = {
    path: `/api/plugins/human-gate${pluginApiSuffix(path)}`,
    profile: scope.profile
  }
  if (scope.connectionId) request.connectionId = scope.connectionId
  if (options.method !== undefined) request.method = options.method
  if (options.body !== undefined) request.body = options.body
  if (options.upload !== undefined) request.upload = options.upload
  if (options.timeoutMs !== undefined) request.timeoutMs = options.timeoutMs
  return window.hermesDesktop.api(request)
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
      throw new Error('Human Gate could not inspect Hermes profile routes.')
    }

    const routes = await host.profileRoutes()
    if (!Array.isArray(routes)) {
      throw new Error('Hermes returned an invalid profile route list for Human Gate.')
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
          ? 'Human Gate is not enabled in any available Hermes profile.'
          : 'Human Gate is enabled in more than one Hermes profile. Select its owning profile first.'
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
  const stop = ownerAtom?.listen?.(rememberFocusedOwner)
  if (typeof stop === 'function' && typeof ctx.onDispose === 'function') {
    ctx.onDispose(stop)
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
      throw new Error('Human Gate refused to deliver an empty decision prompt; a nonempty decision prompt is required.')
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
    const runtimeSessionId = String(resumed?.session_id || '')
    const storedSessionId = String(resumed?.session_key || '')
    if (!runtimeSessionId) {
      throw new Error('The stored session resumed without an active runtime.')
    }
    if (!storedSessionId) {
      throw new Error('The resumed runtime did not prove the stored session lineage.')
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
      if (['approve', 'comment', 'deny', 'cancel'].includes(decision)) {
        const instruction = await profileRest(ctx, `/requests/${request.id}/resume-instruction`, {
          method: 'POST',
          body: { token: ownerToken }
        })
        await submitResume(ctx, instruction.resume, ownerToken)
      }
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
        children: 'Session wake is in progress or uncertain. Human Gate will not retry it automatically.'
      }),

      error && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) px-2 py-1.5 text-xs text-(--ui-danger)',
        children: error
      })
    ]
  })
}

function HumanGatePage({ ctx }) {
  const [ownerToken, setOwnerToken] = useState('')
  const [ownerError, setOwnerError] = useState('')
  const { requests, loading, error, refresh } = useRequests(ctx)
  const pendingCount = useMemo(
    () => requests.filter(request => request.state === 'pending').length,
    [requests]
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
              jsx('h1', { className: 'text-lg font-semibold', children: 'Human Gate' }),
              jsx('p', {
                className: 'max-w-2xl text-sm text-(--ui-text-secondary)',
                children: 'Consequential tool calls stay blocked until you approve the exact call. Cards do not expire.'
              })
            ]
          }),
          jsx('button', {
            type: 'button',
            className: 'rounded border border-(--ui-stroke-secondary) px-2.5 py-1 text-xs',
            onClick: () => void refresh(),
            children: 'Refresh'
          })
        ]
      }),
      jsx('div', {
        className: 'text-xs text-(--ui-text-tertiary)',
        children: `${pendingCount} pending · ${requests.length} shown`
      }),
      ownerError && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) p-3 text-sm text-(--ui-danger)',
        children: `Owner control could not initialize. ${ownerError}`
      }),
      error && jsx('div', {
        role: 'alert',
        className: 'rounded bg-(--ui-danger-bg) p-3 text-sm text-(--ui-danger)',
        children: error
      }),
      loading && jsx('div', {
        className: 'text-sm text-(--ui-text-tertiary)',
        children: 'Loading approval cards…'
      }),
      !loading && !requests.length && jsx('div', {
        className: 'rounded-lg border border-dashed border-(--ui-stroke-secondary) p-6 text-sm text-(--ui-text-tertiary)',
        children: 'No approval requests yet.'
      }),
      jsx('section', {
        className: 'grid gap-3',
        children: requests.map(request =>
          jsx(ApprovalCard, {
            ctx,
            request,
            ownerToken,
            onChanged: refresh
          }, request.id)
        )
      })
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
    onClick: () => host.navigate('/human-gate'),
    'aria-label': `${count} pending Human Gate approvals`,
    children: `gate ${count}`
  })
}

export default {
  id: 'human-gate',
  name: 'Human Gate',
  defaultEnabled: true,
  register(ctx) {
    trackFocusedOwner(ctx)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/human-gate' },
        render: () => jsx(HumanGatePage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/human-gate', label: 'Human Gate', codicon: 'shield' }
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
          id: 'human-gate.open',
          label: 'Open Human Gate approvals',
          keywords: ['approval', 'gate', 'human'],
          run: () => host.navigate('/human-gate')
        }
      }
    ])
  }
}
