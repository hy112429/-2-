import { ipcMain } from 'electron'
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { getClientPaths, getDownloadDir } from './app-dirs'
import { waitForCartVideoDownload } from './baiying_batch'
import { runEngineCommand, runScript } from './engine'
import { queryPlayableVideos, resolveAuthorizedAweme } from './qianchuan_api'
import {
  enqueuePostPublishAssets,
  getPostPublishEligibleAssets,
  getPostPublishQuotaRemaining,
  getPostPublishUploadTargets
} from './qianchuan_upload'

type Json = Record<string, unknown>
type TriggerSource = 'upload-finish' | 'submit-finish' | 'mark-published' | 'renderer-submit-finish' | 'manual_retry'

const activeQueueIds = new Set<number>()
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
    try {
      const quota = await getPostPublishQuotaRemaining(accountId, productId)
      remaining = Math.max(0, Math.min(3, Math.trunc(Number(quota['remaining'] ?? 0))))
    } catch (error) {
      const reason = `无法读取 Step17 当日额度：${error instanceof Error ? error.message : String(error)}`
      await saveStatus(id, 'needs_review', reason, { stage: 'quota_preflight' })
      return { ok: false, status: 'needs_review', error: reason }
    }
    if (remaining < 1) {
      const reason = '当前 Step17 每品/账号/分组当日剩余额度为 0；未下载或入队。'
      await saveStatus(id, 'skipped_quota', reason, { stage: 'quota_preflight' })
      return { ok: true, status: 'skipped_quota', reason }
    }

    if (!await enabled()) {
      const reason = '下载前检测到全局开关已关闭；未发起下载。'
      await saveStatus(id, 'disabled', reason, { stage: 'feature_disabled' })
      return { ok: true, status: 'disabled', reason }
    }

    // 同商品正在下载时先等其完成，再复核素材，避免发布流程同时重复下载。
    await waitForCartVideoDownload(accountId, productId)
    // 加车后可能已从同账号百应下载入库；发布确认后直接复用，避免再取同款视频。
    let reusableAssets: number[]
    try {
      const existing = await getPostPublishEligibleAssets(accountId)
      reusableAssets = rows(existing['rows'])
        .filter((row) => row['eligible'] === true && row['sourceType'] === 'web_download' &&
          Number(row['accountId']) === accountId && String(row['productId'] ?? '') === productId)
        .slice(0, remaining)
        .map((row) => safeId(row['assetId'], '素材 ID'))
    } catch (error) {
      const reason = `无法复核已下载素材：${error instanceof Error ? error.message : String(error)}`
      await saveStatus(id, 'needs_review', reason, { stage: 'existing_asset_check' })
      return { ok: false, status: 'needs_review', error: reason }
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
      const reason = `复用加车后已下载的 ${accepted} 条同账号同商品素材，已加入 Step17；未重复下载。`
      await saveStatus(id, 'enqueued', reason, {
        stage: 'step17_enqueue_existing', videoCount: accepted,
        assetIds: acceptedAssetIds, uploadJobIds
      })
      return { ok: true, status: 'enqueued', acceptedCount: accepted, assetIds: acceptedAssetIds, uploadJobIds, reason }
    }

    let resolvedAweme: { advertiserId: string; awemeId: string; awemeName: string }
    try {
      resolvedAweme = await resolveAuthorizedAweme(advertiserId, String(binding['douyin_id'] ?? ''))
    } catch (error) {
      const reason = `千川授权预检未通过：${error instanceof Error ? error.message : String(error)}`
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
      const serial = String((attempt - 1) * 3 + index + 1).padStart(3, '0')
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
    await saveStatus(id, 'enqueued', reason, {
      stage: 'step17_enqueue', videoCount: videos.length, assetIds, uploadJobIds
    })
    return { ok: true, status: 'enqueued', acceptedCount: accepted, assetIds, uploadJobIds, reason }
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
