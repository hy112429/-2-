import assert from 'node:assert/strict'
import { mkdir, readFile, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { isAbsolute, join, resolve, sep } from 'node:path'
import { test, before, after } from 'node:test'
import ts from 'typescript'

const testDataDir = join('D:\\software\\codex', `selection-registry-adapter-test-${process.pid}-${Date.now()}`)
const intendedTestRoot = resolve('D:\\software\\codex')
const resolvedTestDataDir = resolve(testDataDir)
if (!isAbsolute(resolvedTestDataDir) || !resolvedTestDataDir.startsWith(`${intendedTestRoot}${sep}`)) {
  throw new Error(`Refusing to use unexpected test data path: ${resolvedTestDataDir}`)
}
const require = createRequire(import.meta.url)
const handlers = new Map()
const ipcMain = { handle: (name, handler) => handlers.set(name, handler) }
const safeStorage = {
  isEncryptionAvailable: () => true,
  encryptString: (value) => Buffer.from(value, 'utf8').toString('base64'),
  decryptString: (value) => Buffer.from(value.toString('utf8'), 'base64').toString('utf8')
}
const electron = { app: { getPath: () => testDataDir }, ipcMain, safeStorage }
const originalFetch = globalThis.fetch
let lastRequest

before(async () => {
  await mkdir(testDataDir, { recursive: true })
  const source = await readFile(new URL('../src/main/selection_registry.ts', import.meta.url), 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText
  const moduleValue = { exports: {} }
  const mockedRequire = (name) => name === 'electron' ? electron : require(name)
  new Function('exports', 'require', 'module', compiled)(moduleValue.exports, mockedRequire, moduleValue)
  moduleValue.exports.registerSelectionRegistryIpc()
})

after(async () => {
  globalThis.fetch = originalFetch
  await rm(testDataDir, { recursive: true, force: true })
})

const invoke = (name, ...args) => {
  const handler = handlers.get(name)
  assert.ok(handler, `IPC handler not registered: ${name}`)
  return handler({}, ...args)
}

const jsonResponse = (value) => ({
  ok: true,
  status: 200,
  json: async () => value,
  text: async () => JSON.stringify(value)
})

test('配置仅返回密钥状态，回环 HTTP 可用，检查连接会携带认证头', async () => {
  const saved = await invoke('selection-registry:set-config', {
    baseUrl: 'http://127.0.0.1:8094/',
    apiToken: 'local-test-token'
  })
  assert.deepEqual(saved, { configured: true, baseUrl: 'http://127.0.0.1:8094', tokenConfigured: true })
  assert.deepEqual(await invoke('selection-registry:get-config'), {
    configured: true,
    baseUrl: 'http://127.0.0.1:8094',
    tokenConfigured: true
  })

  const encryptedConfig = await readFile(join(testDataDir, 'selection-registry-config.bin'))
  assert.equal(encryptedConfig.toString('utf8').includes('local-test-token'), false)

  globalThis.fetch = async (url, init) => {
    lastRequest = { url: String(url), method: init?.method ?? 'GET', authorization: new Headers(init?.headers).get('Authorization') }
    return jsonResponse({ ok: true, service: 'selection-registry' })
  }
  assert.deepEqual(await invoke('selection-registry:check'), {
    ok: true,
    baseUrl: 'http://127.0.0.1:8094',
    service: 'selection-registry'
  })
  assert.deepEqual(lastRequest, {
    url: 'http://127.0.0.1:8094/api/v1/registry/status',
    method: 'GET',
    authorization: 'Bearer local-test-token'
  })
})

test('候选快照会清洗账号 ID、去重本轮商品/末级类目并传递认证密钥', async () => {
  await invoke('selection-registry:set-config', { baseUrl: 'http://localhost:8094' })
  globalThis.fetch = async (url, init) => {
    lastRequest = {
      url: String(url),
      method: init.method,
      authorization: new Headers(init.headers).get('Authorization'),
      body: JSON.parse(init.body)
    }
    return jsonResponse({
      usedProductIds: ['product-global-used'],
      accountUsedCategoryIds: ['category-history-used'],
      categoryUsage: { categoryA: 2 }
    })
  }

  const result = await invoke('selection-registry:snapshot', ' account-42 ', [
    { productId: 'product-1', leafCategoryId: 'category-A' },
    { productId: 'product-1', leafCategoryId: 'category-A' },
    { productId: 'product-2', leafCategoryId: 'category-B' },
    { productId: '', leafCategoryId: '' }
  ])

  assert.deepEqual(result, {
    usedProductIds: ['product-global-used'],
    accountUsedCategoryIds: ['category-history-used'],
    categoryUsage: { categoryA: 2 }
  })
  assert.deepEqual(lastRequest, {
    url: 'http://localhost:8094/api/v1/registry/snapshot',
    method: 'POST',
    authorization: 'Bearer local-test-token',
    body: { accountKey: 'account-42', productIds: ['product-1', 'product-2'], leafCategoryIds: ['category-A', 'category-B'] }
  })
})

test('缺少稳定账号 ID 时拒绝快照请求，非回环 HTTP 配置会被拦截', async () => {
  let fetchCalls = 0
  globalThis.fetch = async () => {
    fetchCalls += 1
    return jsonResponse({})
  }
  await assert.rejects(invoke('selection-registry:snapshot', '  ', []), /稳定抖音账号 ID/)
  assert.throws(
    () => invoke('selection-registry:set-config', { baseUrl: 'http://registry.example.internal:8094', apiToken: 'other-token' }),
    /必须使用 HTTPS/
  )
  assert.equal(fetchCalls, 0)
})
