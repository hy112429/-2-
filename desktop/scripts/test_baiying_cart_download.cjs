const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
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
const replacementVideoId = '7694555774554623283'
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'baiying-cart-'))
const existingFiles = Array.from({ length: 9 }, (_, index) => {
  const filePath = path.join(root, `existing-${index}.mp4`)
  fs.writeFileSync(filePath, `different content ${index}`)
  return { videoId: String(BigInt(firstVideoId) + BigInt(index * 2)), filePath }
})
const calls = { discover: 0, existing: 0, download: 0, tasks: [] }
const state = { status: 'queued', targetCount: 10, uniqueCount: 0 }
let downloadedPath = ''
const downloadedFiles = []
let finishDownload
const downloadStarted = new Promise((resolve) => { finishDownload = resolve })
const stubs = {
  electron: { clipboard: { readText: async () => '' }, ipcMain: { handle() {} } },
  './cart_download_slots': { withCartDownloadSlot: (work) => work() },
  './probe': { onProductCaptured() {} },
  './external_browser': {
    validateManagedProfilePath: () => 'D:\\profiles\\account-6',
    commandManagedBrowser: async () => ({ ok: true })
  },
  './app-dirs': {
    getClientPaths: () => ({ databasePath: path.join(root, 'client.sqlite') }),
    getDownloadDir: () => root
  },
  './engine': {
    async runEngineCommand(script, args) {
      assert.equal(script, 'baiying_cart_download_state.py')
      const command = args[2]
      if (command === 'schedule') { state.targetCount = Number(args[args.indexOf('--target-count') + 1]); return { ok: true, result: { ok: true, job: state } } }
      if (command === 'start') { state.status = 'running'; return { ok: true, result: { ok: true, started: true, job: state } } }
      if (command === 'finish') {
        state.uniqueCount = Number(args[args.indexOf('--unique-count') + 1])
        state.status = state.uniqueCount >= state.targetCount ? 'completed' : 'exhausted'
        return { ok: true, result: { ok: true, job: state } }
      }
      if (command === 'get') return { ok: true, result: { ok: true, job: state } }
      throw new Error(`unexpected state command: ${command}`)
    },
    runScript(script, options) {
      if (script === 'account_manager.py') return { promise: Promise.resolve({ ok: true, result: {
        accounts: [{ id: 6, name: '测试账号', status: 'active' }]
      } }) }
      if (script === 'selection_settings.py') return { promise: Promise.resolve({ ok: true, result: {
        settings: { account: { downloadPath: path.join(root, 'account-6') } }
      } }) }
      if (script === 'baiying_promotion_videos.py' && options.args.includes('--existing-videos')) {
        calls.existing++
        const files = [...existingFiles, ...downloadedFiles]
        return { promise: Promise.resolve({ ok: true, result: { videoIds: files.map(item => item.videoId), files } }) }
      }
      if (script === 'baiying_promotion_videos.py' && options.args.includes('--get-quota')) {
        return { promise: Promise.resolve({ ok: true, result: { quota: 10 } }) }
      }
      if (script === 'baiying_promotion_videos.py') {
        calls.discover++
        assert.equal(options.args[options.args.indexOf('--account-id') + 1], '6')
        assert.equal(options.args[options.args.indexOf('--commodity-id') + 1], commodityId)
        return { promise: Promise.resolve({ ok: true, result: { videos: [
          ...existingFiles.map(item => ({ videoId: item.videoId, url: `https://video.example.test/${item.videoId}.mp4` })),
          { videoId: secondVideoId, url: 'https://video.example.test/duplicate.mp4', publishTime: 1762500000 },
          { videoId: replacementVideoId, url: 'https://video.example.test/replacement.mp4', publishTime: 1762500000 }
        ] } }) }
      }
      if (script === 'download_queue.py') {
        calls.download++
        calls.tasks = options.stdinJson
        downloadedPath = options.stdinJson[0].dest
        fs.mkdirSync(path.dirname(downloadedPath), { recursive: true })
        fs.writeFileSync(downloadedPath, calls.download === 1 ? 'different content 0' : 'replacement content')
        downloadedFiles.push({ videoId: options.stdinJson[0].id.split('-').at(-1), filePath: downloadedPath })
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
  await loaded.exports.waitForCartVideoDownload(6, productId)
  assert.equal(calls.discover, 1)
  assert.ok(calls.existing >= 2)
  assert.equal(calls.download, 2)
  assert.equal(calls.tasks.length, 1)
  assert.equal(calls.tasks[0].account_id, 6)
  assert.equal(calls.tasks[0].product_id, productId)
  assert.equal(calls.tasks[0].verify_video, true)
  assert.ok(calls.tasks[0].dest.includes(`account-6\\`))
  assert.ok(calls.tasks[0].dest.includes(`${productId}_${replacementVideoId}_011_`))
  assert.equal(state.uniqueCount, 10)
  assert.equal(state.status, 'completed')
  fs.rmSync(root, { recursive: true, force: true })
  process.stdout.write('Baiying cart auto download isolation: PASS\n')
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`)
  process.exitCode = 1
})
