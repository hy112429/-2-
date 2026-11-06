import { ipcMain } from 'electron'
import { mkdirSync } from 'fs'
import { join } from 'path'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'
import { existingManagedBrowserConnection } from './external_browser'
import { releaseQianchuanAccount, reserveQianchuanAccount } from './qianchuan_account_lock'

type Json = Record<string, unknown>

const activeJobs = new Set<number>()
const pendingJobs = new Set<number>()
const activeQianchuanAccounts = new Set<number>()
const preflightQianchuanAccounts = new Set<number>()
const schedulerBlockedQianchuanAccounts = new Set<number>()
let activeCount = 0
let schedulerTimer: ReturnType<typeof setInterval> | null = null
let schedulerRunning = false

function positiveId(value: unknown, label: string): number {
  const id = Number(value)
  if (!Number.isSafeInteger(id) || id < 1) throw new Error(`${label}无效`)
  return id
}

function asObject(value: unknown): Json {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : {}
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

async function queueCommand(command: string, args: string[] = [], allowDeferred = false): Promise<Json> {
  if (command === 'eligible-assets' || command === 'enqueue') {
    // 旧库首次进入千川页时先执行素材库的幂等字段迁移，避免要求先手工打开素材库。
    const migration = await runEngineCommand('asset_library.py', ['--db', getClientPaths().databasePath, 'accounts'])
    if (!migration.ok || asObject(migration.result)['ok'] === false) {
      throw new Error(migration.error ?? '素材库字段迁移失败，请检查本地数据库')
    }
  }
  const outcome = await runEngineCommand('qianchuan_upload.py', [
    '--db', getClientPaths().databasePath,
    command,
    ...args
  ])
  if (!outcome.ok) throw new Error(outcome.error ?? `${command} 执行失败`)
  const result = asObject(outcome.result)
  if (result['ok'] === false && !(allowDeferred && result['deferred'] === true)) {
    throw new Error(stringValue(result['error']) || `${command} 执行失败`)
  }
  return result
}

/** 发布后编排复用 Step17 的资格、额度与入队命令；不直接驱动千川浏览器上传器。 */
export async function getPostPublishUploadTargets(): Promise<Json> {
  return queueCommand('targets')
}

export async function getPostPublishQuotaRemaining(accountId: number, productId: string): Promise<Json> {
  return queueCommand('product-quota-remaining', [
    '--account-id', String(positiveId(accountId, '抖音账号 ID')),
    '--product-id', String(productId)
  ])
}

export async function getPostPublishEligibleAssets(accountId: number): Promise<Json> {
  return queueCommand('eligible-assets', [
    '--account-id', String(positiveId(accountId, '抖音账号 ID')),
    '--limit', '1000'
  ])
}

export async function enqueuePostPublishAssets(
  accountId: number, productId: string, qianchuanAccountId: number, assetIds: number[]
): Promise<Json> {
  // 最后一次资格读取必须紧贴 Step17 enqueue；engine enqueue 仍会在事务内再次验证来源、发布态与配额。
  const eligible = await getPostPublishEligibleAssets(accountId)
  const rows = Array.isArray(eligible['rows']) ? eligible['rows'] as Json[] : []
  const requested = new Set(assetIds.map((value) => positiveId(value, '素材 ID')))
  const allowed = rows.filter((row) => row['eligible'] === true &&
    row['sourceType'] === 'web_download' && Number(row['accountId']) === accountId &&
    String(row['productId'] ?? '') === productId && requested.has(Number(row['assetId'])))
  const allowedIds = [...new Set(allowed.map((row) => positiveId(row['assetId'], '素材 ID')))]
  if (!allowedIds.length) {
    return { ok: true, acceptedCount: 0, rejectedCount: assetIds.length,
      jobs: [], rejected: assetIds.map((assetId) => ({ assetId, reason: '入队前复核时已不满足 Step17 eligible 资格' })) }
  }
  return queueCommand('enqueue', [
    '--account-id', String(positiveId(accountId, '抖音账号 ID')),
    '--asset-ids', allowedIds.join(','),
    '--qianchuan-account-id', String(positiveId(qianchuanAccountId, '千川账户 ID'))
  ])
}

function jobIdFrom(value: unknown): number {
  return positiveId(value, '上传任务 ID')
}

async function readJob(id: number): Promise<Json> {
  const response = await queueCommand('job', ['--id', String(id)])
  const job = asObject(response['job'])
  if (!Object.keys(job).length) throw new Error(`上传任务 ${id} 不存在`)
  return job
}

function numericField(row: Json, camel: string, snake: string): number {
  return positiveId(row[camel] ?? row[snake], camel)
}

async function runOneUploadCore(rawJobId: unknown): Promise<Json> {
  const id = jobIdFrom(rawJobId)
  if (activeJobs.has(id)) return { ok: false, error: '该素材正在上传' }

  const jobBeforeClaim = await readJob(id)
  if (jobBeforeClaim['status'] !== 'queued') return { ok: false, error: `任务当前状态为 ${String(jobBeforeClaim['status'])}，不能重复上传` }
  const nextRetryAt = stringValue(jobBeforeClaim['nextRetryAt'])
  if (nextRetryAt && nextRetryAt > new Date().toLocaleString('sv-SE').replace(' ', 'T')) {
    return { ok: false, status: 'retry_wait', error: `任务等待自动重试，预计 ${nextRetryAt}`, job: jobBeforeClaim }
  }
  const qcId = numericField(jobBeforeClaim, 'qianchuanAccountId', 'qianchuan_account_id')
  if (preflightQianchuanAccounts.has(qcId) || activeQianchuanAccounts.has(qcId)) {
    return { ok: false, error: '该千川账户正在上传或预检查中' }
  }
  if (!reserveQianchuanAccount(qcId)) return { ok: false, error: '该千川账户正在处理素材，请稍后再试' }
  preflightQianchuanAccounts.add(qcId)
  try {
  const accountId = numericField(jobBeforeClaim, 'accountId', 'account_id')
  const config = await queueCommand('quota-config', ['--account-id', String(accountId)])
  const limits = asObject(config['limits'])
  const maxConcurrent = Number(limits['maxConcurrentUploads'] ?? 1)
  if (activeCount >= maxConcurrent || activeQianchuanAccounts.has(qcId)) {
    return { ok: false, error: '当前并发已满或该千川账户正在上传，请稍后再试' }
  }
  const connection = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
  if (!connection) {
    return { ok: false, error: `千川账户 ${qcId} 的专属浏览器未打开或无法连接；请在账号管理打开后再上传` }
  }

  activeJobs.add(id)
  activeQianchuanAccounts.add(qcId)
  preflightQianchuanAccounts.delete(qcId)
  activeCount += 1
  let claimed = false
  try {
    const response = await queueCommand('claim', ['--id', String(id)], true)
    if (response['deferred'] === true) return { ok: false, status: 'deferred', error: response['error'], job: response['job'] }
    const job = asObject(response['job'])
    if (!Object.keys(job).length) throw new Error('任务未能取得上传锁')
    claimed = true
    const filePath = stringValue(job['filePath'] ?? job['file_path'])
    const materialName = stringValue(job['materialName'] ?? job['material_name'])
    const claimedQcId = numericField(job, 'qianchuanAccountId', 'qianchuan_account_id')
    if (claimedQcId !== qcId || !filePath) throw new Error('任务归属或素材路径在领取后发生变化')

    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-upload')
    mkdirSync(diagnosticsDir, { recursive: true })
    const browserOutcome = await runEngineCommand('qianchuan_browser.py', [
      'upload',
      '--cdp-port', String(connection.port),
      '--profile-path', connection.profilePath,
      '--qianchuan-account-id', String(qcId),
      '--asset-path', filePath,
      '--asset-name', materialName,
      '--diagnostics-dir', diagnosticsDir,
      '--min-delay-ms', String(limits['minStepDelayMs'] ?? 800),
      '--max-delay-ms', String(limits['maxStepDelayMs'] ?? 2000)
    ])
    // 子进程异常退出时无法证明提交是否已发生，隔离待核查，避免重复上传。
    if (!browserOutcome.ok) {
      const reason = browserOutcome.error ?? '浏览器上传进程异常结束，提交状态未知'
      await queueCommand('fail', ['--id', String(id), '--reason', reason, '--stage', 'browser_process', '--submit-attempted'])
      return { ok: false, status: 'needs_review', stage: 'browser_process', error: reason }
    }
    const browser = asObject(browserOutcome.result)
    const diagnostic = stringValue(browser['diagnosticPath'])
    if (diagnostic) {
      try {
        await queueCommand('event', ['--id', String(id), '--stage', stringValue(browser['stage']) || 'verification',
          '--outcome', stringValue(browser['status']) || 'unknown', '--reason', `浏览器诊断文件：${diagnostic}`])
      } catch { /* 诊断事件失败不改变上传结果。 */ }
    }
    const remoteId = stringValue(browser['remoteMaterialId'] ?? browser['remote_material_id'])
    if (browser['status'] === 'succeeded' && remoteId) {
      const completed = await queueCommand('complete', ['--id', String(id), '--remote-material-id', remoteId])
      return { ok: true, status: 'succeeded', stage: browser['stage'], job: completed['job'], remoteMaterialId: remoteId }
    }
    const reason = stringValue(browser['reason']) || '千川未返回可确认的素材标识'
    const failArgs = ['--id', String(id), '--reason', reason, '--stage', stringValue(browser['stage']) || 'verification']
    const screenshot = stringValue(browser['screenshotPath'])
    if (screenshot) failArgs.push('--screenshot-path', screenshot)
    if (browser['submitAttempted'] === true || browser['status'] === 'needs_review') failArgs.push('--submit-attempted')
    if (browser['manualActionRequired'] === true) failArgs.push('--manual-action-required')
    const failed = await queueCommand('fail', failArgs)
    return { ok: false, status: asObject(failed['job'])['status'] ?? browser['status'] ?? 'failed', stage: browser['stage'],
      manualActionRequired: browser['manualActionRequired'] === true, error: reason, screenshotPath: screenshot, job: failed['job'] }
  } catch (error) {
    const reason = String(error)
    if (claimed) {
      try {
        // claim 之后的未知异常可能发生在平台提交后，按待核查隔离。
        await queueCommand('fail', ['--id', String(id), '--reason', reason, '--stage', 'orchestration', '--submit-attempted'])
      } catch {
        // 队列中仍是 uploading；下次启动由队列恢复逻辑隔离。
      }
    }
    return { ok: false, status: claimed ? 'needs_review' : 'failed', stage: 'orchestration', error: reason }
  } finally {
    activeJobs.delete(id)
    activeQianchuanAccounts.delete(qcId)
    activeCount -= 1
  }
  } finally {
    preflightQianchuanAccounts.delete(qcId)
    releaseQianchuanAccount(qcId)
  }
}

async function runOneUpload(rawJobId: unknown): Promise<Json> {
  const id = jobIdFrom(rawJobId)
  if (pendingJobs.has(id)) return { ok: false, error: '该上传任务已在执行或预检查中' }
  pendingJobs.add(id)
  try {
    const result = await runOneUploadCore(id)
    if (result['status'] === 'succeeded') {
      const targetId = Number(asObject(result['job'])['qianchuanAccountId'])
      if (Number.isSafeInteger(targetId)) schedulerBlockedQianchuanAccounts.delete(targetId)
    }
    return result
  }
  finally { pendingJobs.delete(id) }
}

function reply<T>(operation: () => Promise<T>): Promise<T | Json> {
  return operation().catch((error) => ({ ok: false, error: String(error) }))
}

/** Step 17：只暴露受约束的业务操作，渲染进程不能直接改上传终态。 */
export function registerQianchuanUploadIpc(): void {
  ipcMain.handle('qianchuan:upload:targets', () => reply(() => queueCommand('targets')))
  ipcMain.handle('qianchuan:upload:eligibleAssets', (_event, accountId: unknown) => reply(() =>
    queueCommand('eligible-assets', ['--account-id', String(positiveId(accountId, '抖音账号 ID')), '--limit', '500'])
  ))
  ipcMain.handle('qianchuan:upload:jobs', (_event, accountId?: unknown) => reply(() =>
    queueCommand('jobs', accountId == null ? [] : ['--account-id', String(positiveId(accountId, '抖音账号 ID'))])
  ))
  ipcMain.handle('qianchuan:upload:enqueue', (_event, params: { accountId: unknown; assetIds: unknown; qianchuanAccountId: unknown }) => reply(() => {
    const accountId = positiveId(params?.accountId, '抖音账号 ID')
    const qcId = positiveId(params?.qianchuanAccountId, '千川账户 ID')
    const rawAssetIds = Array.isArray(params?.assetIds) ? params.assetIds : []
    const assetIds = [...new Set(rawAssetIds.map((value) => positiveId(value, '素材 ID')))]
    if (!assetIds.length || assetIds.length > 50) throw new Error('一次请选择 1–50 条素材')
    return queueCommand('enqueue', [
      '--account-id', String(accountId),
      '--asset-ids', assetIds.join(','),
      '--qianchuan-account-id', String(qcId)
    ])
  }))
  ipcMain.handle('qianchuan:upload:run', (_event, id: unknown) => reply(() => runOneUpload(id)))
  ipcMain.handle('qianchuan:upload:retrySafe', (_event, id: unknown) => reply(() =>
    queueCommand('retry-safe', ['--id', String(jobIdFrom(id))])
  ))
  ipcMain.handle('qianchuan:upload:reviewAbsent', (_event, params: { id: unknown; note: unknown }) => reply(() => {
    const note = stringValue(params?.note).trim()
    if (note.length < 6) throw new Error('请写明在千川核对无此素材的依据（至少 6 字）')
    return queueCommand('resolve-review-absent', ['--id', String(jobIdFrom(params?.id)), '--note', note])
  }))
  ipcMain.handle('qianchuan:upload:reviewPresent', (_event, params: { id: unknown; remoteMaterialId: unknown }) => reply(async () => {
    const id = jobIdFrom(params?.id)
    const remoteId = stringValue(params?.remoteMaterialId).trim()
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(remoteId)) throw new Error('请填写在千川核实的素材 ID（8–64 位）')
    const job = await readJob(id)
    if (job['status'] !== 'needs_review') throw new Error('仅待核查任务可人工补录平台素材 ID')
    await queueCommand('event', ['--id', String(id), '--stage', 'manual_review', '--outcome', 'remote_present',
      '--reason', `人工在千川确认素材 ID：${remoteId}`])
    return queueCommand('complete', ['--id', String(id), '--remote-material-id', remoteId])
  }))
  ipcMain.handle('qianchuan:upload:quotaConfig', (_event, accountId?: unknown) => reply(() =>
    queueCommand('quota-config', accountId == null ? [] : ['--account-id', String(positiveId(accountId, '抖音账号 ID'))])
  ))
  ipcMain.handle('qianchuan:upload:retryConfig', () => reply(() => queueCommand('retry-config')))
  ipcMain.handle('qianchuan:upload:setRetryConfig', (_event, enabled: unknown) => reply(() => {
    if (typeof enabled !== 'boolean') throw new Error('自动重试开关无效')
    return queueCommand('retry-config', ['--enabled', enabled ? '1' : '0'])
  }))
  ipcMain.handle('qianchuan:upload:setQuotaConfig', (_event, params: {
    accountId: unknown; perProductVideos: unknown; perAccountProducts: unknown; perAccountVideos: unknown;
    perGroupProducts: unknown; perGroupVideos: unknown; maxConcurrentUploads: unknown;
    minStepDelayMs: unknown; maxStepDelayMs: unknown
  }) => reply(() => {
    const values = [params?.perProductVideos, params?.perAccountProducts, params?.perAccountVideos,
      params?.perGroupProducts, params?.perGroupVideos, params?.maxConcurrentUploads].map(Number)
    if (values.some((value) => !Number.isSafeInteger(value) || value < 1 || value > 10000)) throw new Error('额度必须为 1–10000 的整数')
    const minDelay = Number(params?.minStepDelayMs)
    const maxDelay = Number(params?.maxStepDelayMs)
    if (!Number.isSafeInteger(minDelay) || !Number.isSafeInteger(maxDelay) || minDelay < 1 || maxDelay < minDelay || maxDelay > 60000) {
      throw new Error('步骤延时须为 1–60000 毫秒，最大值不得小于最小值')
    }
    return queueCommand('quota-config', [
      '--account-id', String(positiveId(params?.accountId, '抖音账号 ID')),
      '--per-product-videos', String(values[0]),
      '--per-account-products', String(values[1]),
      '--per-account-videos', String(values[2]),
      '--per-group-products', String(values[3]),
      '--per-group-videos', String(values[4]),
      '--max-concurrent-uploads', String(values[5]),
      '--min-step-delay-ms', String(minDelay),
      '--max-step-delay-ms', String(maxDelay)
    ])
  }))
}

/** 到期任务在客户端运行期间自动续；异常账号本次运行暂停，避免同会话批量失败。 */
async function schedulerTick(): Promise<void> {
  if (schedulerRunning || activeCount > 0) return
  schedulerRunning = true
  try {
    await queueCommand('recover-interrupted', ['--older-than-minutes', '30'])
    const result = await queueCommand('jobs', ['--status', 'queued', '--limit', '500'])
    const rows = Array.isArray(result['rows']) ? result['rows'] as Json[] : []
    const today = new Date().toLocaleDateString('sv-SE')
    for (const job of rows.reverse()) {
      if (stringValue(job['scheduledDate']) > today) continue
      const retryAt = stringValue(job['nextRetryAt'])
      if (retryAt && retryAt > new Date().toLocaleString('sv-SE').replace(' ', 'T')) continue
      const qcId = numericField(job, 'qianchuanAccountId', 'qianchuan_account_id')
      if (schedulerBlockedQianchuanAccounts.has(qcId)) continue
      const connection = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
      if (!connection) continue
      const outcome = await runOneUpload(job['id'])
      if (outcome['ok'] === false && outcome['status'] !== 'deferred') {
        const stage = stringValue(outcome['stage'])
        const error = stringValue(outcome['error'])
        if (outcome['status'] === 'needs_review' || outcome['manualActionRequired'] === true ||
            /登录|验证码|无权限/.test(error) || /login|captcha/.test(stage)) {
          schedulerBlockedQianchuanAccounts.add(qcId)
        }
      }
    }
  } catch {
    // 启动早于数据库初始化、或脚本暂不可用时，下一轮继续；不伪造成功。
  } finally {
    schedulerRunning = false
  }
}

export function startQianchuanUploadScheduler(): void {
  if (schedulerTimer) return
  schedulerTimer = setInterval(() => { void schedulerTick() }, 60_000)
  setTimeout(() => { void schedulerTick() }, 15_000)
}

export function disposeQianchuanUploadScheduler(): void {
  if (schedulerTimer) clearInterval(schedulerTimer)
  schedulerTimer = null
}
