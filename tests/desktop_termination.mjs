import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm'

const context = createContext({ console, crypto, setInterval, clearInterval })
const source = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
assert.match(
  source,
  /const ACTIVE_STATES = '[^']*cancelled[^']*'/
)
const plugin = new SourceTextModule(source, { context })

function synthetic(exports) {
  const names = Object.keys(exports)
  return new SyntheticModule(names, function () {
    for (const name of names) this.setExport(name, exports[name])
  }, { context })
}

const host = {}
const modules = {
  '@hermes/plugin-sdk': synthetic({
    PALETTE_AREA: 'palette',
    ROUTES_AREA: 'routes',
    SIDEBAR_NAV_AREA: 'sidebar',
    STATUSBAR_AREAS: { right: 'right' },
    haptic: () => undefined,
    host
  }),
  react: synthetic({
    useCallback: value => value,
    useEffect: () => undefined,
    useMemo: value => value(),
    useState: value => [value, () => undefined]
  }),
  'react/jsx-runtime': synthetic({
    jsx: () => null,
    jsxs: () => null
  })
}

await plugin.link(specifier => modules[specifier])
await plugin.evaluate()
const cancellationLabel = plugin.namespace.approvalCancellationLabel
assert.equal(cancellationLabel('approved'), 'Revoke approval')
assert.equal(cancellationLabel('changes_requested'), 'Cancel request')
assert.equal(cancellationLabel('pending'), '')
assert.equal(cancellationLabel('claimed'), '')

const activeInboxRequests = plugin.namespace.activeInboxRequests
assert.equal(typeof activeInboxRequests, 'function')
const activePending = { id: 'pending', state: 'pending', resume_state: 'not_requested' }
const cancelledFailed = { id: 'cancelled-failed', state: 'cancelled', resume_state: 'failed' }
const cancelledDelivered = { id: 'cancelled-delivered', state: 'cancelled', resume_state: 'delivered' }
assert.deepEqual(
  JSON.parse(JSON.stringify(activeInboxRequests([
    activePending,
    cancelledFailed,
    cancelledDelivered
  ]))),
  [activePending, cancelledFailed]
)
assert.deepEqual(JSON.parse(JSON.stringify(activeInboxRequests(null))), [])

const retrySessionWakeLabel = plugin.namespace.retrySessionWakeLabel
assert.equal(retrySessionWakeLabel(cancelledFailed), 'Retry session wake')
assert.equal(retrySessionWakeLabel(cancelledDelivered), '')

const select = plugin.namespace.selectRuntimeToClose

assert.throws(() => select(null, 'stored'), /valid sessions array/)
assert.throws(() => select({}, 'stored'), /valid sessions array/)
assert.throws(() => select({ sessions: 'bad' }, 'stored'), /valid sessions array/)
assert.throws(() => select({ sessions: [{}] }, 'stored'), /valid id and session_key/)
assert.throws(
  () => select({ sessions: [{ id: 'runtime', session_key: '' }] }, 'stored'),
  /valid id and session_key/
)
assert.throws(
  () => select({ sessions: [{ id: '', session_key: 'other' }] }, 'stored'),
  /valid id and session_key/
)
assert.equal(select({ sessions: [] }, 'stored'), null)
assert.equal(select({ sessions: [{ id: 'runtime', session_key: 'other' }] }, 'stored'), null)
assert.equal(
  select({ sessions: [{ id: 'runtime', session_key: 'stored' }] }, 'stored'),
  'runtime'
)
assert.throws(
  () => select({ sessions: [{ id: '', session_key: 'stored' }] }, 'stored'),
  /valid id and session_key/
)
assert.throws(
  () => select({ sessions: [
    { id: 'one', session_key: 'stored' },
    { id: 'two', session_key: 'stored' }
  ] }, 'stored'),
  /More than one live runtime/
)

const route = { targetProfile: 'life', connectionId: 'local' }
const calls = []
let released = 0
host.profileRoutes = async () => [route]
host.retainProfile = async selected => {
  assert.equal(selected, route)
  return () => { released += 1 }
}
host.requestProfile = async (selected, method, params) => {
  calls.push({ selected, method, params })
  if (method === 'session.active_list') {
    return { sessions: [{ id: 'runtime', session_key: 'stored' }] }
  }
  if (method === 'session.close') return { closed: true }
  throw new Error(`unexpected method ${method}`)
}

const terminate = plugin.namespace.terminateSession
assert.equal(await terminate({ profile: 'life', stored_session_id: 'stored' }), true)
assert.deepEqual(JSON.parse(JSON.stringify(calls.map(call => [call.method, call.params]))), [
  ['session.active_list', {}],
  ['session.close', { session_id: 'runtime' }]
])
assert.equal(released, 1)

calls.length = 0
host.requestProfile = async (selected, method, params) => {
  calls.push({ selected, method, params })
  return { sessions: [] }
}
assert.equal(await terminate({ profile: 'life', stored_session_id: 'stored' }), false)
assert.deepEqual(calls.map(call => call.method), ['session.active_list'])
assert.equal(released, 2)

host.requestProfile = async () => ({ bad: true })
await assert.rejects(
  terminate({ profile: 'life', stored_session_id: 'stored' }),
  /valid sessions array/
)
assert.equal(released, 3)

const submitResume = plugin.namespace.submitResume
assert.equal(typeof submitResume, 'function')
const resume = {
  profile: 'life',
  stored_session_id: 'stored',
  request_id: 'request-1',
  record_version: 7,
  prompt: 'continue'
}
const restCalls = []
const ctx = {
  rest: async (path, options) => {
    restCalls.push({ path, options })
    return { request: { resume_state: 'failed' } }
  }
}

await assert.rejects(
  submitResume(ctx, { ...resume, prompt: '   ' }, 'owner-token'),
  /nonempty decision prompt/
)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')
assert.equal(restCalls[0].options.body.record_version, 7)
restCalls.length = 0

const pluginRest = plugin.namespace.profileRest
assert.equal(typeof pluginRest, 'function')
const apiCalls = []
context.window = {
  hermesDesktop: {
    api: async request => {
      apiCalls.push(request)
      return { requests: [] }
    }
  }
}
host.state = {
  connectionId: { get: () => null },
  focusedSessionOwner: {
    get: () => ({ authoritative: true, connectionId: 'local', profile: 'life' })
  },
  focusedSessionProfile: { get: () => 'life' },
  profile: { get: () => 'default' }
}
assert.deepEqual(
  JSON.parse(JSON.stringify(await pluginRest(ctx, '/requests?state=pending'))),
  { requests: [] }
)
assert.deepEqual(JSON.parse(JSON.stringify(apiCalls)), [{
  path: '/api/plugins/human-gate/requests?state=pending',
  profile: 'life',
  connectionId: 'local'
}])
assert.equal(restCalls.length, 0)

host.state.focusedSessionOwner = {
  get: () => ({ authoritative: true, connectionId: 'local', profile: 'splashifax' })
}
host.state.focusedSessionProfile = { get: () => 'splashifax' }
host.profileRoutes = async () => [
  { targetProfile: 'splashifax', connectionId: 'local' },
  { targetProfile: 'paperclip', connectionId: 'local' },
  route
]
apiCalls.length = 0
context.window.hermesDesktop.api = async request => {
  apiCalls.push(request)
  if (request.profile === 'splashifax') {
    throw new Error('Error invoking remote method hermes:api: Error: 404: Plugin not found')
  }
  if (request.profile === 'paperclip') {
    throw new Error('Error invoking remote method hermes:api: Error: 404: {"detail":"Not Found"}')
  }
  return { requests: [] }
}
assert.deepEqual(
  JSON.parse(JSON.stringify(await pluginRest(ctx, '/requests?state=pending'))),
  { requests: [] }
)
assert.equal(apiCalls.at(-1).profile, 'life')
assert.equal(apiCalls.at(-1).path, '/api/plugins/human-gate/requests?state=pending')

const rememberFocusedOwner = plugin.namespace.rememberFocusedOwner
rememberFocusedOwner({ profile: 'life', connectionId: 'local' })
host.state.focusedSessionOwner = { get: () => null }
host.state.focusedSessionProfile = { get: () => '' }
apiCalls.length = 0
await pluginRest(ctx, '/requests?state=pending')
assert.deepEqual(JSON.parse(JSON.stringify(apiCalls)), [{
  path: '/api/plugins/human-gate/requests?state=pending',
  profile: 'life',
  connectionId: 'local'
}])

await assert.rejects(pluginRest(ctx, '/../config'), /path traversal rejected/)
rememberFocusedOwner({ profile: 'default', connectionId: 'local' })
host.state.focusedSessionOwner = { get: () => null }
host.state.focusedSessionProfile = { get: () => 'life' }
await assert.rejects(pluginRest(ctx, '/requests'), /focused session owner could not be resolved/)
host.state.focusedSessionOwner = {
  get: () => ({ authoritative: true, connectionId: 'local', profile: 'life' })
}
host.state.profile = { get: () => 'life' }

host.profileRoutes = async () => []
await assert.rejects(submitResume(ctx, resume, 'owner-token'), /Expected one route/)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')
assert.equal(restCalls[0].options.method, 'POST')
assert.equal(restCalls[0].options.body.token, 'owner-token')
assert.equal(restCalls[0].options.body.record_version, 7)
assert.match(restCalls[0].options.body.error, /Expected one route for profile life, found 0/)

restCalls.length = 0
host.profileRoutes = async () => [route]
host.retainProfile = async () => {
  throw new Error('profile unavailable')
}
await assert.rejects(submitResume(ctx, resume, 'owner-token'), /profile unavailable/)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')

restCalls.length = 0
host.retainProfile = async () => () => { released += 1 }
host.requestProfile = async () => {
  throw new Error('wake response lost')
}
await assert.rejects(submitResume(ctx, resume, 'owner-token'), /wake response lost/)
assert.equal(restCalls.length, 0)
assert.equal(released, 4)

restCalls.length = 0
host.profileRoutes = async () => [route]
host.retainProfile = async () => () => { released += 1 }
host.requestProfile = async (_selected, method) => {
  if (method === 'session.resume') return { session_id: 'runtime-without-lineage' }
  throw new Error(`prompt.submit must not run after unproven lineage: ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /did not prove the stored session lineage/
)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')

restCalls.length = 0
const promptCalls = []
host.profileRoutes = async () => [route]
host.retainProfile = async () => () => { released += 1 }
host.requestProfile = async (selected, method, params) => {
  promptCalls.push({ selected, method, params })
  if (method === 'session.resume') {
    return { session_id: 'runtime-resumed', session_key: 'continuation-tip' }
  }
  if (method === 'prompt.submit') {
    throw new Error('Error invoking remote method: active session limit (3/3)')
  }
  throw new Error(`unexpected method ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /active session limit \(3\/3\)/
)
assert.equal(promptCalls[1].method, 'prompt.submit')
assert.deepEqual(JSON.parse(JSON.stringify(promptCalls[1].params)), {
  session_id: 'runtime-resumed',
  text: 'continue',
  display_kind: 'hidden'
})
assert.equal(restCalls.length, 2)
assert.equal(restCalls[0].path, '/requests/request-1/resume-target')
assert.equal(restCalls[0].options.body.session_id, 'continuation-tip')
assert.equal(restCalls[1].path, '/requests/request-1/resume-failed')
assert.match(restCalls[1].options.body.error, /active session limit \(3\/3\)/)
assert.equal(restCalls[1].options.body.record_version, 7)

restCalls.length = 0
host.requestProfile = async (_selected, method) => {
  if (method === 'session.resume') {
    return { session_id: 'runtime-resumed', session_key: 'continuation-tip' }
  }
  if (method === 'prompt.submit') throw new Error('transport response lost after wake')
  throw new Error(`unexpected method ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /transport response lost after wake/
)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-target')
