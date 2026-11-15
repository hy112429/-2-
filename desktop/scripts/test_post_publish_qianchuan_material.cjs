const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const Module = require('node:module')
const ts = require('typescript')

const sourcePath = path.resolve(__dirname, '../src/main/post_publish_qianchuan_material.ts')
const source = fs.readFileSync(sourcePath, 'utf8')
const compiledSource = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true }
}).outputText

function loadIsolatedScenario(options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'post-publish-qc-'))
  const state = { enabled: true, run: null, updates: [], beginCount: 0 }
  const calls = { auth: 0, query: 0, download: 0, enqueue: 0, markPublished: 0, waitForCart: 0, retryCart: 0, downloadTasks: [], enqueuedAssetIds: [], queriedMaxVideos: 0 }
  const stubs = {
    electron: { ipcMain: { handle() {} } },
    './app-dirs': {
      getClientPaths: () => ({ databasePath: path.join(root, 'isolated.sqlite') }),
      getDownloadDir: () => path.join(root, 'downloads')
    },
    './baiying_batch': {
      async getCartVideoDownloadStatus() { return options.cartDownloadStatus ?? null },
      async waitForCartVideoDownload() { calls.waitForCart += 1 },
      async retryCartVideoDownload() { calls.retryCart += 1; if (options.cartRetryError) throw new Error(options.cartRetryError) },
      async getPromotionVideoQuota() { return options.accountVideoLimit ?? 10 }
    },
    './engine': {
      async runEngineCommand(script, args) {
        if (script === 'publish_queue.py' && args.includes('mark-published')) {
          calls.markPublished += 1
          return { ok: true, result: { ok: true, platformStatus: 'published' } }
        }
        assert.equal(script, 'post_publish_material.py')
        const command = args[2]
        const value = (name) => args[args.indexOf(name) + 1]
        if (command === 'settings-get') return { ok: true, result: { ok: true, enabled: state.enabled } }
        if (command === 'begin' || command === 'retry') {
          state.beginCount += 1
          if (state.run) {
            if (['awaiting_upload', 'needs_fill', 'enqueued'].includes(state.run.status)) {
              state.run = { ...state.run, status: 'running', attemptCount: state.run.attemptCount + 1 }
              return { ok: true, result: { ok: true, shouldRun: true, alreadyStarted: false, run: state.run } }
            }
            return { ok: true, result: { ok: true, shouldRun: false, alreadyStarted: true, run: state.run } }
          }
          state.run = {
            publishQueueId: Number(value('--publish-queue-id')), accountId: 4,
            productId: '3851408796480045486', productName: '隔离商品', attemptCount: 1, status: 'running'
          }
          return { ok: true, result: { ok: true, shouldRun: state.enabled, alreadyStarted: false, run: state.run } }
        }
        if (command === 'update') {
          state.run = { ...state.run, status: value('--status'), reason: value('--reason') }
          state.updates.push(state.run.status)
          return { ok: true, result: { ok: true, run: state.run } }
        }
        if (command === 'get') return { ok: true, result: { ok: true, run: state.run } }
        throw new Error(`unexpected command: ${command}`)
      },
      runScript(script, scriptOptions) {
        if (script === 'video_visual_dedupe.py') {
          const request = scriptOptions.stdinJson
          const acceptedIds = request.candidates.slice(0, request.limit).map((candidate) => candidate.id)
          return { promise: Promise.resolve({ ok: true, result: { ok: true, acceptedIds, duplicates: [] } }) }
        }
        assert.equal(script, 'download_queue.py')
        calls.download += 1
        calls.downloadTasks = scriptOptions.stdinJson
        return { promise: Promise.resolve(options.downloadOutcome ?? { ok: true, result: { data: { success: 1, failed: 0 } } }) }
      }
    },
    './qianchuan_api': {
      async resolveAuthorizedAweme() {
        calls.auth += 1
        if (options.authError) throw new Error('没有 QIANCHUAN 授权')
        return { advertiserId: '987654321', awemeId: '102741886977', awemeName: '隔离抖音号' }
      },
      async queryPlayableVideos(params) {
        calls.query += 1
        calls.queriedMaxVideos = params.maxVideos
        return options.videos ?? []
      }
    },
    './qianchuan_upload': {
      async getPostPublishUploadTargets() {
        return { ok: true, rows: options.targets ?? [{ account_id: 4, account_name: '测试账号', account_status: 'active',
          douyin_id: '102741886977', qianchuan_account_id: 9, qc_id: '987654321' }] }
      },
      async getPostPublishQuotaRemaining() { return { ok: true, remaining: options.remaining ?? 10 } },
      async getPostPublishProductProgress() { return { ok: true, uploadedUniqueCount: options.uploadedUniqueCount ?? 0,
        pendingCount: options.pendingCount ?? 0 } },
      async getPostPublishEligibleAssets() {
        const task = calls.downloadTasks[0]
        const recovered = calls.retryCart && options.eligibleRowsAfterRetry
        return { ok: true, rows: recovered || options.eligibleRows || (task ? [{ assetId: 77, accountId: 4, productId: '3851408796480045486',
          sourceType: options.assetSource ?? 'web_download', eligible: true, filePath: task.dest }] : []) }
      },
      async enqueuePostPublishAssets(_accountId, _productId, _qcId, assetIds) {
        calls.enqueue += 1
        calls.enqueuedAssetIds = assetIds
        return { ok: true, acceptedCount: assetIds.length, jobs: assetIds.map((assetId, index) => ({ id: index + 1, assetId })) }
      }
    }
  }
  const loaded = new Module(sourcePath, module)
  loaded.filename = sourcePath
  loaded.paths = Module._nodeModulePaths(path.dirname(sourcePath))
  const previousLoad = Module._load
  Module._load = function (request, parent, isMain) {
    if (parent && parent.filename === sourcePath && Object.prototype.hasOwnProperty.call(stubs, request)) return stubs[request]
    return previousLoad.call(this, request, parent, isMain)
  }
  try { loaded._compile(compiledSource, sourcePath) }
  finally { Module._load = previousLoad }
  return { api: loaded.exports, state, calls, root }
}

async function main() {
  const noAuth = loadIsolatedScenario({ authError: true })
  const blocked = await noAuth.api.runPostPublishQianchuanMaterialForQueue(41)
  assert.equal(blocked.status, 'blocked_auth')
  assert.equal(noAuth.state.run.status, 'blocked_auth')
  assert.equal(noAuth.calls.query, 0)
  assert.equal(noAuth.calls.download, 0)

  const noBinding = loadIsolatedScenario({ targets: [] })
  const bindingBlocked = await noBinding.api.runPostPublishQianchuanMaterialForQueue(49)
  assert.equal(bindingBlocked.status, 'blocked_binding')
  assert.equal(noBinding.calls.auth, 0)
  assert.equal(noBinding.calls.download, 0)

  const noQuota = loadIsolatedScenario({ remaining: 0 })
  const quotaSkipped = await noQuota.api.runPostPublishQianchuanMaterialForQueue(50)
  assert.equal(quotaSkipped.status, 'needs_fill')
  assert.equal(noQuota.calls.query, 0)
  assert.equal(noQuota.calls.download, 0)

  const noVideo = loadIsolatedScenario()
  const skipped = await noVideo.api.runPostPublishQianchuanMaterialForQueue(42)
  assert.equal(skipped.status, 'skipped_no_video')
  assert.equal(noVideo.state.run.status, 'skipped_no_video')
  assert.equal(noVideo.calls.query, 1)
  assert.equal(noVideo.calls.download, 0)

  const duplicate = await noVideo.api.runPostPublishQianchuanMaterialForQueue(42)
  assert.equal(duplicate.started, false)
  assert.equal(noVideo.calls.query, 1)

  const hook = loadIsolatedScenario()
  hook.api.firePostPublishQianchuanMaterial(43, 'upload-finish')
  await new Promise((resolve) => setTimeout(resolve, 25))
  assert.equal(hook.state.beginCount, 1)
  assert.equal(hook.state.run.status, 'skipped_no_video')

  const reviewOnly = loadIsolatedScenario()
  assert.equal(reviewOnly.api.notifyPostPublishFinished(46, 'submitted_review', 'renderer-submit-finish').triggered, false)
  assert.equal(reviewOnly.api.notifyPostPublishFinished(47, 'failed', 'renderer-submit-finish').triggered, false)
  assert.equal(reviewOnly.state.beginCount, 0)

  const markedPublished = loadIsolatedScenario()
  const marked = await markedPublished.api.markQueuePublishedAndTrigger(48)
  assert.equal(marked.platformStatus, 'published')
  await new Promise((resolve) => setTimeout(resolve, 25))
  assert.equal(markedPublished.calls.markPublished, 1)
  assert.equal(markedPublished.state.beginCount, 1)
  assert.equal(markedPublished.state.run.status, 'skipped_no_video')

  const success = loadIsolatedScenario({ videos: [{ videoId: 'v-1', awemeItemId: 'aw-1', title: '隔离视频',
    url: 'https://video.example.test/isolated.mp4', duration: 10 }] })
  const enqueued = await success.api.runPostPublishQianchuanMaterialForQueue(44)
  assert.equal(enqueued.status, 'awaiting_upload')
  assert.equal(success.calls.downloadTasks[0].account_id, 4)
  assert.equal(success.calls.downloadTasks[0].product_id, '3851408796480045486')
  assert.equal(success.calls.downloadTasks[0].verify_video, true)
  assert.deepEqual(success.calls.enqueuedAssetIds, [77])
  assert.equal(success.calls.queriedMaxVideos, 10)

  const generatedOnly = loadIsolatedScenario({
    videos: [{ videoId: 'v-2', awemeItemId: 'aw-2', title: '隔离图转视频',
      url: 'https://video.example.test/generated.mp4', duration: 10 }],
    assetSource: 'detail_generated'
  })
  const refused = await generatedOnly.api.runPostPublishQianchuanMaterialForQueue(45)
  assert.equal(refused.status, 'needs_review')
  assert.equal(generatedOnly.calls.enqueue, 0)

  const failedDownload = loadIsolatedScenario({
    videos: [{ videoId: 'v-3', awemeItemId: 'aw-3', url: 'https://video.example.test/failed.mp4' }],
    downloadOutcome: { ok: false, error: '模拟下载失败', result: { data: { success: 0, failed: 1 } } },
    eligibleRows: []
  })
  const failed = await failedDownload.api.runPostPublishQianchuanMaterialForQueue(51)
  assert.equal(failed.status, 'failed_download')
  assert.equal(failedDownload.calls.enqueue, 0)

  const reusable = loadIsolatedScenario({
    authError: true,
    eligibleRows: [
      { assetId: 91, accountId: 4, productId: '3851408796480045486', sourceType: 'web_download', eligible: true },
      { assetId: 92, accountId: 5, productId: '3851408796480045486', sourceType: 'web_download', eligible: true },
      { assetId: 93, accountId: 4, productId: '3851408796480045486', sourceType: 'detail_generated', eligible: true }
    ]
  })
  const reused = await reusable.api.runPostPublishQianchuanMaterialForQueue(52)
  assert.equal(reused.status, 'awaiting_upload')
  assert.deepEqual(reusable.calls.enqueuedAssetIds, [91])
  assert.equal(reusable.calls.auth, 0)
  assert.equal(reusable.calls.query, 0)
  assert.equal(reusable.calls.download, 0)
  assert.equal(reusable.calls.waitForCart, 1)
  assert.equal(reusable.calls.retryCart, 1)

  const recovered = loadIsolatedScenario({
    authError: true,
    eligibleRowsAfterRetry: [{ assetId: 102, accountId: 4, productId: '3851408796480045486',
      sourceType: 'web_download', eligible: true }]
  })
  const recoveredRun = await recovered.api.runPostPublishQianchuanMaterialForQueue(53)
  assert.equal(recoveredRun.status, 'awaiting_upload')
  assert.equal(recovered.calls.retryCart, 1)
  assert.equal(recovered.calls.auth, 0)
  assert.deepEqual(recovered.calls.enqueuedAssetIds, [102])

  const tenAssets = Array.from({ length: 10 }, (_, index) => ({
    assetId: 201 + index, accountId: 4, productId: '3851408796480045486',
    sourceType: 'web_download', eligible: true
  }))
  const accountQuota = loadIsolatedScenario({ accountVideoLimit: 10, remaining: 10, eligibleRows: tenAssets })
  const quotaRun = await accountQuota.api.runPostPublishQianchuanMaterialForQueue(54)
  assert.equal(quotaRun.status, 'awaiting_upload')
  assert.equal(quotaRun.acceptedCount, 10)
  assert.deepEqual(accountQuota.calls.enqueuedAssetIds, tenAssets.map((asset) => asset.assetId))
  assert.equal(accountQuota.calls.auth, 0)

  const step17Quota = loadIsolatedScenario({ accountVideoLimit: 10, remaining: 7, eligibleRows: tenAssets })
  const limitedRun = await step17Quota.api.runPostPublishQianchuanMaterialForQueue(55)
  assert.equal(limitedRun.acceptedCount, 7)
  assert.deepEqual(step17Quota.calls.enqueuedAssetIds, tenAssets.slice(0, 7).map((asset) => asset.assetId))

  const beforeRetry = []
  const afterRetry = []
  const duplicateContent = loadIsolatedScenario({ remaining: 3, eligibleRows: beforeRetry, eligibleRowsAfterRetry: afterRetry })
  const files = [0, 1, 2, 3].map((index) => path.join(duplicateContent.root, `video-${index}.mp4`))
  ;['same bytes', 'same bytes', 'other bytes', 'third bytes'].forEach((content, index) => fs.writeFileSync(files[index], content))
  const assetRow = (assetId, filePath) => ({ assetId, filePath, accountId: 4,
    productId: '3851408796480045486', sourceType: 'web_download', eligible: true })
  beforeRetry.push(assetRow(311, files[0]), assetRow(312, files[1]), assetRow(313, files[2]))
  afterRetry.push(...beforeRetry, assetRow(314, files[3]))
  const distinctRun = await duplicateContent.api.runPostPublishQianchuanMaterialForQueue(56)
  assert.equal(distinctRun.acceptedCount, 3)
  assert.equal(duplicateContent.calls.retryCart, 1)
  assert.deepEqual(duplicateContent.calls.enqueuedAssetIds, [311, 313, 314])

  const replacementOptions = { uploadedUniqueCount: 0, pendingCount: 0, eligibleRows: tenAssets }
  const replacement = loadIsolatedScenario(replacementOptions)
  const first = await replacement.api.runPostPublishQianchuanMaterialForQueue(57)
  assert.equal(first.acceptedCount, 10)
  replacementOptions.uploadedUniqueCount = 9
  replacementOptions.eligibleRows = [{ assetId: 401, accountId: 4, productId: '3851408796480045486',
    sourceType: 'web_download', eligible: true }]
  const backfill = await replacement.api.runPostPublishQianchuanMaterialForQueue(57, 'reconcile')
  assert.equal(backfill.acceptedCount, 1)
  assert.deepEqual(replacement.calls.enqueuedAssetIds, [401])
  replacementOptions.uploadedUniqueCount = 10
  replacementOptions.eligibleRows = []
  const finished = await replacement.api.runPostPublishQianchuanMaterialForQueue(57, 'reconcile')
  assert.equal(finished.status, 'completed')
  assert.equal(replacement.calls.enqueue, 2)

  fs.rmSync(noAuth.root, { recursive: true, force: true })
  fs.rmSync(noBinding.root, { recursive: true, force: true })
  fs.rmSync(noQuota.root, { recursive: true, force: true })
  fs.rmSync(noVideo.root, { recursive: true, force: true })
  fs.rmSync(hook.root, { recursive: true, force: true })
  fs.rmSync(reviewOnly.root, { recursive: true, force: true })
  fs.rmSync(markedPublished.root, { recursive: true, force: true })
  fs.rmSync(success.root, { recursive: true, force: true })
  fs.rmSync(generatedOnly.root, { recursive: true, force: true })
  fs.rmSync(failedDownload.root, { recursive: true, force: true })
  fs.rmSync(reusable.root, { recursive: true, force: true })
  fs.rmSync(recovered.root, { recursive: true, force: true })
  fs.rmSync(accountQuota.root, { recursive: true, force: true })
  fs.rmSync(step17Quota.root, { recursive: true, force: true })
  fs.rmSync(duplicateContent.root, { recursive: true, force: true })
  fs.rmSync(replacement.root, { recursive: true, force: true })
  process.stdout.write('post-publish Qianchuan isolation: PASS (blocked_auth, blocked_binding, skipped_quota, skipped_no_video, failed_download, idempotence, published-only hook, mark-published trigger, account/product attribution, detail_generated excluded, reuse-cart-download)\n')
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`)
  process.exitCode = 1
})
