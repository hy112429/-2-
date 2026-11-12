import { ipcMain } from 'electron'
import { createHash } from 'node:crypto'
import { createReadStream, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { getClientPaths, getDownloadDir } from './app-dirs'
import { getCartVideoDownloadStatus, getPromotionVideoQuota, retryCartVideoDownload, waitForCartVideoDownload } from './baiying_batch'
import { runEngineCommand, runScript } from './engine'
import { queryPlayableVideos, resolveAuthorizedAweme } from './qianchuan_api'
import {
  enqueuePostPublishAssets,
  getPostPublishEligibleAssets,
  getPostPublishProductProgress,
  getPostPublishQuotaRemaining,
  getPostPublishUploadTargets
} from './qianchuan_upload'

type Json = Record<string, unknown>
type TriggerSource = 'upload-finish' | 'submit-finish' | 'mark-published' | 'renderer-submit-finish' | 'manual_retry'
  | 'reconcile'

const activeQueueIds = new Set<number>()
let reconcileTimer: ReturnType<typeof setInterval> | null = null
let reconcileRunning = false
const PRODUCT_ID = /^\d{1,20}$/

function object(value: unknown): Json {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : {}
}

function safeId(value: unknown, label: string): number {
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || parsed < 1) throw new Error(`${label}无效`)
  return parsed
}

function dateStamp(date = new Date()): string {
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
}

function dayFolder(date = new Date()): string {
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function safePathSegment(value: unknown, fallback: string): string {
  const segment = String(value ?? '').trim().replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/[. ]+$/g, '').slice(0, 80)
  return segment && segment !== '.' && segment !== '..' ? segment : fallback
}

async function stateCommand(...args: string[]): Promise<Json> {
  const outcome = await runEngineCommand('post_publish_material.py', ['--db', getClientPaths().databasePath, ...args])
  if (!outcome.ok) throw new Error(outcome.error ?? '发布后素材状态命令失败')
  const result = object(outcome.result)
  if (result['ok'] === false) throw new Error(String(result['error'] ?? '发布后素材状态命令失败'))
  return result
}

function jsonArg(value: unknown): string {
  return JSON.stringify(value)
}

async function saveStatus(
  queueId: number, status: string, reason: string, options: {
    stage?: string; videoCount?: number; assetIds?: number[]; uploadJobIds?: number[]
  } = {}
): Promise<void> {
  await stateCommand(
    'update', '--publish-queue-id', String(queueId), '--status', status,
    '--reason', reason.slice(0, 1800), '--stage', options.stage ?? status,
    '--video-count', String(options.videoCount ?? 0),
    '--asset-ids-json', jsonArg(options.assetIds ?? []),
    '--upload-job-ids-json', jsonArg(options.uploadJobIds ?? [])
  )
}

async function enabled(): Promise<boolean> {
  const settings = await stateCommand('settings-get')
  return settings['enabled'] === true
}

function rows(value: unknown): Json[] {
  return Array.isArray(value) ? value.map(object) : []
}

async function fileSha256(path: string): Promise<string> {
  return new Promise((resolveHash, rejectHash) => {
    const hash = createHash('sha256')
    const stream = createReadStream(path)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.on('error', rejectHash)
    stream.on('end', () => resolveHash(hash.digest('hex')))
  })
}

export async function runPostPublishQianchuanMaterialForQueue(
  queueId: number, trigger: TriggerSource = 'upload-finish', retry = false
): Promise<Json> {
  const id = safeId(queueId, '发布队列 ID')
  if (activeQueueIds.has(id)) return { ok: true, started: false, reason: '本进程中该发布任务已在编排' }
  activeQueueIds.add(id)
  try {
    const initial = await stateCommand(
      retry ? 'retry' : 'begin', '--publish-queue-id', String(id), '--trigger', trigger
    )
    const run = object(initial['run'])
    if (initial['shouldRun'] !== true) {
      return { ok: true, started: false, run }
    }
    if (initial['alreadyStarted'] === true) return { ok: true, started: false, run }

    const accountId = safeId(run['accountId'], '发布队列账号 ID')
    const productId = String(run['productId'] ?? '').trim()
    const attempt = Math.max(1, Number(run['attemptCount'] ?? 1))
    if (!PRODUCT_ID.test(productId)) {
      await saveStatus(id, 'failed_query', '发布队列商品 ID 格式无效，未调用千川接口。', { stage: 'validate_product_id' })
      return { ok: false, status: 'failed_query', error: '发布队列商品 ID 格式无效' }
    }

    const targetsResult = await getPostPublishUploadTargets()
    const targets = rows(targetsResult['rows']).filter((row) => Number(row['account_id']) === accountId)
    const bindings = targets.filter((row) => Number(row['qianchuan_account_id']) > 0)
    if (targets.length === 0 || bindings.length === 0) {
      const reason = `本地账号 ${accountId} 没有绑定 Step17 千川账户。`
      await saveStatus(id, 'blocked_binding', reason, { stage: 'binding_preflight' })
      return { ok: false, status: 'blocked_binding', error: reason }
    }
    if (bindings.length !== 1 || String(bindings[0]['account_status'] ?? '') !== 'active') {
      const reason = bindings.length !== 1
        ? `本地账号 ${accountId} 绑定了 ${bindings.length} 个千川账户，无法安全选定唯一上传目标。`
        : `本地账号 ${accountId} 未启用，不能进入 Step17 上传队列。`
      await saveStatus(id, 'blocked_binding', reason, { stage: 'binding_preflight' })
      return { ok: false, status: 'blocked_binding', error: reason }
    }
    const binding = bindings[0]
    const localQianchuanId = safeId(binding['qianchuan_account_id'], '本地千川绑定 ID')
    const advertiserId = String(binding['qc_id'] ?? '').trim()
    if (!PRODUCT_ID.test(advertiserId)) {
      const reason = '本地千川绑定缺少有效广告主 ID。'
      await saveStatus(id, 'blocked_binding', reason, { stage: 'binding_preflight' })
      return { ok: false, status: 'blocked_binding', error: reason }
    }

    if (!await enabled()) {
      const reason = '全局自动下视频开关已关闭；未查询、下载或入队。'
      await saveStatus(id, 'disabled', reason, { stage: 'feature_disabled' })
      return { ok: true, status: 'disabled', reason }
    }

    let remaining: number
    let accountVideoLimit: number
    let targetCount: number
    let uploadedUniqueCount: number
    let pendingCount: number
    try {
      const quota = await getPostPublishQuotaRemaining(accountId, productId)
      const progress = await getPostPublishProductProgress(accountId, productId)
      accountVideoLimit = await getPromotionVideoQuota(accountId)
      const limits = object(quota['limits'])
      targetCount = Math.min(accountVideoLimit, Math.max(1, Number(limits['perProductVideos'] ?? 10)))
      uploadedUniqueCount = Math.max(0, Number(progress['uploadedUniqueCount'] ?? 0))
      pendingCount = Math.max(0, Number(progress['pendingCount'] ?? 0))
      remaining = Math.max(0, Math.min(targetCount - uploadedUniqueCount - pendingCount,
        Math.trunc(Number(quota['remaining'] ?? 0))))
    } catch (error) {
      const reason = `无法读取 Step17 当日额度：${error instanceof Error ? error.message : String(error)}`
      await saveStatus(id, 'needs_review', reason, { stage: 'quota_preflight' })
      return { ok: false, status: 'needs_review', error: reason }
    }
    if (uploadedUniqueCount >= targetCount) {
      const reason = `已在千川确认 ${uploadedUniqueCount} 个不同远端素材 ID，达到本商品目标 ${targetCount}。`
      await saveStatus(id, 'completed', reason, { stage: 'remote_unique_complete', videoCount: uploadedUniqueCount })
      return { ok: true, status: 'completed', reason }
    }
    if (remaining < 1) {
      const reason = pendingCount > 0
        ? `千川已确认 ${uploadedUniqueCount}/${targetCount} 个不同素材，另有 ${pendingCount} 条在队列或待核查。`
        : `千川已确认 ${uploadedUniqueCount}/${targetCount} 个不同素材，当前 Step17 额度不足，等待额度恢复。`
      const status = pendingCount > 0 ? 'awaiting_upload' : 'needs_fill'
      await saveStatus(id, status, reason, { stage: 'quota_or_upload_wait', videoCount: uploadedUniqueCount })
      return { ok: true, status, reason }
    }

    if (!await enabled()) {
      const reason = '下载前检测到全局开关已关闭；未发起下载。'
      await saveStatus(id, 'disabled', reason, { stage: 'feature_disabled' })
      return { ok: true, status: 'disabled', reason }
    }

    // 同商品正在下载时先等其完成，再复核素材，避免发布流程同时重复下载。
    await waitForCartVideoDownload(accountId, productId)
    // 加车后可能已从同账号百应下载入库；发布确认后直接复用，避免再取同款视频。
    const findReusableAssets = async (): Promise<number[]> => {
      const existing = await getPostPublishEligibleAssets(accountId)
      const allRows = rows(existing['rows'])
      const candidates = allRows
        .filter((row) => row['eligible'] === true && row['sourceType'] === 'web_download' &&
          Number(row['accountId']) === accountId && String(row['productId'] ?? '') === productId)
      const seenContent = new Set<string>()
      const anchors: Array<{ id: string; path: string }> = []
      for (const row of allRows) {
        if (row['sourceType'] !== 'web_download' || Number(row['accountId']) !== accountId ||
            String(row['productId'] ?? '') !== productId || !row['jobId']) continue
        const filePath = String(row['filePath'] ?? '')
        if (filePath) {
          seenContent.add(await fileSha256(filePath))
          anchors.push({ id: `job:${row['jobId']}`, path: filePath })
        }
      }
      const uniqueAssets: Array<{ id: number; path: string }> = []
      for (const row of candidates) {
        const filePath = String(row['filePath'] ?? '')
        const fingerprint = filePath ? await fileSha256(filePath) : `asset:${row['assetId']}`
        if (seenContent.has(fingerprint)) continue
        seenContent.add(fingerprint)
        uniqueAssets.push({ id: safeId(row['assetId'], '素材 ID'), path: filePath })
      }
      if (!uniqueAssets.length) return []
      const checked = await runScript('video_visual_dedupe.py', {
        internal: true,
        stdinJson: { anchors, candidates: uniqueAssets, limit: remaining },
        onEvent: () => undefined
      }).promise
      if (!checked.ok || !checked.result) throw new Error(checked.error ?? '视频画面去重未完成')
      const result = object(checked.result)
      if (result['ok'] !== true || !Array.isArray(result['acceptedIds'])) {
        throw new Error(String(result['error'] ?? '视频画面去重没有返回有效结果'))
      }
      const candidateIds = new Set(uniqueAssets.map((asset) => asset.id))
      const acceptedIds = result['acceptedIds'].map((value) => Number(value))
      if (acceptedIds.some((value) => !candidateIds.has(value))) {
        throw new Error('视频画面去重返回了不属于当前商品的素材')
      }
      if (Array.isArray(result['duplicates']) && result['duplicates'].length) {
        console.info(`[发布后素材] 账号 ${accountId} 商品 ${productId}：跳过 ${result['duplicates'].length} 条画面高度重复的视频`)
      }
      return acceptedIds
    }
    let reusableAssets: number[]
    try {
      reusableAssets = await findReusableAssets()
    } catch (error) {
      const reason = `无法复核已下载素材：${error instanceof Error ? error.message : String(error)}`
      await saveStatus(id, 'needs_review', reason, { stage: 'existing_asset_check' })
      return { ok: false, status: 'needs_review', error: reason }
    }
    // 加车阶段可能因百应页面短暂未就绪而未取到视频；发布已确认后再按原账号补试。
    let baiyingRecoveryError = ''
    if (reusableAssets.length < remaining) {
      try {
        const downloadState = await getCartVideoDownloadStatus(accountId, productId)
        const retryAt = String(downloadState?.['nextRetryAt'] ?? '')
        if (retryAt && retryAt > new Date().toLocaleString('sv-SE').replace(' ', 'T')) {
          baiyingRecoveryError = `百应下载等待自动重试：${retryAt}`
        } else if (downloadState?.['status'] !== 'exhausted') {
          await retryCartVideoDownload(accountId, productId, remaining - reusableAssets.length)
        }
        reusableAssets = await findReusableAssets()
      } catch (error) {
        baiyingRecoveryError = error instanceof Error ? error.message : String(error)
        console.warn(`[发布后素材] 百应补下载失败：账号 ${accountId}，商品 ${productId}，${baiyingRecoveryError}`)
      }
    }
    if (reusableAssets.length) {
      const enqueued = await enqueuePostPublishAssets(accountId, productId, localQianchuanId, reusableAssets)
      const jobs = rows(enqueued['jobs'])
      const accepted = Math.max(0, Number(enqueued['acceptedCount'] ?? jobs.length))
      const acceptedAssetIds = jobs.map((job) => Number(job['assetId'])).filter((value) => Number.isSafeInteger(value) && value > 0)
      const uploadJobIds = jobs.map((job) => Number(job['id'])).filter((value) => Number.isSafeInteger(value) && value > 0)
      if (accepted < 1) {
        const reason = '加车后已下载素材在 Step17 入队复核时不再符合资格，请先核查素材归属。'
        await saveStatus(id, 'needs_review', reason, { stage: 'existing_asset_enqueue', assetIds: reusableAssets })
        return { ok: false, status: 'needs_review', error: reason }
      }
      const reason = `本次新增 ${accepted} 条同账号同商品素材到 Step17；千川不同素材 ${uploadedUniqueCount}/${targetCount}，上传后继续核对补位。`
      await saveStatus(id, 'awaiting_upload', reason, {
        stage: 'step17_enqueue_existing', videoCount: accepted,
        assetIds: acceptedAssetIds, uploadJobIds
      })
      return { ok: true, status: 'awaiting_upload', acceptedCount: accepted, assetIds: acceptedAssetIds, uploadJobIds, reason }
    }

    const downloadState = await getCartVideoDownloadStatus(accountId, productId)
    if (downloadState) {
      const status = downloadState['status'] === 'exhausted' ? 'candidate_exhausted' : 'needs_fill'
      const reason = status === 'candidate_exhausted'
        ? `百应同款作品候选已耗尽；千川不同素材 ${uploadedUniqueCount}/${targetCount}，另有 ${pendingCount} 条上传待处理或待核查。未使用其他账号或重复素材补数。`
        : `百应同账号视频尚未补齐；千川不同素材 ${uploadedUniqueCount}/${targetCount}。${baiyingRecoveryError}`
      await saveStatus(id, status, reason, { stage: 'baiying_fill_wait', videoCount: uploadedUniqueCount })
      return { ok: true, status, reason }
    }

    let resolvedAweme: { advertiserId: string; awemeId: string; awemeName: string }
    try {
      resolvedAweme = await resolveAuthorizedAweme(advertiserId, String(binding['douyin_id'] ?? ''))
    } catch (error) {
      const reason = `千川授权预检未通过：${error instanceof Error ? error.message : String(error)}` +
        (baiyingRecoveryError ? `；百应同账号补下载失败：${baiyingRecoveryError}` : '')
      await saveStatus(id, 'blocked_auth', reason, { stage: 'authorization_preflight' })
      return { ok: false, status: 'blocked_auth', error: reason }
    }

    let videos
    try {
      videos = await queryPlayableVideos({
        advertiserId: resolvedAweme.advertiserId,
        productId,
        awemeId: resolvedAweme.awemeId,
        maxVideos: remaining,
        maxPages: 100
      })
    } catch (error) {
      const reason = `千川视频查询失败：${error instanceof Error ? error.message : String(error)}`
      const authFailure = /授权|access.?token|token.*过期|无权限|not authorized/i.test(reason)
      await saveStatus(id, authFailure ? 'blocked_auth' : 'failed_query', reason, { stage: 'video_query' })
      return { ok: false, status: authFailure ? 'blocked_auth' : 'failed_query', error: reason }
    }
    if (!videos.length) {
      const reason = '千川接口在已授权抖音号与商品 ID 下没有返回可下载 HTTPS 视频。'
      await saveStatus(id, 'skipped_no_video', reason, { stage: 'video_query', videoCount: 0 })
      return { ok: true, status: 'skipped_no_video', reason }
    }

    if (!await enabled()) {
      const reason = '下载任务创建前检测到全局开关已关闭；未发起下载。'
      await saveStatus(id, 'disabled', reason, { stage: 'feature_disabled', videoCount: videos.length })
      return { ok: true, status: 'disabled', reason }
    }

    const accountName = safePathSegment(binding['account_name'], `账号-${accountId}`)
    const folder = join(getDownloadDir(), accountName, dayFolder(), productId)
    mkdirSync(folder, { recursive: true })
    const timestamp = dateStamp()
    const tasks = videos.map((video, index) => {
      const videoId = safePathSegment(video.videoId || video.awemeItemId, `video-${index + 1}`)
      const serial = String((attempt - 1) * accountVideoLimit + index + 1).padStart(3, '0')
      return {
        id: `postpub-${id}-${attempt}-${index + 1}`,
        url: video.url,
        dest: join(folder, `${productId}_${videoId}_${serial}_${timestamp}.mp4`),
        account_id: accountId,
        product_id: productId,
        verify_video: true
      }
    })
    await saveStatus(id, 'running', `开始下载 ${tasks.length} 条；重试上限为每条 2 次（最多 3 次尝试）。`, {
      stage: 'download_started', videoCount: videos.length
    })
    const downloadHandle = runScript('download_queue.py', {
      internal: true,
      args: ['--db', getClientPaths().databasePath, '--workers', '1', '--max-retries', '2', '--min-delay', '0', '--max-delay', '0'],
      stdinJson: tasks,
      onEvent: () => undefined
    })
    const downloadOutcome = await downloadHandle.promise
    const result = object(downloadOutcome.result)
    const stats = object(result['data'] ?? result)
    const failedCount = Math.max(0, Number(stats['failed'] ?? (downloadOutcome.ok ? 0 : tasks.length)))
    const expectedPaths = new Set(tasks.map((task) => resolve(task.dest).toLowerCase()))

    if (!await enabled()) {
      const reason = '下载已结束，但全局开关在过程中关闭；没有将素材加入 Step17。'
      await saveStatus(id, 'disabled', reason, { stage: 'feature_disabled_after_download', videoCount: videos.length })
      return { ok: true, status: 'disabled', reason }
    }

    let eligibleResult: Json
    try {
      eligibleResult = await getPostPublishEligibleAssets(accountId)
    } catch (error) {
      const reason = `下载完成后无法复核 Step17 eligible：${error instanceof Error ? error.message : String(error)}`
      await saveStatus(id, 'needs_review', reason, { stage: 'eligible_read', videoCount: videos.length })
      return { ok: false, status: 'needs_review', error: reason }
    }
    const eligibleRows = rows(eligibleResult['rows']).filter((row) =>
      row['eligible'] === true && row['sourceType'] === 'web_download' &&
      Number(row['accountId']) === accountId && String(row['productId'] ?? '') === productId &&
      expectedPaths.has(resolve(String(row['filePath'] ?? '')).toLowerCase())
    )
    const assetIds = [...new Set(eligibleRows.map((row) => safeId(row['assetId'], '素材 ID')))]
    if (!assetIds.length) {
      const failed = !downloadOutcome.ok || failedCount > 0
      const reason = failed
        ? `下载器在每条最多 3 次尝试后未产生可入队素材；下载器错误：${downloadOutcome.error ?? '下载失败'}`
        : '文件可能已下载，但素材库未能登记为符合 Step17 资格的 web_download；未入队。'
      await saveStatus(id, failed ? 'failed_download' : 'needs_review', reason, {
        stage: failed ? 'download_failed' : 'asset_registration_check', videoCount: videos.length
      })
      return { ok: false, status: failed ? 'failed_download' : 'needs_review', error: reason }
    }

    if (!await enabled()) {
      const reason = '入队前检测到全局开关已关闭；下载素材已保存在素材库，未加入 Step17。'
      await saveStatus(id, 'disabled', reason, { stage: 'feature_disabled_before_enqueue', videoCount: videos.length, assetIds })
      return { ok: true, status: 'disabled', reason }
    }

    const enqueued = await enqueuePostPublishAssets(accountId, productId, localQianchuanId, assetIds)
    const jobs = rows(enqueued['jobs'])
    const accepted = Math.max(0, Number(enqueued['acceptedCount'] ?? jobs.length))
    const uploadJobIds = jobs.map((job) => Number(job['id'])).filter((value) => Number.isSafeInteger(value) && value > 0)
    if (accepted < 1) {
      const rejected = rows(enqueued['rejected'])
      const reason = `下载素材未能进入 Step17：${String(rejected[0]?.['reason'] ?? '入队前资格复核或额度预占未通过')}`
      await saveStatus(id, 'needs_review', reason, { stage: 'step17_enqueue', videoCount: videos.length, assetIds })
      return { ok: false, status: 'needs_review', error: reason }
    }
    const partial = failedCount > 0 || accepted < assetIds.length
    const reason = partial
      ? `已将 ${accepted} 条合格素材加入 Step17；${failedCount} 条视频下载失败或 ${Math.max(0, assetIds.length - accepted)} 条未能入队。Step17 后续按原配额与登录态处理。`
      : `已将 ${accepted} 条素材加入 Step17；实际上传、登录校验与远端 ID 回读仍由 Step17 原流程处理。`
    await saveStatus(id, 'awaiting_upload', reason, {
      stage: 'step17_enqueue', videoCount: videos.length, assetIds, uploadJobIds
    })
    return { ok: true, status: 'awaiting_upload', acceptedCount: accepted, assetIds, uploadJobIds, reason }
  } catch (error) {
    const reason = `发布后千川素材编排异常：${error instanceof Error ? error.message : String(error)}`
    try {
      const existing = object((await stateCommand('get', '--publish-queue-id', String(id)))['run'])
      if (Object.keys(existing).length && existing['status'] === 'running') {
        await saveStatus(id, 'needs_review', reason, { stage: 'orchestrator_exception' })
      }
    } catch { /* 状态库不可用时保留原异常日志，不影响已发布任务。 */ }
    console.error(`[发布后千川素材] 队列 #${id}：${reason}`)
    return { ok: false, error: reason }
  } finally {
    activeQueueIds.delete(id)
  }
}

/** 发布成功钩子：只排入本机异步微任务，不等待千川 API / 下载 / Step17。 */
export function firePostPublishQianchuanMaterial(queueId: number, trigger: TriggerSource): void {
  setImmediate(() => {
    void runPostPublishQianchuanMaterialForQueue(queueId, trigger).then((result) => {
      console.info(`[发布后千川素材] 队列 #${queueId} 编排已返回：${String(result.status ?? (result.started ? 'running' : 'skipped'))}`)
    }).catch((error) => {
      console.error(`[发布后千川素材] 队列 #${queueId} 异步启动失败：${String(error)}`)
    })
  })
}

async function reconcileTick(): Promise<void> {
  if (reconcileRunning) return
  reconcileRunning = true
  try {
    const due = await stateCommand('due', '--limit', '30')
    for (const row of rows(due['rows'])) {
      const queueId = Number(row['publishQueueId'])
      if (Number.isSafeInteger(queueId) && queueId > 0 && !activeQueueIds.has(queueId)) {
        // 只有状态库已复核为唯一有效绑定的旧阻塞记录，才走显式重试路径。
        // 普通素材补位仍使用可幂等恢复的 begin，避免重复下载或重复入队。
        const retryBinding = row['status'] === 'blocked_binding'
        await runPostPublishQianchuanMaterialForQueue(queueId, 'reconcile', retryBinding)
      }
    }
  } catch (error) {
    console.warn(`[发布后素材] 补位检查失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    reconcileRunning = false
  }
}

export function startPostPublishReconcileScheduler(): void {
  if (reconcileTimer) return
  reconcileTimer = setInterval(() => { void reconcileTick() }, 60_000)
  setTimeout(() => { void reconcileTick() }, 20_000)
}

export function disposePostPublishReconcileScheduler(): void {
  if (reconcileTimer) clearInterval(reconcileTimer)
  reconcileTimer = null
}

export async function markQueuePublishedAndTrigger(queueId: number): Promise<Json> {
  const id = safeId(queueId, '发布队列 ID')
  const outcome = await runEngineCommand('publish_queue.py', [
    '--db', getClientPaths().databasePath, 'mark-published', '--id', String(id)
  ])
  if (!outcome.ok) throw new Error(outcome.error ?? '记录发布成功失败')
  const result = object(outcome.result)
  if (result['ok'] !== true) throw new Error(String(result['error'] ?? '记录发布成功失败'))
  if (result['platformStatus'] === 'published') firePostPublishQianchuanMaterial(id, 'mark-published')
  return result
}

/** renderer 的 submit-finish 也走成功后通知；此入口只接受 published。 */
export function notifyPostPublishFinished(queueId: number, platformStatus: unknown, trigger: TriggerSource): Json {
  const id = safeId(queueId, '发布队列 ID')
  if (platformStatus === 'published') firePostPublishQianchuanMaterial(id, trigger)
  return { ok: true, triggered: platformStatus === 'published' }
}

export function schedulePostPublishRetry(queueId: number): Json {
  const id = safeId(queueId, '发布队列 ID')
  setImmediate(() => {
    void runPostPublishQianchuanMaterialForQueue(id, 'manual_retry', true).then((result) => {
      console.info(`[发布后千川素材] 队列 #${id} 手动重试返回：${String(result.status ?? (result.started ? 'running' : 'skipped'))}`)
    }).catch((error) => {
      console.error(`[发布后千川素材] 队列 #${id} 手动重试失败：${String(error)}`)
    })
  })
  return { ok: true, started: true }
}

export function registerPostPublishQianchuanMaterialIpc(): void {
  ipcMain.handle('publish:markPublished', (_event, queueId: unknown) =>
    markQueuePublishedAndTrigger(safeId(queueId, '发布队列 ID')))
  ipcMain.handle('publish:postPublish:notify', (_event, params: { queueId: unknown; platformStatus: unknown }) =>
    notifyPostPublishFinished(safeId(params?.queueId, '发布队列 ID'), params?.platformStatus, 'renderer-submit-finish'))
  ipcMain.handle('qianchuan:postPublish:settings', () => stateCommand('settings-get'))
  ipcMain.handle('qianchuan:postPublish:setEnabled', async (_event, value: unknown) => {
    if (typeof value !== 'boolean') throw new Error('自动下视频开关值无效')
    return stateCommand('settings-set', '--enabled', value ? '1' : '0')
  })
  ipcMain.handle('qianchuan:postPublish:runs', () => stateCommand('list', '--limit', '30'))
  ipcMain.handle('qianchuan:postPublish:retry', (_event, queueId: unknown) =>
    schedulePostPublishRetry(safeId(queueId, '发布队列 ID')))
}
