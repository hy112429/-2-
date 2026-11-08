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
let statusTimer: ReturnType<typeof setInterval> | null = null
let statusRunning = false
const statusRetryAt = new Map<number, number>()

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
  if (result['ok'] === false && !(allowDeferred && (result['deferred'] === true || result['rejected'] === true))) {
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

export async function getPostPublishProductProgress(accountId: number, productId: string): Promise<Json> {
  return queueCommand('product-progress', [
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
  const queued = await queueCommand('enqueue', [
    '--account-id', String(positiveId(accountId, '抖音账号 ID')),
    '--asset-ids', allowedIds.join(','),
    '--qianchuan-account-id', String(positiveId(qianchuanAccountId, '千川账户 ID'))
  ])
  void schedulerTick()
  return queued
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

/** 人工核清该账户全部待核查任务后，恢复本进程的自动上传。 */
async function unblockAfterManualReview(qianchuanAccountId: number): Promise<void> {
  try {
    const result = await queueCommand('jobs', ['--status', 'needs_review', '--limit', '500'])
    const rows = Array.isArray(result['rows']) ? result['rows'] as Json[] : []
    if (!rows.some((row) => Number(row['qianchuanAccountId'] ?? row['qianchuan_account_id']) === qianchuanAccountId)) {
      schedulerBlockedQianchuanAccounts.delete(qianchuanAccountId)
      void schedulerTick()
    }
  } catch (error) {
    // 人工核查已经完成；后台恢复检查失败不应把已成功的操作回报为失败。
    console.warn(`千川账户 ${qianchuanAccountId} 恢复自动上传检查失败：${error instanceof Error ? error.message : String(error)}`)
  }
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
  const targets = await queueCommand('targets')
  const targetRows = Array.isArray(targets['rows']) ? targets['rows'] as Json[] : []
  const binding = targetRows.find((row) => Number(row['account_id']) === accountId &&
    Number(row['qianchuan_account_id']) === qcId)
  const advertiserId = stringValue(binding?.['qc_id'])
  const dedicatedConnection = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
  // 同一抖音账号的专属浏览器若已登录目标千川，可复用其登录态；浏览器执行器还会
  // 在选文件前核对页面显示的广告主 ID，绝不借用其他账号的登录态。
  const accountConnection = !dedicatedConnection && /^\d{8,20}$/.test(advertiserId)
    ? await existingManagedBrowserConnection(`account-${accountId}`) : null
  const connection = dedicatedConnection ?? accountConnection
  if (!connection) {
    return { ok: false, error: `千川账户 ${qcId} 的专属浏览器未打开，且账号 ${accountId} 浏览器没有可复用的目标千川登录态` }
  }

  activeJobs.add(id)
  activeQianchuanAccounts.add(qcId)
  preflightQianchuanAccounts.delete(qcId)
  activeCount += 1
  let claimed = false
  try {
    const response = await queueCommand('claim', ['--id', String(id)], true)
    if (response['deferred'] === true) return { ok: false, status: 'deferred', error: response['error'], job: response['job'] }
    if (response['rejected'] === true) return { ok: false, status: 'rejected_quality', error: response['error'], job: response['job'] }
    const job = asObject(response['job'])
    if (!Object.keys(job).length) throw new Error('任务未能取得上传锁')
    claimed = true
    const filePath = stringValue(job['filePath'] ?? job['file_path'])
    const materialName = stringValue(job['materialName'] ?? job['material_name'])
    const claimedQcId = numericField(job, 'qianchuanAccountId', 'qianchuan_account_id')
    if (claimedQcId !== qcId || !filePath) throw new Error('任务归属或素材路径在领取后发生变化')

    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-upload')
    mkdirSync(diagnosticsDir, { recursive: true })
    const browserArgs = [
      'upload',
      '--cdp-port', String(connection.port),
      '--profile-path', connection.profilePath,
      '--qianchuan-account-id', String(qcId),
      '--asset-path', filePath,
      '--asset-name', materialName,
      '--diagnostics-dir', diagnosticsDir,
      '--min-delay-ms', String(limits['minStepDelayMs'] ?? 800),
      '--max-delay-ms', String(limits['maxStepDelayMs'] ?? 2000)
    ]
    if (/^\d{8,20}$/.test(advertiserId)) browserArgs.push('--advertiser-id', advertiserId)
    if (accountConnection) browserArgs.push('--douyin-account-id', String(accountId))
    const browserOutcome = await runEngineCommand('qianchuan_browser.py', browserArgs)
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

async function runBatchUpload(jobs: Json[]): Promise<Json> {
  const ids = jobs.map((job) => jobIdFrom(job['id']))
  if (ids.length < 2 || ids.length > 10 || ids.some((id) => pendingJobs.has(id) || activeJobs.has(id))) {
    return { ok: false, status: 'preflight_failed', error: '批量任务数量或状态无效' }
  }
  const first = jobs[0]
  const qcId = numericField(first, 'qianchuanAccountId', 'qianchuan_account_id')
  const accountId = numericField(first, 'accountId', 'account_id')
  const productId = stringValue(first['productId'])
  if (jobs.some((job) => numericField(job, 'qianchuanAccountId', 'qianchuan_account_id') !== qcId ||
      numericField(job, 'accountId', 'account_id') !== accountId || stringValue(job['productId']) !== productId)) {
    return { ok: false, status: 'preflight_failed', error: '批量素材必须属于同一账号和商品' }
  }
  if (preflightQianchuanAccounts.has(qcId) || activeQianchuanAccounts.has(qcId) ||
      !reserveQianchuanAccount(qcId)) {
    return { ok: false, status: 'preflight_failed', error: '该千川账号正在处理其他任务' }
  }
  ids.forEach((id) => pendingJobs.add(id))
  preflightQianchuanAccounts.add(qcId)
  let claimed = false
  try {
    const config = await queueCommand('quota-config', ['--account-id', String(accountId)])
    const limits = asObject(config['limits'])
    if (activeCount >= Number(limits['maxConcurrentUploads'] ?? 1)) {
      return { ok: false, status: 'preflight_failed', error: '当前上传并发已满' }
    }
    const targets = await queueCommand('targets')
    const rows = Array.isArray(targets['rows']) ? targets['rows'] as Json[] : []
    const binding = rows.find((row) => Number(row['account_id']) === accountId &&
      Number(row['qianchuan_account_id']) === qcId)
    const advertiserId = stringValue(binding?.['qc_id'])
    const dedicated = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
    const accountBrowser = !dedicated && /^\d{8,20}$/.test(advertiserId)
      ? await existingManagedBrowserConnection(`account-${accountId}`) : null
    const connection = dedicated ?? accountBrowser
    if (!connection) return { ok: false, status: 'preflight_failed', error: '目标千川账号的专属浏览器未打开' }

    activeQianchuanAccounts.add(qcId)
    preflightQianchuanAccounts.delete(qcId)
    ids.forEach((id) => activeJobs.add(id))
    activeCount += 1
    const response = await queueCommand('claim-batch', ['--ids', ids.join(',')], true)
    if (response['rejected'] === true) {
      return { ok: false, status: 'preflight_failed', error: response['error'] }
    }
    claimed = true
    const claimedJobs = Array.isArray(response['jobs']) ? response['jobs'] as Json[] : []
    if (claimedJobs.length !== ids.length) throw new Error('批量领取结果与请求数量不一致')
    const assets = claimedJobs.map((job) => ({
      filePath: stringValue(job['filePath']), materialName: stringValue(job['materialName'])
    }))
    if (assets.some((asset) => !asset.filePath || !asset.materialName)) {
      throw new Error('批量任务缺少本地文件路径或素材名')
    }
    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-upload')
    mkdirSync(diagnosticsDir, { recursive: true })
    const browserArgs = [
      'batch-upload', '--cdp-port', String(connection.port), '--profile-path', connection.profilePath,
      '--qianchuan-account-id', String(qcId), '--batch-assets-json', JSON.stringify(assets),
      '--diagnostics-dir', diagnosticsDir,
      '--min-delay-ms', String(limits['minStepDelayMs'] ?? 800),
      '--max-delay-ms', String(limits['maxStepDelayMs'] ?? 2000)
    ]
    if (/^\d{8,20}$/.test(advertiserId)) browserArgs.push('--advertiser-id', advertiserId)
    if (accountBrowser) browserArgs.push('--douyin-account-id', String(accountId))
    const browserOutcome = await runEngineCommand('qianchuan_browser.py', browserArgs)
    const browser = asObject(browserOutcome.result)
    const browserItems = Array.isArray(browser['items']) ? browser['items'] as Json[] : []
    const manualActionRequired = browserItems.some((item) => item['manualActionRequired'] === true)
    const byName = new Map(browserItems.map((item) => [stringValue(item['assetName']), item]))
    // 浏览器在文件选择前失败时，队列仍可安全回排并走原单条路径；一旦
    // 有文件已选择、提交已开始或回执缺失，则绝不回退，交由待核查流程处理。
    const noTouchFailedIds: number[] = []
    const confirmedItems: Array<{ id: number; remoteId: string }> = []
    let hasUncertainItem = false
    for (const job of claimedJobs) {
      const item = byName.get(stringValue(job['materialName']))
      const remoteId = stringValue(item?.['remoteMaterialId'])
      if (browserOutcome.ok && item?.['status'] === 'succeeded' && remoteId) {
        confirmedItems.push({ id: jobIdFrom(job['id']), remoteId })
      } else if (browserOutcome.ok && item?.['status'] === 'failed' &&
                 item['submitAttempted'] !== true && !remoteId) {
        noTouchFailedIds.push(jobIdFrom(job['id']))
      } else {
        hasUncertainItem = true
      }
    }
    if (browserOutcome.ok && noTouchFailedIds.length > 0 && !hasUncertainItem) {
      for (const item of confirmedItems) {
        await queueCommand('complete', ['--id', String(item.id), '--remote-material-id', item.remoteId])
      }
      await queueCommand('requeue-preflight-batch', ['--ids', noTouchFailedIds.join(',')])
      return { ok: false, status: 'preflight_failed', error: '批量尚未选择文件，已安全回退单条上传路径' }
    }
    const results: Json[] = []
    for (const job of claimedJobs) {
      const id = jobIdFrom(job['id'])
      const item = byName.get(stringValue(job['materialName']))
      const remoteId = stringValue(item?.['remoteMaterialId'])
      if (browserOutcome.ok && item?.['status'] === 'succeeded' && remoteId) {
        const completed = await queueCommand('complete', ['--id', String(id), '--remote-material-id', remoteId])
        results.push({ id, status: 'succeeded', job: completed['job'], remoteMaterialId: remoteId })
        continue
      }
      const reason = browserOutcome.ok
        ? stringValue(item?.['reason']) || '批量提交后未确认该素材的远端 ID'
        : browserOutcome.error ?? '批量浏览器进程异常，远端提交状态未知'
      const stage = browserOutcome.ok ? stringValue(item?.['stage']) || 'verification' : 'browser_process'
      const failArgs = ['--id', String(id), '--reason', reason, '--stage', stage]
      const screenshot = stringValue(item?.['screenshotPath'])
      if (screenshot) failArgs.push('--screenshot-path', screenshot)
      if (!browserOutcome.ok || !item || item['submitAttempted'] === true || item['status'] === 'needs_review') {
        failArgs.push('--submit-attempted')
      }
      const failed = await queueCommand('fail', failArgs)
      results.push({ id, status: asObject(failed['job'])['status'], reason })
    }
    const needsReview = results.some((row) => row['status'] === 'needs_review')
    return { ok: !needsReview && results.every((row) => row['status'] === 'succeeded'),
      status: needsReview ? 'needs_review' : results.every((row) => row['status'] === 'succeeded') ? 'succeeded' : 'failed',
      count: results.filter((row) => row['status'] === 'succeeded').length,
      manualActionRequired, jobs: results }
  } catch (error) {
    if (claimed) {
      for (const id of ids) {
        try {
          const current = await readJob(id)
          if (current['status'] === 'uploading') {
            await queueCommand('fail', ['--id', String(id), '--reason', String(error),
              '--stage', 'batch_orchestration', '--submit-attempted'])
          }
        } catch { /* 下次启动时 recover-interrupted 会隔离仍在上传中的任务。 */ }
      }
    }
    return { ok: false, status: claimed ? 'needs_review' : 'preflight_failed', error: String(error) }
  } finally {
    ids.forEach((id) => { activeJobs.delete(id); pendingJobs.delete(id) })
    if (activeQianchuanAccounts.delete(qcId)) activeCount -= 1
    preflightQianchuanAccounts.delete(qcId)
    releaseQianchuanAccount(qcId)
  }
}

function reply<T>(operation: () => Promise<T>): Promise<T | Json> {
  return operation().catch((error) => ({ ok: false, error: String(error) }))
}

async function refreshMaterialStatusCore(rawJobId: unknown): Promise<Json> {
    const job = await readJob(jobIdFrom(rawJobId))
    if (job['status'] !== 'succeeded' || !/^\d{8,20}$/.test(stringValue(job['remoteMaterialId']))) {
      throw new Error('仅已上传且有千川素材 ID 的任务可以查询平台判定')
    }
    const qcId = numericField(job, 'qianchuanAccountId', 'qianchuan_account_id')
    const accountId = numericField(job, 'accountId', 'account_id')
    if (activeQianchuanAccounts.has(qcId) || !reserveQianchuanAccount(qcId)) {
      throw new Error('该千川账户正在上传或读取素材，请稍后再查')
    }
    try {
      const targets = await queueCommand('targets')
      const targetRows = Array.isArray(targets['rows']) ? targets['rows'] as Json[] : []
      const binding = targetRows.find((row) => Number(row['account_id']) === accountId &&
        Number(row['qianchuan_account_id']) === qcId)
      const advertiserId = stringValue(binding?.['qc_id'])
      if (!/^\d{8,20}$/.test(advertiserId)) throw new Error('该账号没有已核实的千川广告主 ID')
      const dedicated = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
      const accountBrowser = dedicated ? null : await existingManagedBrowserConnection(`account-${accountId}`)
      const connection = dedicated ?? accountBrowser
      if (!connection) throw new Error('目标千川账户的专属浏览器未打开')
      const args = [
        '--db', getClientPaths().databasePath, '--job-id', String(job['id']),
        '--profile-path', connection.profilePath, '--cdp-port', String(connection.port),
        '--advertiser-id', advertiserId
      ]
      if (accountBrowser) args.push('--douyin-account-id', String(accountId))
      const outcome = await runEngineCommand('qianchuan_material_status.py', args)
      if (!outcome.ok) throw new Error(outcome.error ?? '千川素材判定读取失败')
      const result = asObject(outcome.result)
      if (result['ok'] === false) throw new Error(stringValue(result['error']) || '千川素材判定读取失败')
      return result
    } finally {
      releaseQianchuanAccount(qcId)
    }
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
  ipcMain.handle('qianchuan:upload:materialRecords', (_event, rawParams: unknown) => reply(async () => {
    const params = asObject(rawParams)
    const accountId = positiveId(params['accountId'], '抖音账号 ID')
    const productId = stringValue(params['productId']).trim()
    if (!/^\d{1,30}$/.test(productId)) throw new Error('商品 ID 无效')
    const result = await queueCommand('jobs', [
      '--status', 'succeeded', '--account-id', String(accountId), '--product-id', productId, '--limit', '1000'
    ])
    const jobs = Array.isArray(result['rows']) ? result['rows'] as Json[] : []
    const seen = new Set<string>()
    return { ok: true, rows: jobs.filter((job) => {
      const remoteId = stringValue(job['remoteMaterialId'])
      const key = `${job['qianchuanAccountId']}:${remoteId}`
      if (!/^\d{8,20}$/.test(remoteId) || seen.has(key)) return false
      seen.add(key)
      return true
    }).map((job) => ({
      id: job['id'], accountId: job['accountId'], qianchuanAccountId: job['qianchuanAccountId'],
      productId: job['productId'], materialName: job['materialName'], remoteMaterialId: job['remoteMaterialId'],
      reviewStatus: job['reviewStatus'], materialEvaluation: job['materialEvaluation'],
      evaluationCheckedAt: job['evaluationCheckedAt'], completedAt: job['updatedAt']
    })) }
  }))
  ipcMain.handle('qianchuan:upload:refreshMaterialStatus', (_event, rawJobId: unknown) =>
    reply(() => refreshMaterialStatusCore(rawJobId)))
  ipcMain.handle('qianchuan:upload:enqueue', (_event, params: { accountId: unknown; assetIds: unknown; qianchuanAccountId: unknown }) => reply(async () => {
    const accountId = positiveId(params?.accountId, '抖音账号 ID')
    const qcId = positiveId(params?.qianchuanAccountId, '千川账户 ID')
    const rawAssetIds = Array.isArray(params?.assetIds) ? params.assetIds : []
    const assetIds = [...new Set(rawAssetIds.map((value) => positiveId(value, '素材 ID')))]
    if (!assetIds.length || assetIds.length > 50) throw new Error('一次请选择 1–50 条素材')
    const queued = await queueCommand('enqueue', [
      '--account-id', String(accountId),
      '--asset-ids', assetIds.join(','),
      '--qianchuan-account-id', String(qcId)
    ])
    void schedulerTick()
    return queued
  }))
  ipcMain.handle('qianchuan:upload:run', (_event, id: unknown) => reply(() => runOneUpload(id)))
  ipcMain.handle('qianchuan:upload:retrySafe', (_event, id: unknown) => reply(() =>
    queueCommand('retry-safe', ['--id', String(jobIdFrom(id))])
  ))
  ipcMain.handle('qianchuan:upload:reviewAbsent', (_event, params: { id: unknown; note: unknown }) => reply(async () => {
    const note = stringValue(params?.note).trim()
    if (note.length < 6) throw new Error('请写明在千川核对无此素材的依据（至少 6 字）')
    const id = jobIdFrom(params?.id)
    const job = await readJob(id)
    const resolved = await queueCommand('resolve-review-absent', ['--id', String(id), '--note', note])
    await unblockAfterManualReview(numericField(job, 'qianchuanAccountId', 'qianchuan_account_id'))
    return resolved
  }))
  ipcMain.handle('qianchuan:upload:reviewPresent', (_event, params: { id: unknown; remoteMaterialId: unknown }) => reply(async () => {
    const id = jobIdFrom(params?.id)
    const remoteId = stringValue(params?.remoteMaterialId).trim()
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(remoteId)) throw new Error('请填写在千川核实的素材 ID（8–64 位）')
    const job = await readJob(id)
    if (job['status'] !== 'needs_review') throw new Error('仅待核查任务可人工补录平台素材 ID')
    await queueCommand('event', ['--id', String(id), '--stage', 'manual_review', '--outcome', 'remote_present',
      '--reason', `人工在千川确认素材 ID：${remoteId}`])
    const completed = await queueCommand('complete', ['--id', String(id), '--remote-material-id', remoteId])
    await unblockAfterManualReview(numericField(job, 'qianchuanAccountId', 'qianchuan_account_id'))
    return completed
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
    const ordered = rows.reverse()
    const processed = new Set<number>()
    for (const job of ordered) {
      const jobId = jobIdFrom(job['id'])
      if (processed.has(jobId)) continue
      if (stringValue(job['scheduledDate']) > today) continue
      const retryAt = stringValue(job['nextRetryAt'])
      if (retryAt && retryAt > new Date().toLocaleString('sv-SE').replace(' ', 'T')) continue
      const qcId = numericField(job, 'qianchuanAccountId', 'qianchuan_account_id')
      if (schedulerBlockedQianchuanAccounts.has(qcId)) continue
      let connection = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
      if (!connection) {
        const accountId = numericField(job, 'accountId', 'account_id')
        const targets = await queueCommand('targets')
        const targetRows = Array.isArray(targets['rows']) ? targets['rows'] as Json[] : []
        const binding = targetRows.find((row) => Number(row['account_id']) === accountId &&
          Number(row['qianchuan_account_id']) === qcId)
        if (/^\d{8,20}$/.test(stringValue(binding?.['qc_id']))) {
          connection = await existingManagedBrowserConnection(`account-${accountId}`)
        }
      }
      if (!connection) continue
      const group = ordered.filter((candidate) =>
        !processed.has(jobIdFrom(candidate['id'])) &&
        candidate['status'] === 'queued' &&
        stringValue(candidate['scheduledDate']) === today &&
        Number(candidate['accountId']) === Number(job['accountId']) &&
        Number(candidate['qianchuanAccountId']) === qcId &&
        stringValue(candidate['productId']) === stringValue(job['productId']) &&
        (!stringValue(candidate['nextRetryAt']) ||
          stringValue(candidate['nextRetryAt']) <= new Date().toLocaleString('sv-SE').replace(' ', 'T'))
      ).slice(0, 10)
      group.forEach((candidate) => processed.add(jobIdFrom(candidate['id'])))
      const batchOutcome = group.length > 1 ? await runBatchUpload(group) : null
      // 批量预检查尚未触及远端时，安全地回退到现有逐条上传。
      const outcomes: Json[] = []
      if (!batchOutcome || batchOutcome['status'] === 'preflight_failed') {
        for (const candidate of group) {
          const single = await runOneUpload(candidate['id'])
          outcomes.push(single)
          if (single['status'] === 'needs_review' || single['manualActionRequired'] === true) break
        }
      } else {
        outcomes.push(batchOutcome)
      }
      for (const outcome of outcomes) {
        if (outcome['ok'] !== false || outcome['status'] === 'deferred') continue
        const stage = stringValue(outcome['stage'])
        const error = stringValue(outcome['error'])
        if (outcome['status'] === 'needs_review' || outcome['manualActionRequired'] === true ||
            /登录|验证码|无权限/.test(error) || /login|captcha/.test(stage)) {
          schedulerBlockedQianchuanAccounts.add(qcId)
          break
        }
      }
    }
  } catch {
    // 启动早于数据库初始化、或脚本暂不可用时，下一轮继续；不伪造成功。
  } finally {
    schedulerRunning = false
  }
}

async function materialStatusTick(): Promise<void> {
  if (statusRunning || activeCount > 0 || schedulerRunning) return
  statusRunning = true
  try {
    const result = await queueCommand('jobs', ['--status', 'succeeded', '--limit', '1000'])
    const jobs = Array.isArray(result['rows']) ? result['rows'] as Json[] : []
    const now = Date.now()
    // 新上传且未判定的素材优先查询，避免旧任务占满轮询窗口。
    const candidate = jobs.find((job) => {
      const id = Number(job['id'])
      if (!Number.isSafeInteger(id) || (statusRetryAt.get(id) ?? 0) > now) return false
      const checkedAt = stringValue(job['evaluationCheckedAt'])
      const evaluation = stringValue(job['materialEvaluation'])
      const review = stringValue(job['reviewStatus'])
      if (!checkedAt) return true
      const elapsed = now - new Date(checkedAt).getTime()
      if (!Number.isFinite(elapsed)) return true
      // 平台判定可能在初次审核后改变；已给出完整结果的素材每天复核一次。
      if (evaluation && evaluation !== '-' && review && review !== '-') return elapsed >= 24 * 60 * 60_000
      return elapsed >= 30 * 60_000
    })
    if (!candidate) return
    const id = Number(candidate['id'])
    statusRetryAt.set(id, now + 15 * 60_000)
    try {
      await refreshMaterialStatusCore(id)
    } catch (error) {
      console.info(`[千川素材判定] 任务 #${id} 稍后再查：${error instanceof Error ? error.message : String(error)}`)
    }
  } catch (error) {
    console.warn(`[千川素材判定] 自动读取失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    statusRunning = false
  }
}

export function startQianchuanUploadScheduler(): void {
  if (schedulerTimer) return
  schedulerTimer = setInterval(() => { void schedulerTick() }, 60_000)
  statusTimer = setInterval(() => { void materialStatusTick() }, 60_000)
  setTimeout(() => { void schedulerTick() }, 15_000)
  setTimeout(() => { void materialStatusTick() }, 45_000)
}

export function disposeQianchuanUploadScheduler(): void {
  if (schedulerTimer) clearInterval(schedulerTimer)
  schedulerTimer = null
  if (statusTimer) clearInterval(statusTimer)
  statusTimer = null
}
