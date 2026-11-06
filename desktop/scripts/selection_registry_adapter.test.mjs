import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('../src/main/selection_registry.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const handlers = new Map()
const calls = []
let response = { ok: true }
const mocks = {
  electron: { ipcMain: { handle: (name, handler) => handlers.set(name, handler) } },
  './app-dirs': { getClientPaths: () => ({ databasePath: 'D:\\software\\codex\\registry-isolated.sqlite' }) },
  './engine': {
    runEngineCommand: async (script, args) => {
      calls.push({ script, args })
      return response
    }
  }
}
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(moduleValue.exports, (name) => {
  assert.ok(Object.hasOwn(mocks, name), `unexpected import: ${name}`)
  return mocks[name]
}, moduleValue)
const registry = moduleValue.exports
registry.registerSelectionRegistryIpc()

function reset(result) {
  calls.length = 0
  response = { ok: true, result }
}

test('本机快照使用本地账号 ID 和 SQLite，不访问共享登记服务', async () => {
  reset({ ok: true, usedProductIds: ['p-1'], accountUsedCategoryIds: ['c-1'], categoryUsage: { 'c-1': 2 } })
  const snapshot = await handlers.get('selection-registry:snapshot')({}, 'local:42', [
    { productId: 'p-2', leafCategoryId: 'c-2' }
  ])
  assert.deepEqual(snapshot, {
    usedProductIds: ['p-1'], accountUsedCategoryIds: ['c-1'], categoryUsage: { 'c-1': 2 }, mode: 'local'
  })
  assert.deepEqual(calls, [{
    script: 'selection_registry_local.py',
    args: ['--db', 'D:\\software\\codex\\registry-isolated.sqlite', 'snapshot', '--account-key', 'local:42']
  }])
})

test('预占候选带完整商品与类目，开始和结束沿用同一令牌', async () => {
  reset({ ok: true, reserved: [{ productId: 'p-2', token: 'token-1', state: 'RESERVED' }], rejected: [] })
  const result = await registry.reserveRegistryCandidates('local:42', 'request-1', [
    { productId: 'p-2', leafCategoryId: 'c-2' }
  ], 1)
  assert.equal(result.mode, 'local')
  assert.deepEqual(result.reserved.map((item) => item.token), ['token-1'])
  assert.deepEqual(calls[0].args.slice(2), [
    'reserve', '--account-key', 'local:42', '--request-id', 'request-1',
    '--items-json', '[{"productId":"p-2","leafCategoryId":"c-2"}]', '--max-reserved', '1'
  ])
  reset({ ok: true })
  await registry.beginRegistryReservation('token-1')
  await registry.finishRegistryReservation('token-1', 'ADDED')
  assert.deepEqual(calls.map((call) => call.args.slice(2)), [
    ['begin', '--token', 'token-1'],
    ['finish', '--token', 'token-1', '--outcome', 'ADDED']
  ])
})

test('缺少本地账号、请求标识或候选时直接拒绝，失败命令不返回可用快照', async () => {
  reset({ ok: true })
  await assert.rejects(registry.getRegistrySnapshot('account-42', []), /本机选品登记缺少有效/)
  await assert.rejects(registry.reserveRegistryCandidates('local:42', ' ', [{ productId: 'p', leafCategoryId: 'c' }]), /请求标识为空/)
  await assert.rejects(registry.reserveRegistryCandidates('local:42', 'request-1', []), /没有完整商品 ID/)
  assert.equal(calls.length, 0)
  response = { ok: false, error: '本机数据库不可用' }
  await assert.rejects(registry.getRegistrySnapshot('local:42', []), /本机数据库不可用/)
})
