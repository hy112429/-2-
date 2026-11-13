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
const firstVideoId = '7694555774554623272'
const secondVideoId = '7694555774554623273'
const calls = { discover: 0, existing: 0, download: 0, tasks: [] }
let finishDownload
const downloadStarted = new Promise((resolve) => { finishDownload = resolve })
const stubs = {
  electron: { clipboard: { readText: async () => '' }, ipcMain: { handle() {} } },
  './probe': { onProductCaptured() {} },
  './external_browser': {
    validateManagedProfilePath: () => 'D:\\profiles\\account-6',
    commandManagedBrowser: async () => ({ ok: true })
  },
  './app-dirs': {
    getClientPaths: () => ({ databasePath: 'D:\\client\\client.sqlite' }),
    getDownloadDir: () => 'D:\\client\\downloads'
  },
  './engine': {
    runScript(script, options) {
      if (script === 'account_manager.py') return { promise: Promise.resolve({ ok: true, result: {
        accounts: [{ id: 6, name: '测试账号', status: 'active' }]
      } }) }
      if (script === 'selection_settings.py') return { promise: Promise.resolve({ ok: true, result: {
        settings: { account: { downloadPath: 'D:\\client\\account-6' } }
      } }) }
      if (script === 'baiying_promotion_videos.py' && options.args.includes('--existing-videos')) {
        calls.existing++
        return { promise: Promise.resolve({ ok: true, result: { videoIds: [firstVideoId] } }) }
      }
      if (script === 'baiying_promotion_videos.py') {
        calls.discover++
        assert.equal(options.args[options.args.indexOf('--account-id') + 1], '6')
        assert.equal(options.args[options.args.indexOf('--commodity-id') + 1], commodityId)
        return { promise: Promise.resolve({ ok: true, result: { videos: [
          { videoId: firstVideoId, url: 'https://video.example.test/one.mp4', publishTime: 1762500000 },
          { videoId: secondVideoId, url: 'https://video.example.test/two.mp4', publishTime: 1762500000 }
        ] } }) }
      }
      if (script === 'download_queue.py') {
        calls.download++
        calls.tasks = options.stdinJson
        finishDownload()
        return { promise: Promise.resolve({ ok: true, result: { data: { success: 1, failed: 0 } } }) }
      }
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

async function main() {
  loaded.exports.fireCartVideoDownload(6, productId, commodityId)
  loaded.exports.fireCartVideoDownload(6, productId, commodityId)
  await Promise.race([
    downloadStarted,
    new Promise((_, reject) => setTimeout(() => reject(new Error('cart download did not start')), 1000))
  ])
  assert.equal(calls.discover, 1)
  assert.equal(calls.existing, 1)
  assert.equal(calls.download, 1)
  assert.equal(calls.tasks.length, 1)
  assert.equal(calls.tasks[0].account_id, 6)
  assert.equal(calls.tasks[0].product_id, productId)
  assert.equal(calls.tasks[0].verify_video, true)
  assert.ok(calls.tasks[0].dest.includes(`account-6\\`))
  assert.ok(calls.tasks[0].dest.includes(`${productId}_${secondVideoId}_002_`))
  process.stdout.write('Baiying cart auto download isolation: PASS\n')
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`)
  process.exitCode = 1
})
