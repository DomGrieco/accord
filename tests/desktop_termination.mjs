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

const policyEditorKey = plugin.namespace.policyEditorKey
assert.equal(typeof policyEditorKey, 'function')
assert.equal(policyEditorKey(3, { tool_name: '' }), policyEditorKey(3, { tool_name: 'terminal' }))
assert.equal(policyEditorKey(3, { tool_glob: 'records_*' }), policyEditorKey(3, { tool_glob: '*_write' }))
assert.notEqual(policyEditorKey(3, {}), policyEditorKey(4, {}))

const toolOptions = [
  { name: 'terminal', toolset: 'terminal', description: 'Run a command', fields: ['command', 'workdir'] },
  { name: 'x_create_post', toolset: 'x', description: 'Publish a post', fields: ['account', 'text'] },
  { name: 'x_search', toolset: 'x', description: 'Search posts', fields: ['account', 'limit', 'query'] },
  { name: ']value', toolset: 'fixture', description: 'Fixture', fields: [] },
  { name: '[value', toolset: 'fixture', description: 'Fixture', fields: [] },
  { name: '[abc', toolset: 'fixture', description: 'Fixture', fields: [] },
  { name: '^value', toolset: 'fixture', description: 'Fixture', fields: [] },
  { name: 'alpha', toolset: 'fixture', description: 'Fixture', fields: [] }
]
const matchingToolOptions = plugin.namespace.matchingToolOptions
assert.equal(typeof matchingToolOptions, 'function')
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, 'terminal', 'exact'))).map(tool => tool.name),
  ['terminal']
)
assert.deepEqual(JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, ' terminal ', 'exact'))), [])
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, 'x_*', 'glob'))).map(tool => tool.name),
  ['x_create_post', 'x_search']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, 'x_[cs]*', 'glob'))).map(tool => tool.name),
  ['x_create_post', 'x_search']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[!x]*', 'glob'))).map(tool => tool.name),
  ['[abc', '[value', ']value', '^value', 'alpha', 'terminal']
)
assert.deepEqual(JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[z-a]*', 'glob'))), [])
assert.deepEqual(JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[', 'glob'))), [])
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[]a]*', 'glob'))).map(tool => tool.name),
  [']value', 'alpha']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[[]*', 'glob'))).map(tool => tool.name),
  ['[abc', '[value']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[^x]*', 'glob'))).map(tool => tool.name),
  ['^value', 'x_create_post', 'x_search']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(toolOptions, '[abc', 'glob'))).map(tool => tool.name),
  ['[abc']
)
const rangeEdgeTools = ['😀', 'a', 'é', '-', '!'].map(name => ({ name, fields: [] }))
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(rangeEdgeTools, '[!-éaa]', 'glob'))).map(tool => tool.name).sort(),
  ['!', '😀']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(rangeEdgeTools, '[--!!]', 'glob'))).map(tool => tool.name).sort(),
  ['!', '-', 'a', 'é', '😀']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(matchingToolOptions(rangeEdgeTools, '[!^]', 'glob'))).map(tool => tool.name).sort(),
  ['!', '-', 'a', 'é', '😀']
)

const policyFieldOptions = plugin.namespace.policyFieldOptions
assert.equal(typeof policyFieldOptions, 'function')
assert.deepEqual(
  JSON.parse(JSON.stringify(policyFieldOptions(toolOptions, 'terminal', 'exact'))),
  ['command', 'workdir']
)
assert.deepEqual(
  JSON.parse(JSON.stringify(policyFieldOptions(toolOptions, 'x_*', 'glob'))),
  ['account']
)

const availableReplayFields = plugin.namespace.availableReplayFields
assert.equal(typeof availableReplayFields, 'function')
assert.deepEqual(
  JSON.parse(JSON.stringify(availableReplayFields(['record_id', 'summary'], ['record_id']))),
  ['summary']
)

const filterAndSortRequests = plugin.namespace.filterAndSortRequests
assert.equal(typeof filterAndSortRequests, 'function')
const approvalRows = [
  {
    id: 'new-terminal',
    state: 'pending',
    effect_kind: 'local_command',
    profile: 'life',
    tool_name: 'terminal',
    display: { command: 'git status --short' },
    created_at: '2026-08-30T12:00:00Z'
  },
  {
    id: 'old-publish',
    state: 'executed',
    effect_kind: 'publish',
    profile: 'life',
    tool_name: 'x_create_post',
    display: { account: 'FixtureAccount', text: 'Fixture post' },
    created_at: '2026-08-29T12:00:00Z'
  }
]
assert.deepEqual(
  JSON.parse(JSON.stringify(filterAndSortRequests(approvalRows, {
    query: 'git status',
    state: 'all',
    effect: 'all',
    sort: 'newest'
  }))),
  [approvalRows[0]]
)
assert.deepEqual(
  JSON.parse(JSON.stringify(filterAndSortRequests(approvalRows, {
    query: '',
    state: 'executed',
    effect: 'publish',
    sort: 'oldest'
  }))),
  [approvalRows[1]]
)
assert.deepEqual(
  JSON.parse(JSON.stringify(filterAndSortRequests(approvalRows, {
    query: '',
    state: 'all',
    effect: 'all',
    sort: 'tool'
  }))).map(row => row.tool_name),
  ['terminal', 'x_create_post']
)

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
  path: '/api/plugins/accord/requests?state=pending',
  profile: 'life',
  connectionId: 'local'
}])
assert.equal(restCalls.length, 0)

apiCalls.length = 0
context.window.hermesDesktop.api = async request => {
  apiCalls.push(request)
  if (String(request.path).startsWith('/api/plugins/accord/')) {
    throw new Error('Error invoking remote method hermes:api: Error: 404: Plugin not found')
  }
  return { requests: [{ id: 'legacy' }] }
}
assert.deepEqual(
  JSON.parse(JSON.stringify(await pluginRest(ctx, '/requests?state=pending'))),
  { requests: [{ id: 'legacy' }] }
)
assert.equal(apiCalls.at(-1).path, '/api/plugins/human-gate/requests?state=pending')
context.window.hermesDesktop.api = async request => {
  apiCalls.push(request)
  return { requests: [] }
}

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
assert.equal(apiCalls.at(-1).path, '/api/plugins/accord/requests?state=pending')

const rememberFocusedOwner = plugin.namespace.rememberFocusedOwner
rememberFocusedOwner({ profile: 'life', connectionId: 'local' })
host.state.focusedSessionOwner = { get: () => null }
host.state.focusedSessionProfile = { get: () => '' }
apiCalls.length = 0
await pluginRest(ctx, '/requests?state=pending')
assert.deepEqual(JSON.parse(JSON.stringify(apiCalls)), [{
  path: '/api/plugins/accord/requests?state=pending',
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
host.requestProfile = async (_selected, method) => {
  if (method === 'session.resume') {
    return { session_id: 'runtime-resumed', session_key: 'continuation-tip' }
  }
  throw new Error(`prompt.submit must not run without a resolved resume target: ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /did not identify its resolved stored session/
)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')

restCalls.length = 0
host.requestProfile = async (_selected, method) => {
  if (method === 'session.resume') {
    return {
      session_id: 'runtime-resumed',
      session_key: 'continuation-tip',
      resumed: 'different-tip'
    }
  }
  throw new Error(`prompt.submit must not run after mismatched lineage proof: ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /returned conflicting stored session identities/
)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')

restCalls.length = 0
host.requestProfile = async (_selected, method) => {
  if (method === 'session.resume') {
    return {
      session_id: 'runtime-resumed',
      session_key: 'continuation-tip',
      resumed: 'continuation-tip'
    }
  }
  throw new Error(`prompt.submit must not run without a requested session echo: ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /did not identify the requested stored session/
)
assert.equal(restCalls.length, 1)
assert.equal(restCalls[0].path, '/requests/request-1/resume-failed')

restCalls.length = 0
host.requestProfile = async (_selected, method) => {
  if (method === 'session.resume') {
    return {
      session_id: 'runtime-resumed',
      session_key: 'continuation-tip',
      resumed: 'continuation-tip',
      requested_session_id: 'different-request'
    }
  }
  throw new Error(`prompt.submit must not run after requested session mismatch: ${method}`)
}
await assert.rejects(
  submitResume(ctx, resume, 'owner-token'),
  /resumed a different stored session than requested/
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
    return {
      session_id: 'runtime-resumed',
      session_key: 'continuation-tip',
      resumed: 'continuation-tip',
      requested_session_id: 'stored'
    }
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
    return {
      session_id: 'runtime-resumed',
      session_key: 'continuation-tip',
      resumed: 'continuation-tip',
      requested_session_id: 'stored'
    }
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

const submitDecisionAndResume = plugin.namespace.submitDecisionAndResume
assert.equal(typeof submitDecisionAndResume, 'function')

for (const decision of ['approve', 'comment', 'deny', 'cancel']) {
  const id = `matrix-${decision}`
  const comment = decision === 'comment' ? 'Use the smaller scope.' : ''
  const matrixRest = []
  const matrixRpc = []
  ctx.rest = async (path, options = {}) => {
    matrixRest.push({ path, options })
    if (path === `/requests/${id}/resume-instruction`) {
      return { resume: {
        profile: 'life',
        stored_session_id: `stored-${decision}`,
        request_id: id,
        record_version: 2,
        prompt: `Human decision: ${decision}. ${comment}`
      } }
    }
    return { request: { id, state: decision } }
  }
  host.state.focusedSessionOwner = {
    get: () => ({ authoritative: true, connectionId: 'local', profile: 'life' })
  }
  host.state.focusedSessionProfile = { get: () => 'life' }
  host.state.profile = { get: () => 'life' }
  host.state.connectionId = { get: () => 'local' }
  host.profileRoutes = async () => [route]
  host.retainProfile = async () => () => { released += 1 }
  host.requestProfile = async (_selected, method, params) => {
    matrixRpc.push({ method, params })
    if (method === 'session.resume') {
      return {
        session_id: `runtime-${decision}`,
        session_key: `stored-${decision}`,
        resumed: `stored-${decision}`,
        requested_session_id: `stored-${decision}`
      }
    }
    if (method === 'prompt.submit') return { accepted: true }
    throw new Error(`unexpected matrix method ${method}`)
  }

  await submitDecisionAndResume({
    ctx,
    request: { id, call_digest: 'a'.repeat(64), record_version: 1 },
    decision,
    comment,
    ownerToken: 'owner-token'
  })

  assert.deepEqual(matrixRest.map(call => call.path), [
    `/requests/${id}/decision`,
    `/requests/${id}/resume-instruction`,
    `/requests/${id}/resume-target`,
    `/requests/${id}/resume-ack`
  ])
  assert.deepEqual(JSON.parse(JSON.stringify(matrixRest[0].options.body)), {
    token: 'owner-token', decision, comment, digest: 'a'.repeat(64), record_version: 1
  })
  assert.deepEqual(matrixRpc.map(call => call.method), ['session.resume', 'prompt.submit'])
  assert.equal(matrixRpc[1].params.display_kind, 'hidden')
  if (decision === 'comment') assert.match(matrixRpc[1].params.text, /smaller scope/)
}

console.log('Desktop decision matrix passed: approve, request changes, deny, cancel, resume failures')
