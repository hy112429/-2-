import { ipcMain } from 'electron'
import { existsSync, mkdirSync } from 'fs'
import { join } from 'path'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'
import {
  closeAutomationLaunchedBrowser,
  commandManagedBrowser,
  existingManagedBrowserConnection,
  validateManagedProfilePath
} from './external_browser'
import { reserveQianchuanAccount, releaseQianchuanAccount } from './qianchuan_account_lock'
import { runOneUpload } from './qianchuan_upload'
import { douyinBatchGate, prepareScheduledQianchuanMaterial } from './post_publish_qianchuan_material'

type Json = Record<string, unknown>
let timer: ReturnType<typeof setInterval> | null = null
let running = false
let tickRunning = false
let preparingScheduled = false

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

interface PlanBrowserLease {
  profileKey: string
  profilePath: string
  port: number
  dedicated: boolean
  automationOpened: boolean
}

async function ensurePlanBrowser(job: Json): Promise<{ lease?: PlanBrowserLease; error?: string }> {
  const qcId = validId(job['qianchuan_account_id'])
  const accountId = validId(job['account_id'])
  const advertiserId = value(job['advertiser_id']).trim()
  if (!/^\d{8,20}$/.test(advertiserId)) return { error: '计划的千川广告主 ID 无效，请检查账号绑定。' }
  const profiles = [
    { profileKey: `qianchuan-${qcId}`, dedicated: true },
    { profileKey: `account-${accountId}`, dedicated: false }
  ]

  for (const profile of profiles) {
    const connection = await existingManagedBrowserConnection(profile.profileKey)
    if (connection) return { lease: { ...connection, ...profile, automationOpened: false } }
  }

  const failures: string[] = []
  for (const profile of profiles) {
    try {
      const profilePath = validateManagedProfilePath(profile.profileKey)
      if (!existsSync(join(profilePath, 'Default'))) continue
      const opened = await commandManagedBrowser('open', {
        url: `https://qianchuan.jinritemai.com/home?aavid=${encodeURIComponent(advertiserId)}`
      }, { profileKey: profile.profileKey })
      if (opened['ok'] !== true) throw new Error(value(opened['error']) || '浏览器未确认打开')
      const connection = await existingManagedBrowserConnection(profile.profileKey)
      if (connection) return { lease: { ...connection, ...profile, automationOpened: true } }
      throw new Error('浏览器已启动，但未取得本机调试连接')
    } catch (error) {
      // 只关闭本客户端这次自动启动的窗口；手动打开的浏览器不会被触碰。
      try { await closeAutomationLaunchedBrowser({ profileKey: profile.profileKey }) } catch { /* 保留启动错误 */ }
      failures.push(`${profile.profileKey}: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
  if (failures.length) {
    return { error: `已有登录资料但无法启动千川计划浏览器；请检查浏览器状态后重试。${failures.join('；')}` }
  }
  return { error: '没有可复用的千川或抖音账号登录资料。请先在账号管理打开对应专属浏览器并完成登录；自动流程不会创建空白登录态。' }
}

async function runOne(jobId: number, expectedDailyBudgetYuan?: number, expectedScheduledAt?: string): Promise<Json> {
  if (running || preparingScheduled) return { ok: false, error: '已有千川计划或定时素材正在处理' }
  running = true
  try { return await runOneCore(jobId, expectedDailyBudgetYuan, expectedScheduledAt) }
  finally { running = false }
}

async function runOneCore(jobId: number, expectedDailyBudgetYuan?: number, expectedScheduledAt?: string): Promise<Json> {
  await command('sync-schedules')
  const all = await command('jobs', ['--id', String(jobId)])
  const job = (Array.isArray(all['rows']) ? all['rows'] as Json[] : []).find((row) => Number(row['id']) === jobId)
  if (!job || job['status'] !== 'ready') return { ok: false, error: '任务已经处理或待核查，不能重复发布' }
  if (expectedScheduledAt && (job['schedule_state'] !== 'scheduled' || job['scheduled_at'] !== expectedScheduledAt)) {
    return { ok: false, error: '定时已取消、暂停或改期，本次调度停止' }
  }
  if (job['schedule_state'] === 'confirm_required') return { ok: false, error: '定时授权已暂停，请重新确认定时' }
  const scheduledAt = job['schedule_state'] === 'scheduled' ? value(job['scheduled_at']) : ''
  if (scheduledAt) {
    const preflight = await command('schedule-preflight', ['--id', String(jobId)])
    const gate = await douyinBatchGate()
    const reviews = await command('jobs', ['--qianchuan-account-id', value(job['qianchuan_account_id']), '--status', 'needs_review', '--limit', '1'])
    const uploadReviews = await uploadCommand('review-accounts')
    const hasReview = (Array.isArray(reviews['rows']) && reviews['rows'].length > 0) ||
      (Array.isArray(uploadReviews['qianchuanAccountIds']) && uploadReviews['qianchuanAccountIds'].map(Number).includes(Number(job['qianchuan_account_id'])))
    const reason = !gate.ready ? gate.reason : hasReview ? '同一千川账户存在上传或计划结果待核查，定时提交等待人工核实' :
      preflight['ready'] !== true ? value(preflight['reason']) : ''
    if (reason) {
      await command('defer', ['--id', String(jobId), '--reason', reason])
      return { ok: false, status: 'waiting', error: reason }
    }
  }
  const qcId = validId(job['qianchuan_account_id'])
  if (!reserveQianchuanAccount(qcId)) return { ok: false, error: '该千川账户正在上传或读取素材' }
  let claimed = false
  let lease: PlanBrowserLease | undefined
  let keepAutomationBrowserOpen = false
  try {
    const prepared = await ensurePlanBrowser(job)
    if (!prepared.lease) {
      const reason = prepared.error || '等待千川账号浏览器就绪'
      await command('defer', ['--id', String(jobId), '--reason', reason])
      return { ok: false, error: reason }
    }
    const activeLease = prepared.lease
    lease = activeLease
    await command('claim', [
      '--id', String(jobId),
      ...(scheduledAt ? ['--expected-scheduled-at', scheduledAt] : []),
      ...(expectedDailyBudgetYuan == null ? [] : ['--expected-daily-budget-yuan', String(expectedDailyBudgetYuan)])
    ])
    claimed = true
    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-plan')
    mkdirSync(diagnosticsDir, { recursive: true })
    const outcome = await runEngineCommand('qianchuan_plan_browser.py', [
      '--db', getClientPaths().databasePath, '--job-id', String(jobId),
      '--profile-path', activeLease.profilePath, '--cdp-port', String(activeLease.port),
      '--douyin-account-id', activeLease.dedicated ? '0' : String(job['account_id']),
      '--diagnostics-dir', diagnosticsDir
    ])
    const browserResult = object(outcome.result)
    const status = outcome.ok && ['published', 'needs_review', 'blocked'].includes(value(browserResult['status']))
      ? value(browserResult['status']) : 'needs_review'
    const submitted = status === 'needs_review' || browserResult['submitAttempted'] === true
    const result = await command('finish', [
      '--id', String(jobId), '--status', status,
      '--submit-attempted', submitted ? '1' : '0',
      '--remote-plan-id', value(browserResult['remotePlanId']),
      '--reason', value(browserResult['reason']) || outcome.error || '浏览器执行器未返回可信结果，需人工核查',
      '--screenshot-path', value(browserResult['screenshotPath']),
      '--selected-material-ids-json', JSON.stringify(Array.isArray(browserResult['selectedMaterialIds'])
        ? browserResult['selectedMaterialIds'] : [])
    ])
    if (status === 'needs_review' || (status === 'blocked' && /登录|验证|访问权限|广告主/.test(value(browserResult['reason'])))) {
      // 需要登录/人工核对的页面留给操作员，避免关窗后增加恢复成本。
      keepAutomationBrowserOpen = true
    }
    return result
  } catch (error) {
    if (claimed) {
      keepAutomationBrowserOpen = true
      try {
        await command('finish', ['--id', String(jobId), '--status', 'needs_review',
          '--submit-attempted', '1', '--reason', `计划执行中断，需先在千川核查：${String(error)}`])
      } catch { /* 状态留给启动恢复处理，不能盲目再次发布。 */ }
    }
    return { ok: false, error: String(error) }
  } finally {
    if (lease?.automationOpened && !keepAutomationBrowserOpen) {
      try { await closeAutomationLaunchedBrowser({ profileKey: lease.profileKey }) }
      catch (error) { console.warn(`[千川计划] 自动启动的浏览器未能关闭：${String(error)}`) }
    }
    releaseQianchuanAccount(qcId)
  }
}

async function runSupplement(id: number): Promise<Json> {
  if (running || preparingScheduled) return { ok: false, error: '已有千川计划或定时素材正在处理' }
  running = true
  try { return await runSupplementCore(id) }
  finally { running = false }
}

async function runSupplementCore(id: number): Promise<Json> {
  const all = await command('supplement-jobs', ['--id', String(id)])
  const job = (Array.isArray(all['rows']) ? all['rows'] as Json[] : [])
    .find((row) => Number(row['id']) === id)
  if (!job || job['status'] !== 'scheduled') return { ok: false, error: '追加任务状态已改变' }
  const qcId = validId(job['qianchuan_account_id'])
  const accountId = validId(job['account_id'])
  if (!reserveQianchuanAccount(qcId)) return { ok: false, error: '该千川账户正在执行其他任务' }
  let claimed = false
  let lease: PlanBrowserLease | undefined
  let keepAutomationBrowserOpen = false
  try {
    const prepared = await ensurePlanBrowser(job)
    if (!prepared.lease) return { ok: false, error: prepared.error || '等待千川账号浏览器就绪' }
    const activeLease = prepared.lease
    lease = activeLease
    await command('claim-supplement', ['--id', String(id)])
    claimed = true
    const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-plan')
    mkdirSync(diagnosticsDir, { recursive: true })
    const outcome = await runEngineCommand('qianchuan_plan_supplement_browser.py', [
      '--db', getClientPaths().databasePath, '--supplement-id', String(id),
      '--profile-path', activeLease.profilePath, '--cdp-port', String(activeLease.port),
      '--douyin-account-id', activeLease.dedicated ? '0' : String(accountId),
      '--diagnostics-dir', diagnosticsDir
    ])
    const browser = object(outcome.result)
    const status = outcome.ok && ['applied', 'already_present', 'waiting', 'needs_review', 'blocked']
      .includes(value(browser['status'])) ? value(browser['status']) : 'needs_review'
    const submitted = status === 'needs_review' || browser['submitAttempted'] === true
    const result = await command('finish-supplement', [
      '--id', String(id), '--status', status, '--submit-attempted', submitted ? '1' : '0',
      '--reason', value(browser['reason']) || outcome.error || '追加结果不明确，需核查原计划',
      '--screenshot-path', value(browser['screenshotPath'])
    ])
    if (status === 'needs_review' || (status === 'blocked' && /登录|验证|访问权限|广告主/.test(value(browser['reason'])))) {
      keepAutomationBrowserOpen = true
    }
    return result
  } catch (error) {
    if (claimed) {
      keepAutomationBrowserOpen = true
      try {
        await command('finish-supplement', ['--id', String(id), '--status', 'needs_review',
          '--submit-attempted', '1', '--reason', `追加执行中断，需核查原计划：${String(error)}`])
      } catch { /* 保留待核查，不重复修改原计划。 */ }
    }
    return { ok: false, error: String(error) }
  } finally {
    if (lease?.automationOpened && !keepAutomationBrowserOpen) {
      try { await closeAutomationLaunchedBrowser({ profileKey: lease.profileKey }) }
      catch (error) { console.warn(`[千川计划] 自动补素材后浏览器未能关闭：${String(error)}`) }
    }
    releaseQianchuanAccount(qcId)
  }
}

async function uploadCommand(action: string, args: string[] = []): Promise<Json> {
  const outcome = await runEngineCommand('qianchuan_upload.py', ['--db', getClientPaths().databasePath, action, ...args])
  const data = object(outcome.result)
  if (!outcome.ok || data['ok'] === false) throw new Error(value(data['error']) || outcome.error || '读取定时素材任务失败')
  return data
}

async function prepareScheduledJob(job: Json): Promise<void> {
  const id = validId(job['id'])
  await command('sync-schedules')
  const freshPlan = await command('jobs', ['--id', String(id)])
  const active = object((freshPlan['rows'] as Json[] | undefined)?.[0])
  if (active['schedule_state'] !== 'scheduled' || active['status'] !== 'ready') return
  job = active
  const snapshot = object(JSON.parse(value(job['schedule_snapshot_json']) || '{}'))
  if (snapshot['video_source'] === 'my_video') return
  const prepared = await prepareScheduledQianchuanMaterial(validId(job['publish_queue_id']))
  if (prepared['waiting'] === true && !prepared['status']) {
    await command('defer', ['--id', String(id), '--reason', value(prepared['reason'])])
    return
  }
  if (prepared['ok'] === false || ['needs_review', 'blocked_auth', 'failed_query', 'failed_download'].includes(value(prepared['status']))) {
    await command('defer', ['--id', String(id), '--reason', value(prepared['error'] ?? prepared['reason']) || '素材准备需要人工处理'])
    return
  }
  const qcId = validId(job['qianchuan_account_id'])
  const reviews = await uploadCommand('review-accounts')
  if ((Array.isArray(reviews['qianchuanAccountIds']) ? reviews['qianchuanAccountIds'] as unknown[] : []).map(Number).includes(qcId)) {
    await command('defer', ['--id', String(id), '--reason', '该千川账户有上传结果待核查，定时素材准备暂停'])
    return
  }
  const uploads = await uploadCommand('jobs', ['--account-id', value(job['account_id']),
    '--qianchuan-account-id', String(qcId), '--product-id', value(job['product_id']),
    '--due-only', '--limit', '500'])
  const due = Array.isArray(uploads['rows']) ? uploads['rows'] as Json[] : []
  if (!due.length) return
  // 仅有已到期上传任务才启动已有登录资料；复用上传器的账号锁和逐件回执。
  const browser = await ensurePlanBrowser(job)
  if (!browser.lease) {
    const reason = browser.error || '等待千川浏览器登录'
    await command(/登录|验证|权限/.test(reason) ? 'pause-schedule' : 'defer', ['--id', String(id), '--reason', reason])
    return
  }
  let keepOpen = false
  try {
    for (const upload of due) {
      // 准备素材期间也允许取消；停止下一件，已提交的上传仍保存回执。
      const fresh = await command('jobs', ['--id', String(id)])
      const current = (fresh['rows'] as Json[] | undefined)?.[0]
      if (current?.['schedule_state'] !== 'scheduled' || current['status'] !== 'ready') break
      const outcome = await runOneUpload(upload['id'])
      if (outcome['manualActionRequired'] === true || outcome['status'] === 'needs_review') {
        keepOpen = true
        await command('pause-schedule', ['--id', String(id), '--reason', value(outcome['error']) || '素材上传结果待核查'])
        break
      }
      if (outcome['ok'] === false) {
        keepOpen = /登录|验证|权限/.test(value(outcome['error']))
        await command(keepOpen ? 'pause-schedule' : 'defer', ['--id', String(id), '--reason', value(outcome['error']) || '素材上传等待续行'])
        break
      }
    }
  } finally {
    if (browser.lease.automationOpened && !keepOpen) {
      await closeAutomationLaunchedBrowser({ profileKey: browser.lease.profileKey })
    }
  }
}

async function tick(): Promise<void> {
  if (running || tickRunning) return
  tickRunning = true
  try {
    await command('sync-schedules')
    await command('sync')
    await command('sync-supplements')
    const pausedAccounts = new Set<number>()
    const attemptedJobs = new Set<number>()
    for (let offset = 0; ; offset += 200) {
      const reviews = await command('jobs', ['--status', 'needs_review', '--offset', String(offset)])
      const page = Array.isArray(reviews['rows']) ? reviews['rows'] as Json[] : []
      for (const review of page) pausedAccounts.add(validId(review['qianchuan_account_id']))
      if (page.length < 200) break
    }

    // 单独确认的逐商品定时不依赖“后续新商品自动发布”开关，仍遵守抖音阶段闸门。
    const scheduled: Json[] = []
    for (let offset = 0; ; offset += 200) {
      const result = await command('jobs', ['--scheduled-only', '--offset', String(offset)])
      const page = Array.isArray(result['rows']) ? result['rows'] as Json[] : []
      scheduled.push(...page)
      if (page.length < 200) break
    }
    const gate = scheduled.length ? await douyinBatchGate() : { ready: true, reason: '' }
    for (const job of scheduled) {
      if (running) break
      const id = validId(job['id'])
      if (pausedAccounts.has(validId(job['qianchuan_account_id'])) || !gate.ready) {
        await command('defer', ['--id', String(id), '--reason', gate.reason || '同一千川账户有计划提交结果待核查'])
        continue
      }
      try {
        preparingScheduled = true
        try { await prepareScheduledJob(job) }
        finally { preparingScheduled = false }
        if (Date.parse(value(job['scheduled_at'])) <= Date.now()) {
          await runOne(id, Number(job['daily_budget_yuan']), value(job['scheduled_at']))
          const latest = await command('jobs', ['--id', String(id)])
          if (object((latest['rows'] as Json[] | undefined)?.[0])['status'] === 'needs_review') {
            pausedAccounts.add(validId(job['qianchuan_account_id']))
          }
        }
      } catch (error) {
        console.warn(`[千川定时] 任务 #${id} 等待续行：${String(error)}`)
      }
    }

    // 始终从队首重新找一条本轮未尝试的任务；已完成任务会离开 ready 集合，
    // 因而不会在分页偏移变化时漏掉 200 条之后的商品，也不会被无登录态任务卡住。
    while (true) {
      let next: Json | undefined
      for (let offset = 0; ; offset += 200) {
        const jobs = await command('jobs', ['--status', 'ready', '--offset', String(offset)])
        const page = Array.isArray(jobs['rows']) ? jobs['rows'] as Json[] : []
        next = page.find((job) => {
          const id = Number(job['id'])
          const qcId = Number(job['qianchuan_account_id'])
          return value(job['creation_source'] ?? 'auto') === 'auto' &&
            Number.isSafeInteger(id) && !attemptedJobs.has(id) &&
            Number.isSafeInteger(qcId)
        })
        if (next || page.length < 200) break
      }
      if (!next) break
      const id = validId(next['id'])
      const qcId = validId(next['qianchuan_account_id'])
      attemptedJobs.add(id)
      if (pausedAccounts.has(qcId)) {
        const reason = '同一千川广告主有计划提交结果待核查；核实前暂停后续自动发布。'
        await command('defer', ['--id', String(id), '--reason', reason])
        continue
      }
      const result = await runOne(id)
      if (result['ok'] === false) {
        console.warn(`[千川计划] 任务 #${id} 暂未执行：${value(result['error'])}`)
      }
      const latest = await command('jobs', ['--id', String(id)])
      const latestJob = (Array.isArray(latest['rows']) ? latest['rows'] as Json[] : [])
        .find((job) => Number(job['id']) === id)
      if (latestJob?.['status'] === 'needs_review') pausedAccounts.add(qcId)
    }

    const attemptedSupplements = new Set<number>()
    while (true) {
      let next: Json | undefined
      for (let offset = 0; ; offset += 200) {
        const supplements = await command('supplement-jobs', [
          '--status', 'scheduled', '--due-only', '--offset', String(offset)
        ])
        const page = Array.isArray(supplements['rows']) ? supplements['rows'] as Json[] : []
        next = page.find((job) => {
          const id = Number(job['id'])
          const qcId = Number(job['qianchuan_account_id'])
          return Number.isSafeInteger(id) && !attemptedSupplements.has(id) &&
            Number.isSafeInteger(qcId) && !pausedAccounts.has(qcId)
        })
        if (next || page.length < 200) break
      }
      if (!next) break
      const id = validId(next['id'])
      attemptedSupplements.add(id)
      const result = await runSupplement(id)
      if (result['ok'] === false) {
        console.warn(`[千川计划] 原计划补素材任务 #${id} 暂未执行：${value(result['error'])}`)
      }
      const refreshed = await command('supplement-jobs', ['--id', String(id)])
      const supplement = (Array.isArray(refreshed['rows']) ? refreshed['rows'] as Json[] : [])
        .find((job) => Number(job['id']) === id)
      if (supplement?.['status'] === 'needs_review') {
        pausedAccounts.add(validId(supplement['qianchuan_account_id']))
      }
    }
  } catch (error) {
    console.warn(`[千川计划] 后台检查失败：${String(error)}`)
  } finally {
    tickRunning = false
  }
}

function reply(operation: () => Promise<Json>): Promise<Json> {
  return operation().catch((error) => ({ ok: false, error: String(error) }))
}

export function registerQianchuanPlanIpc(): void {
  ipcMain.handle('qianchuan:plan:scheduleContext', (_event, raw: unknown) => reply(() => {
    const params = object(raw)
    return command('schedule-context', ['--publish-queue-id', String(validId(params['publishQueueId'])),
      '--qianchuan-account-id', String(validId(params['qianchuanAccountId']))])
  }))
  ipcMain.handle('qianchuan:plan:saveSchedule', (_event, raw: unknown) => reply(async () => {
    const params = object(raw)
    const result = await command('save-schedule', ['--publish-queue-id', String(validId(params['publishQueueId'])),
      '--qianchuan-account-id', String(validId(params['qianchuanAccountId'])),
      '--scheduled-at', value(params['scheduledAt']), '--confirmation-json', value(params['confirmationJson'])])
    void tick()
    return result
  }))
  ipcMain.handle('qianchuan:plan:cancelSchedule', (_event, id: unknown) => reply(() =>
    command('cancel-schedule', ['--id', String(validId(id))])))
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
  ipcMain.handle('qianchuan:plan:jobs', (_event, raw?: unknown) => reply(() => {
    const params = typeof raw === 'number' ? { qianchuanAccountId: raw } : object(raw)
    const args = ['--offset', String(Math.max(0, Number(params['offset'] ?? 0))),
      '--limit', String(Math.min(200, Math.max(1, Number(params['limit'] ?? 20))))]
    if (params['qianchuanAccountId'] != null) args.push('--qianchuan-account-id', String(validId(params['qianchuanAccountId'])))
    if (params['accountId'] != null) args.push('--account-id', String(validId(params['accountId'])))
    const status = value(params['status']).trim()
    if (status) {
      if (!['ready', 'submitting', 'published', 'blocked', 'needs_review'].includes(status)) throw new Error('计划状态筛选值无效')
      args.push('--status', status)
    }
    return command('jobs', args)
  }))
  ipcMain.handle('qianchuan:plan:logs', (_event, raw?: unknown) => reply(() => {
    const params = object(raw)
    const args = ['--offset', String(Math.max(0, Number(params['offset'] ?? 0))),
      '--limit', String(Math.min(200, Math.max(1, Number(params['limit'] ?? 25))))]
    if (params['id'] != null && Number(params['id']) > 0) args.push('--id', String(validId(params['id'])))
    if (params['qianchuanAccountId'] != null) args.push('--qianchuan-account-id', String(validId(params['qianchuanAccountId'])))
    if (params['accountId'] != null) args.push('--account-id', String(validId(params['accountId'])))
    const status = value(params['status']).trim()
    if (status) {
      if (!['ready', 'submitting', 'published', 'blocked', 'needs_review'].includes(status)) throw new Error('计划日志状态筛选值无效')
      args.push('--status', status)
    }
    const startDate = value(params['startDate']).trim()
    const endDate = value(params['endDate']).trim()
    if (startDate) args.push('--start-date', startDate)
    if (endDate) args.push('--end-date', endDate)
    return command('logs', args)
  }))
  ipcMain.handle('qianchuan:plan:candidates', (_event, raw: unknown) => reply(() => {
    const params = object(raw)
    return command('candidates', [
      '--account-id', String(validId(params['accountId'])),
      '--qianchuan-account-id', String(validId(params['qianchuanAccountId']))
    ])
  }))
  ipcMain.handle('qianchuan:plan:createManual', (_event, raw: unknown) => reply(() => {
    const params = object(raw)
    return command('create-manual', [
      '--publish-queue-id', String(validId(params['publishQueueId'])),
      '--qianchuan-account-id', String(validId(params['qianchuanAccountId']))
    ])
  }))
  ipcMain.handle('qianchuan:plan:supplements', () => reply(() => command('supplement-jobs')))
  ipcMain.handle('qianchuan:plan:run', (_event, id: unknown, expectedBudget: unknown) => reply(() => {
    if (expectedBudget == null) return runOne(validId(id))
    const budget = Number(expectedBudget)
    if (!Number.isSafeInteger(budget) || budget < 50) throw new Error('发布前确认的日预算须为至少 50 元的整数')
    return runOne(validId(id), budget)
  }))
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
