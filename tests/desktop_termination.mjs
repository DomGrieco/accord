import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm'

const context = createContext({ console, crypto, setInterval, clearInterval })
const source = await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
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
  prompt: 'continue'
}
const restCalls = []
const ctx = {
  rest: async (path, options) => {
    restCalls.push({ path, options })
    return { request: { resume_state: 'failed' } }
  }
}

host.profileRoutes = async () => []
await assert.rejects(submitResume(ctx, resume, 'owner-token'), /Expected one route/)
assert.deepEqual(JSON.parse(JSON.stringify(restCalls)), [{
  path: '/requests/request-1/resume-failed',
  options: { method: 'POST', body: { token: 'owner-token' } }
}])

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
