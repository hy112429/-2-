import { ipcMain } from 'electron'
import { existsSync, mkdirSync } from 'fs'
import { join } from 'path'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'
import { existingManagedBrowserConnection, openManagedBrowser, validateManagedProfilePath } from './external_browser'
import { reserveQianchuanAccount, releaseQianchuanAccount } from './qianchuan_account_lock'

type Json = Record<string, unknown>
let timer: ReturnType<typeof setInterval> | null = null
let running = false

function object(value: unknown): Json { return value && typeof value === 'object' ? value as Json : {} }
function validId(value: unknown): number {
  const id = Number(value)
  if (!Number.isSafeInteger(id) || id < 1) throw new Error('千川计划或账户 ID 无效')
  return id
}
function value(value: unknown): string { return typeof value === 'string' ? value : String(value ?? '') }
async function command(action: string, args: string[] = []): Promise<Json> {
  const outcome = await runEngineCommand('qianchuan_plan.py', ['--db', getClientPaths().databasePath, action, ...args])
  if (!outcome.ok) throw new Error(outcome.error ?? '千川计划任务处理失败')
  const result = object(outcome.result)
  if (result['ok'] === false) throw new Error(value(result['error']))
  return result
}

async function runOne(jobId: number): Promise<Json> {
  if (running) return { ok: false, error: '已有千川计划正在处理' }
  const all = await command('jobs', ['--id', String(jobId)])
  const job = (Array.isArray(all['rows']) ? all['rows'] as Json[] : []).find((row) => Number(row['id']) === jobId)
  if (!job || job['status'] !== 'ready') return { ok: false, error: '任务已经处理或待核查，不能重复发布' }
  const qcId = validId(job['qianchuan_account_id'])
  const accountId = validId(job['account_id'])
  const dedicated = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
  const connection = dedicated ?? await existingManagedBrowserConnection(`account-${accountId}`)
  if (!connection) return { ok: false, error: `千川账户 ${qcId} 或抖音账号 ${accountId} 的专属浏览器未打开，请在账号与登录中打开后再试` }
  if (!reserveQianchuanAccount(qcId)) return { ok: false, error: '该千川账户正在上传或读取素材' }
  running = true
  let claimed = false
  try {
    await command('claim', ['--id', String(jobId)])
    claimed = true
    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-plan')
    mkdirSync(diagnosticsDir, { recursive: true })
    const outcome = await runEngineCommand('qianchuan_plan_browser.py', [
      '--db', getClientPaths().databasePath, '--job-id', String(jobId),
      '--profile-path', connection.profilePath, '--cdp-port', String(connection.port),
      '--douyin-account-id', dedicated ? '0' : String(accountId),
      '--diagnostics-dir', diagnosticsDir
    ])
    const browser = object(outcome.result)
    const status = outcome.ok && ['published', 'needs_review', 'blocked'].includes(value(browser['status']))
      ? value(browser['status']) : 'needs_review'
    const submitted = status === 'needs_review' || browser['submitAttempted'] === true
    const result = await command('finish', [
      '--id', String(jobId), '--status', status,
      '--submit-attempted', submitted ? '1' : '0',
      '--remote-plan-id', value(browser['remotePlanId']),
      '--reason', value(browser['reason']) || outcome.error || '浏览器执行器未返回可信结果，需人工核查',
      '--screenshot-path', value(browser['screenshotPath']),
      '--selected-material-ids-json', JSON.stringify(Array.isArray(browser['selectedMaterialIds'])
        ? browser['selectedMaterialIds'] : [])
    ])
    return result
  } catch (error) {
    if (claimed) {
      try {
        await command('finish', ['--id', String(jobId), '--status', 'needs_review',
          '--submit-attempted', '1', '--reason', `计划执行中断，需先在千川核查：${String(error)}`])
      } catch { /* 状态留给启动恢复处理，不能盲目再次发布。 */ }
    }
    return { ok: false, error: String(error) }
  } finally {
    running = false
    releaseQianchuanAccount(qcId)
  }
}

async function runSupplement(id: number): Promise<Json> {
  if (running) return { ok: false, error: '已有千川计划正在处理' }
  const all = await command('supplement-jobs', ['--id', String(id)])
  const job = (Array.isArray(all['rows']) ? all['rows'] as Json[] : [])
    .find((row) => Number(row['id']) === id)
  if (!job || job['status'] !== 'scheduled') return { ok: false, error: '追加任务状态已改变' }
  const qcId = validId(job['qianchuan_account_id'])
  const accountId = validId(job['account_id'])
  const dedicated = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
  let connection = dedicated ?? await existingManagedBrowserConnection(`account-${accountId}`)
  if (!connection) {
    // 次日任务沿用原抖音账号的持久 Chrome 资料，最多只打开这一个账号。
    const profileKey = `account-${accountId}`
    const profilePath = validateManagedProfilePath(profileKey)
    if (!existsSync(join(profilePath, 'Default'))) {
      return { ok: false, error: '原账号没有已建立的专属浏览器资料，等待登录' }
    }
    const advertiser = value(job['advertiser_id'])
    if (!/^\d{8,20}$/.test(advertiser)) return { ok: false, error: '原千川广告主 ID 无效' }
    const opened = await openManagedBrowser(`https://qianchuan.jinritemai.com/uni-prom?aavid=${advertiser}`,
      { profileKey })
    if (!opened.ok || !opened.profilePath || !opened.port) {
      return { ok: false, error: opened.error || '无法打开原账号专属浏览器' }
    }
    connection = { profilePath: opened.profilePath, port: opened.port }
  }
  if (!reserveQianchuanAccount(qcId)) return { ok: false, error: '该千川账户正在执行其他任务' }
  running = true
  let claimed = false
  try {
    await command('claim-supplement', ['--id', String(id)])
    claimed = true
    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-plan')
    mkdirSync(diagnosticsDir, { recursive: true })
    const outcome = await runEngineCommand('qianchuan_plan_supplement_browser.py', [
      '--db', getClientPaths().databasePath, '--supplement-id', String(id),
      '--profile-path', connection.profilePath, '--cdp-port', String(connection.port),
      '--douyin-account-id', dedicated ? '0' : String(accountId),
      '--diagnostics-dir', diagnosticsDir
    ])
    const browser = object(outcome.result)
    const status = outcome.ok && ['applied', 'already_present', 'waiting', 'needs_review', 'blocked']
      .includes(value(browser['status'])) ? value(browser['status']) : 'needs_review'
    const submitted = status === 'needs_review' || browser['submitAttempted'] === true
    return command('finish-supplement', [
      '--id', String(id), '--status', status, '--submit-attempted', submitted ? '1' : '0',
      '--reason', value(browser['reason']) || outcome.error || '追加结果不明确，需核查原计划',
      '--screenshot-path', value(browser['screenshotPath'])
    ])
  } catch (error) {
    if (claimed) {
      try {
        await command('finish-supplement', ['--id', String(id), '--status', 'needs_review',
          '--submit-attempted', '1', '--reason', `追加执行中断，需核查原计划：${String(error)}`])
      } catch { /* 保留待核查，不重复修改原计划。 */ }
    }
    return { ok: false, error: String(error) }
  } finally {
    running = false
    releaseQianchuanAccount(qcId)
  }
}

async function tick(): Promise<void> {
  if (running) return
  try {
    await command('sync')
    await command('sync-supplements')
    for (let offset = 0; ; offset += 200) {
      const jobs = await command('jobs', ['--status', 'ready', '--offset', String(offset)])
      const ready = Array.isArray(jobs['rows']) ? jobs['rows'] as Json[] : []
      for (const job of ready) {
        const qcId = validId(job['qianchuan_account_id'])
        if (await existingManagedBrowserConnection(`qianchuan-${qcId}`) ||
            await existingManagedBrowserConnection(`account-${validId(job['account_id'])}`)) {
          const result = await runOne(validId(job['id']))
          if (result['ok'] !== false) return // 一个轮次只提交一条计划。
          console.warn(`[千川计划] 任务 #${value(job['id'])} 暂未执行：${value(result['error'])}`)
        }
      }
      if (ready.length < 200) break
    }
    for (let offset = 0; ; offset += 200) {
      const supplements = await command('supplement-jobs', ['--status', 'scheduled', '--due-only', '--offset', String(offset)])
      const due = Array.isArray(supplements['rows']) ? supplements['rows'] as Json[] : []
      for (const job of due) {
        const result = await runSupplement(validId(job['id']))
        if (result['ok'] !== false) return // 一个轮次只处理一个原计划。
      }
      if (due.length < 200) break
    }
  } catch (error) {
    console.warn(`[千川计划] 后台检查失败：${String(error)}`)
  }
}

function reply(operation: () => Promise<Json>): Promise<Json> {
  return operation().catch((error) => ({ ok: false, error: String(error) }))
}

export function registerQianchuanPlanIpc(): void {
  ipcMain.handle('qianchuan:plan:settings', (_event, id: unknown) => reply(() =>
    command('settings', ['--qianchuan-account-id', String(validId(id))])))
  ipcMain.handle('qianchuan:plan:saveSettings', (_event, raw: unknown) => reply(() => {
    const params = object(raw)
    const budget = Number(params['dailyBudgetYuan'])
    if (!Number.isSafeInteger(budget)) throw new Error('日预算须为整数元')
    const source = value(params['videoSource'])
    if (!['uploaded', 'my_video', 'both'].includes(source)) throw new Error('视频来源无效')
    return command('set-settings', [
      '--qianchuan-account-id', String(validId(params['qianchuanAccountId'])),
      '--daily-budget-yuan', String(budget), '--video-source', source,
      '--enabled', params['enabled'] === true ? '1' : '0',
      '--long-running', params['longRunning'] === false ? '0' : '1',
      '--smart-coupon', params['smartCoupon'] === false ? '0' : '1',
      '--aigc-enabled', params['aigcEnabled'] === true ? '1' : '0'
    ])
  }))
  ipcMain.handle('qianchuan:plan:jobs', (_event, qcId?: unknown) => reply(() =>
    command('jobs', qcId == null ? [] : ['--qianchuan-account-id', String(validId(qcId))])))
  ipcMain.handle('qianchuan:plan:supplements', () => reply(() => command('supplement-jobs')))
  ipcMain.handle('qianchuan:plan:run', (_event, id: unknown) => reply(() => runOne(validId(id))))
  ipcMain.handle('qianchuan:plan:retryBlocked', (_event, id: unknown) => reply(() =>
    command('retry-blocked', ['--id', String(validId(id))])))
  ipcMain.handle('qianchuan:plan:resolve', (_event, raw: unknown) => reply(() => {
    const params = object(raw)
    const present = params['present'] === true
    const note = value(params['note']).trim()
    if (note.length < 6) throw new Error('请填写至少 6 字的千川核查依据')
    return command(present ? 'resolve-present' : 'resolve-absent', [
      '--id', String(validId(params['id'])), '--reason', note,
      ...(present ? ['--remote-plan-id', value(params['remotePlanId'])] : [])
    ])
  }))
}

export function startQianchuanPlanScheduler(): void {
  if (timer) return
  void command('recover').then(() => tick()).catch((error) => console.warn(`[千川计划] 恢复失败：${String(error)}`))
  timer = setInterval(() => { void tick() }, 60_000)
}

export function disposeQianchuanPlanScheduler(): void {
  if (timer) clearInterval(timer)
  timer = null
}
