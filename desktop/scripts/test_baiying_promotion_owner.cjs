const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const Module = require('node:module')
const ts = require('typescript')

const sourcePath = path.resolve(__dirname, '../src/main/baiying_batch.ts')
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true }
}).outputText
const productId = '3827855045362319495'
const commodityId = '3827965688014831943'
const handlers = new Map()
let scenario
let discoveredAccountIds

function reply(result) { return { promise: Promise.resolve({ ok: true, result }) } }

const stubs = {
  electron: { clipboard: { readText: () => '' }, ipcMain: { handle(name, handler) { handlers.set(name, handler) } } },
  './probe': { onProductCaptured() {} },
  './external_browser': {
    validateManagedProfilePath: (key) => `D:\\profiles\\${key}`,
    commandManagedBrowser: async () => ({ ok: true, baiying: { state: 'logged_in' } })
  },
  './app-dirs': {
    getClientPaths: () => ({ databasePath: 'D:\\client\\client.sqlite' }),
    getDownloadDir: () => 'D:\\client\\downloads'
  },
  './engine': {
    runScript(script, options) {
      if (script === 'account_manager.py') {
        return reply({ accounts: scenario.activeIds.map((id) => ({ id, name: `账号 ${id}`, status: 'active' })) })
      }
      if (script === 'selection_settings.py') return reply({ settings: { account: {} } })
      if (script === 'baiying_promotion_videos.py') {
        if (options.args.includes('--resolve-account')) {
          return reply({ found: true, accountId: 6, commodityId, accountSource: scenario.source })
        }
        if (options.args.includes('--find-open-product')) {
          const profile = options.args[options.args.indexOf('--profile-path') + 1]
          const id = Number(profile.match(/account-(\d+)$/)[1])
          return reply(scenario.openId === id ? { found: true, commodityId } : { found: false })
        }
        const accountId = Number(options.args[options.args.indexOf('--account-id') + 1])
        discoveredAccountIds.push(accountId)
        return reply({ ok: true, productId, commodityId, videos: [{ videoId: '7694555774554623272', url: 'https://example.test/video.mp4' }] })
      }
      if (script === 'baiying_cache.py') return reply({ saved: true })
      throw new Error(`unexpected script: ${script}`)
    }
  }
}

const loaded = new Module(sourcePath, module)
loaded.filename = sourcePath
loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath))
const originalLoad = Module._load
Module._load = function (request, parent, isMain) {
  if (parent && parent.filename === sourcePath && Object.prototype.hasOwnProperty.call(stubs, request)) return stubs[request]
  return originalLoad.call(this, request, parent, isMain)
}
try { loaded._compile(compiled, sourcePath) }
finally { Module._load = originalLoad }
loaded.exports.registerBaiyingBatchIpc()

async function query(nextScenario) {
  scenario = nextScenario
  discoveredAccountIds = []
  const result = await handlers.get('baiying:promotionVideos')(null, { productId })
  return { result, discoveredAccountIds }
}

async function main() {
  const fallback = await query({ source: '百应浏览缓存', activeIds: [1] })
  assert.equal(fallback.result.ok, true)
  assert.equal(fallback.result.accountId, 1)
  assert.match(fallback.result.accountSource, /唯一启用账号/)
  assert.deepEqual(fallback.discoveredAccountIds, [1])

  const selected = await query({ source: '选品登记', activeIds: [1] })
  assert.equal(selected.result.ok, false)
  assert.match(selected.result.error, /启用原账号/)
  assert.deepEqual(selected.discoveredAccountIds, [])

  const opened = await query({ source: '百应浏览缓存', activeIds: [1, 2], openId: 2 })
  assert.equal(opened.result.ok, true)
  assert.equal(opened.result.accountId, 2)
  assert.deepEqual(opened.discoveredAccountIds, [2])

  const ambiguous = await query({ source: '百应浏览缓存', activeIds: [1, 2] })
  assert.equal(ambiguous.result.ok, false)
  assert.match(ambiguous.result.error, /无法自动确定下载账号/)
  assert.deepEqual(ambiguous.discoveredAccountIds, [])
  process.stdout.write('Baiying promotion owner fallback: PASS\n')
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`)
  process.exitCode = 1
})
