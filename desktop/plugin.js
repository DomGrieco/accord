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

const ACTIVE_STATES = 'pending,approved,changes_requested,denied,claimed,executed,failed,uncertain'

function randomOwnerToken() {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
}

async function ensureOwner(ctx) {
  let token = ctx.storage.get('ownerToken', '')
  if (!token) {
    token = randomOwnerToken()
    ctx.storage.set('ownerToken', token)
  }
  await ctx.rest('/owner/register', {
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
      const result = await ctx.rest(`/requests?state=${encodeURIComponent(states)}&limit=200`)
      setRequests(Array.isArray(result?.requests) ? result.requests : [])
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

async function submitResume(ctx, resume, token) {
  const route = await profileRoute(resume.profile)
  const release = await host.retainProfile(route)
  try {
    const resumed = await host.requestProfile(route, 'session.resume', {
      session_id: resume.stored_session_id,
      profile: route.targetProfile,
      omit_messages: true
    })
    const runtimeSessionId = String(resumed?.session_id || '')
    const storedSessionId = String(resumed?.session_key || '')
    if (!runtimeSessionId) {
      throw new Error('The stored session resumed without an active runtime.')
    }
    if (storedSessionId && storedSessionId !== resume.stored_session_id) {
      throw new Error('The resumed runtime did not preserve the stored session lineage.')
    }
    await host.requestProfile(route, 'prompt.submit', {
      session_id: runtimeSessionId,
      prompt: [{ type: 'text', text: resume.prompt }],
      display_kind: 'hidden'
    })
    await ctx.rest(`/requests/${resume.request_id}/resume-ack`, {
      method: 'POST',
      body: { token }
    })
  } finally {
    release()
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
      const result = await ctx.rest(`/requests/${request.id}/decision`, {
        method: 'POST',
        body: {
          token: ownerToken,
          decision,
          comment,
          digest: request.call_digest,
          record_version: request.record_version
        }
      })
      if (result.resume) {
        try {
          await submitResume(ctx, result.resume, ownerToken)
        } catch (resumeError) {
          await ctx.rest(`/requests/${request.id}/resume-failed`, {
            method: 'POST',
            body: { token: ownerToken }
          }).catch(() => undefined)
          throw resumeError
        }
      } else if (result.terminate) {
        await terminateSession(result.terminate)
      }
      haptic(decision === 'approve' ? 'success' : 'tap')
      host.notify({
        kind: decision === 'approve' ? 'success' : 'info',
        message: decision === 'approve'
          ? `Approved ${request.tool_name}; the originating session is resuming.`
          : decision === 'deny'
            ? `Denied ${request.tool_name}; the originating session is stopped.`
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
  }, [comment, ctx, onChanged, ownerToken, request.id, request.tool_name])

  const retryResume = useCallback(async () => {
    setBusy('resume')
    setError('')
    try {
      const result = await ctx.rest(`/requests/${request.id}/resume-instruction`, {
        method: 'POST',
        body: { token: ownerToken }
      })
      try {
        await submitResume(ctx, result.resume, ownerToken)
      } catch (resumeError) {
        await ctx.rest(`/requests/${request.id}/resume-failed`, {
          method: 'POST',
          body: { token: ownerToken }
        }).catch(() => undefined)
        throw resumeError
      }
      host.notify({ kind: 'success', message: 'The originating session is resuming.' })
      await onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      await onChanged()
    } finally {
      setBusy('')
    }
  }, [ctx, onChanged, ownerToken, request.id])

  const retryTermination = useCallback(async () => {
    setBusy('terminate')
    setError('')
    try {
      const result = await ctx.rest(`/requests/${request.id}/termination-instruction`, {
        method: 'POST',
        body: { token: ownerToken }
      })
      const closed = await terminateSession(result.terminate)
      host.notify({
        kind: 'success',
        message: closed
          ? 'The originating session is stopped.'
          : 'The originating session was already stopped.'
      })
      await onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy('')
    }
  }, [ctx, onChanged, ownerToken, request.id])

  const pending = request.state === 'pending'
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
                className: 'rounded border border-(--ui-danger) px-3 py-1.5 text-sm text-(--ui-danger) disabled:opacity-50',
                disabled: Boolean(busy),
                onClick: () => void decide('deny'),
                children: busy === 'deny' ? 'Denying…' : 'Deny'
              })
            ]
          })
        ]
      }),
      request.resume_state === 'failed' && jsx('button', {
        type: 'button',
        className: 'w-fit rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
        disabled: Boolean(busy),
        onClick: () => void retryResume(),
        children: busy === 'resume' ? 'Waking session…' : 'Retry session wake'
      }),
      request.state === 'denied' && jsx('button', {
        type: 'button',
        className: 'w-fit rounded border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
        disabled: Boolean(busy),
        onClick: () => void retryTermination(),
        children: busy === 'terminate' ? 'Stopping session…' : 'Ensure session stopped'
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
