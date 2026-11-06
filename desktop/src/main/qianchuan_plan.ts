import { ipcMain } from 'electron'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'
import { existingManagedBrowserConnection } from './external_browser'
import { releaseQianchuanAccount, reserveQianchuanAccount } from './qianchuan_account_lock'

type Json = Record<string, unknown>
const activeJobs = new Set<number>()
let timer: ReturnType<typeof setInterval> | null = null
let ticking = false

function object(value: unknown): Json {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : {}
}

function positiveId(value: unknown, label: string): number {
  const id = Number(value)
  if (!Number.isSafeInteger(id) || id < 1) throw new Error(`${label}无效`)
  return id
}

async function command(...args: string[]): Promise<Json> {
  const result = await runEngineCommand('qianchuan_plan.py', ['--db', getClientPaths().databasePath, ...args])
  if (!result.ok) throw new Error(result.error ?? '千川投放计划状态命令失败')
  const payload = object(result.result)
  if (payload['ok'] === false) throw new Error(String(payload['error'] ?? '千川投放计划状态命令失败'))
  return payload
}

export function scheduleQianchuanPlanForPublish(queueId: number): void {
  setImmediate(() => {
    void command('refresh', '--publish-queue-id', String(positiveId(queueId, '发布队列 ID')))
      .then((result) => {
        const job = object(result['job'])
        if (result['queued'] === true && job['id']) void runOnePlan(Number(job['id']))
      })
      .catch((error) => console.info(`[千川投放计划] 发布队列 #${queueId} 暂未入队：${String(error)}`))
  })
}

async function runOnePlan(id: number): Promise<Json> {
  if (activeJobs.has(id)) return { ok: false, reason: '本进程正在处理该计划' }
  const queued = object((await command('job', '--id', String(id)))['job'])
  if (queued['status'] !== 'queued') return { ok: false, reason: '计划任务不处于待执行状态' }
  const qcId = positiveId(queued['qianchuan_account_id'], '千川账户 ID')
  if (!reserveQianchuanAccount(qcId)) return { ok: false, reason: '该千川账户正在处理其他任务' }
  activeJobs.add(id)
  let claimed = false
  try {
    const connection = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
    if (!connection) return { ok: false, reason: '千川专属浏览器未打开；保留待执行计划' }
    const claim = await command('claim', '--id', String(id))
    if (claim['claimed'] !== true) return claim
    claimed = true
    const job = object(claim['job'])
    const diagnosticDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-plan')
    mkdirSync(diagnosticDir, { recursive: true })
    const outcome = await runEngineCommand('qianchuan_plan_browser.py', [
      'publish', '--cdp-port', String(connection.port), '--profile-path', connection.profilePath,
      '--qianchuan-account-id', String(qcId), '--advertiser-id', String(job['advertiser_id']),
      '--product-id', String(job['product_id']), '--plan-name', String(job['plan_name']),
      '--budget-fen', String(job['budget_fen']), '--material-source', String(job['material_source']),
      '--remote-material-id', String(job['remote_material_id'] ?? ''),
      '--remote-material-name', String(claim['remoteMaterialName'] ?? ''),
      '--published-title', String(claim['publishedTitle'] ?? ''),
      '--video-path', String(claim['videoPath'] ?? ''),
      '--smart-coupon', String(job['smart_coupon']), '--aigc-enabled', String(job['aigc_enabled']),
      '--diagnostics-dir', diagnosticDir
    ])
    // 浏览器进程意外退出可能发生在点击发布之后，必须停在待核查。
    const browser = object(outcome.result)
    if (!outcome.ok) {
      const reason = outcome.error ?? '千川浏览器进程异常，远端计划状态未知'
      await command('finish', '--id', String(id), '--status', 'needs_review', '--reason', reason, '--submit-attempted')
      return { ok: false, status: 'needs_review', reason }
    }
    const status = browser['status'] === 'succeeded' && browser['remotePlanId'] ? 'succeeded'
      : browser['submitAttempted'] === true ? 'needs_review' : 'failed'
    const args = ['finish', '--id', String(id), '--status', status,
      '--reason', String(browser['reason'] ?? ''), '--remote-plan-id', String(browser['remotePlanId'] ?? ''),
      '--diagnostic-path', String(browser['diagnosticPath'] ?? '')]
    if (browser['submitAttempted'] === true) args.push('--submit-attempted')
    const finished = await command(...args)
    return { ok: status === 'succeeded', status, job: finished['job'] }
  } catch (error) {
    const reason = String(error)
    if (claimed) {
      try {
        await command('finish', '--id', String(id), '--status', 'needs_review', '--reason', reason, '--submit-attempted')
      } catch { /* 下次启动由遗留 creating 状态提示人工核查，不能自动重发。 */ }
    }
    return { ok: false, status: claimed ? 'needs_review' : 'failed', reason }
  } finally {
    activeJobs.delete(id)
    releaseQianchuanAccount(qcId)
  }
}

async function tick(): Promise<void> {
  if (ticking) return
  ticking = true
  try {
    await command('recover', '--older-than-minutes', '30')
    await command('refresh-waiting')
    const result = await command('jobs')
    const rows = Array.isArray(result['rows']) ? result['rows'].map(object) : []
    for (const row of rows.reverse()) {
      if (row['status'] === 'queued') await runOnePlan(positiveId(row['id'], '计划任务 ID'))
    }
  } catch (error) {
    console.info(`[千川投放计划] 本轮调度未完成：${String(error)}`)
  } finally {
    ticking = false
  }
}

export function startQianchuanPlanScheduler(): void {
  if (timer) return
  timer = setInterval(() => { void tick() }, 60_000)
  setTimeout(() => { void tick() }, 15_000)
}

export function disposeQianchuanPlanScheduler(): void {
  if (timer) clearInterval(timer)
  timer = null
}

export function registerQianchuanPlanIpc(): void {
  ipcMain.handle('qianchuan:plan:config', (_event, qcId: unknown) =>
    command('config', '--qianchuan-account-id', String(positiveId(qcId, '千川账户 ID'))))
  ipcMain.handle('qianchuan:plan:saveConfig', (_event, params: {
    qianchuanAccountId: unknown; enabled: unknown; dailyBudgetYuan: unknown;
    materialSource: unknown; smartCoupon: unknown; aigcEnabled: unknown
  }) => {
    if (typeof params?.enabled !== 'boolean' || typeof params?.smartCoupon !== 'boolean' ||
        typeof params?.aigcEnabled !== 'boolean') throw new Error('计划开关参数无效')
    const budget = Number(params?.dailyBudgetYuan)
    const fen = Math.round(budget * 100)
    if (!Number.isFinite(budget) || !Number.isSafeInteger(fen) || budget <= 0 ||
        Math.abs(fen / 100 - budget) > 0.0001) throw new Error('日预算须为大于 0 的金额，最多两位小数')
    if (!['homepage', 'uploaded', 'both'].includes(String(params?.materialSource))) throw new Error('素材来源无效')
    return command('config', '--qianchuan-account-id', String(positiveId(params.qianchuanAccountId, '千川账户 ID')),
      '--enabled', params.enabled ? '1' : '0', '--daily-budget-fen', String(fen),
      '--material-source', String(params.materialSource), '--smart-coupon', params.smartCoupon ? '1' : '0',
      '--aigc-enabled', params.aigcEnabled ? '1' : '0')
  })
  ipcMain.handle('qianchuan:plan:jobs', (_event, qcId?: unknown) =>
    qcId ? command('jobs', '--qianchuan-account-id', String(positiveId(qcId, '千川账户 ID'))) : command('jobs'))
  ipcMain.handle('qianchuan:plan:retrySafe', (_event, id: unknown) =>
    command('retry-safe', '--id', String(positiveId(id, '计划任务 ID'))))
  ipcMain.handle('qianchuan:plan:reviewPresent', (_event, params: { id: unknown; remotePlanId: unknown }) => {
    const remoteId = String(params?.remotePlanId ?? '').trim()
    if (!/^[A-Za-z0-9_-]{6,64}$/.test(remoteId)) throw new Error('请填写千川页面显示的有效计划 ID')
    return command('resolve-review', '--id', String(positiveId(params.id, '计划任务 ID')), '--remote-plan-id', remoteId)
  })
  ipcMain.handle('qianchuan:plan:reviewAbsent', (_event, params: { id: unknown; note: unknown }) => {
    const note = String(params?.note ?? '').trim()
    if (note.length < 6) throw new Error('请填写至少 6 字核查依据')
    return command('resolve-review', '--id', String(positiveId(params.id, '计划任务 ID')), '--note', note)
  })
}
