import { BrowserWindow } from 'electron'
import { appendFile, mkdir } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { getClientPaths } from './app-dirs'
import {
  DEFAULT_PUBLISH_JITTER_MAX_SECONDS,
  DEFAULT_PUBLISH_JITTER_MIN_SECONDS,
  PUBLISH_SCHEDULER_BASE_INTERVAL_MS,
  getNextPublishSchedulerDelayMs,
  readPublishJitterSeconds
} from './publish_scheduler'
import { runEngineCommand, type ScriptOutcome } from './engine'
import { commandManagedBrowser, validateManagedProfilePath, type ManagedBrowserProgress } from './external_browser'

export interface AutoPublishProgressPayload extends ManagedBrowserProgress {
  state: 'idle' | 'running' | 'succeeded' | 'warning' | 'failed'
  queueId?: number
  accountId?: number
  productName?: string
  updatedAt: string
  error?: string
  classification?: string
  canConfirmNoDraft?: boolean
}

interface AutoPublishTask {
  id: number
  account_id: number
  product_id: string
  product_name: string
  product_url: string
  video_path: string
  description: string
  schedule_at: string
}

interface ManualRetryTask {
  id: number
  accountId: number
  productId: string
  productName: string
  productUrl: string
  videoPath: string
  description: string
  autoPublish: boolean
}

export interface ManualRetryOptions {
  confirmedNotSubmitted?: boolean
  confirmedNoDraft?: boolean
}

let timer: ReturnType<typeof setTimeout> | null = null
let stopped = false
let firstSchedulerTick = true
let defaultInitialization: Promise<void> | null = null
let activeRun: { queueId: number | null; promise: Promise<void> } | null = null
let manualRetryPending = 0
let manualRetryWaiters: Array<() => void> = []
let currentProgress: AutoPublishProgressPayload = {
  state: 'idle',
  stage: '等待到期发布任务',
  updatedAt: new Date().toISOString()
}

function broadcastProgress(patch: Partial<AutoPublishProgressPayload>): void {
  currentProgress = { ...currentProgress, ...patch, updatedAt: new Date().toISOString() }
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed() && !window.webContents.isDestroyed()) {
      window.webContents.send('publish:autoProgress', currentProgress)
    }
  }
}

export function getAutoPublisherProgress(): AutoPublishProgressPayload {
  return { ...currentProgress }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : {}
}

async function runQueueCommand(...args: string[]): Promise<Record<string, unknown>> {
  const paths = getClientPaths()
  const outcome: ScriptOutcome = await runEngineCommand('publish_queue.py', ['--db', paths.databasePath, ...args])
  if (!outcome.ok) throw new Error(outcome.error ?? '自动发布队列命令失败')
  const result = asRecord(outcome.result)
  if (result.ok === false) throw new Error(String(result.error ?? '自动发布队列命令失败'))
  return result
}

async function writeSchedulerDiagnostic(stage: string, details: Record<string, unknown> = {}): Promise<void> {
  try {
    const path = join(getClientPaths().dataDir, 'diagnostics', 'publish', 'scheduler.jsonl')
    await mkdir(dirname(path), { recursive: true })
    await appendFile(path, `${JSON.stringify({ at: new Date().toISOString(), stage, ...details })}\n`, 'utf8')
  } catch (error) {
    console.warn(`[发布调度] 诊断日志写入失败：${error instanceof Error ? error.message : String(error)}`)
  }
}

async function getProfilePath(accountId: number): Promise<string | undefined> {
  const paths = getClientPaths()
  const outcome = await runEngineCommand('selection_settings.py', [
    '--db', paths.databasePath, 'get', '--account-id', String(accountId)
  ])
  if (!outcome.ok) throw new Error(outcome.error ?? '读取账号浏览器配置失败')
  const result = asRecord(outcome.result)
  if (result.ok === false) throw new Error(String(result.error ?? '读取账号浏览器配置失败'))
  const settings = asRecord(result.settings)
  const account = asRecord(settings.account)
  const profilePath = account.browserProfilePath
  return typeof profilePath === 'string' && profilePath.trim() ? profilePath : undefined
}

async function publishOneDueTask(requestedQueueId?: number, immediate = false): Promise<void> {
  let task: AutoPublishTask | null = null
  let uploadStarted = false
  let failureCheckpointSaved = false
  let finalSubmitStageReached = false
  let submissionMayHaveBeenAttempted = false
  let submissionAttemptKnown = false
  let stage = immediate ? '校验马上发布任务' : '读取到期自动发布任务'
  const reportStage = (nextStage: string, extra: Partial<AutoPublishProgressPayload> = {}): void => {
    stage = nextStage
    if (!task) return
    broadcastProgress({
      state: 'running',
      queueId: task.id,
      accountId: task.account_id,
      productName: task.product_name,
      stage: nextStage,
      message: '',
      percent: undefined,
      indeterminate: true,
      error: undefined,
      ...extra
    })
  }
  try {
    const next = immediate
      ? await runQueueCommand('publish-now', '--id', String(requestedQueueId))
      : await runQueueCommand(
          'auto-next',
          ...(requestedQueueId === undefined ? [] : ['--id', String(requestedQueueId)])
        )
    const skippedStaleTasks = Array.isArray(next.staleTasksSkipped)
      ? next.staleTasksSkipped.map((value) => asRecord(value))
      : []
    if (skippedStaleTasks.length > 0) {
      const summary = skippedStaleTasks
        .map((row) => `#${String(row.id ?? '?')} ${String(row.productName ?? '商品')}`)
        .join('、')
      await writeSchedulerDiagnostic('overdue_tasks_skipped', {
        count: skippedStaleTasks.length,
        taskIds: skippedStaleTasks.map((row) => Number(row.id)).filter(Number.isInteger),
        policy: 'catchup_max_late_hours'
      })
      broadcastProgress({
        state: 'warning',
        stage: '逾期计划已跳过',
        message: `按本机补发期限设置，${skippedStaleTasks.length} 条过期计划已跳过，未执行平台操作：${summary}`,
        percent: 100,
        indeterminate: false,
        error: undefined
      })
    }
    if (!next.task) {
      if (requestedQueueId !== undefined) {
        const message = next.autoPublishEnabled === false
          ? '全自动发布当前已关闭；任务保留在队列中，没有触发抖音操作。'
          : `队列任务 #${requestedQueueId} 当前不可立即执行；任务保留在队列中，没有触发抖音操作。`
        broadcastProgress({
          state: 'failed',
          queueId: requestedQueueId,
          stage: '自动发布未启动',
          message,
          error: message,
          indeterminate: false
        })
      }
      return
    }
    task = asRecord(next.task) as unknown as AutoPublishTask
    if (!Number.isInteger(task.id) || !Number.isInteger(task.account_id)) {
      throw new Error('自动发布队列返回的任务或账号 ID 无效')
    }
    const scheduledAtMs = new Date(task.schedule_at).getTime()
    const latenessMs = Number.isFinite(scheduledAtMs) ? Math.max(0, Date.now() - scheduledAtMs) : null
    if (latenessMs !== null && latenessMs > 0) {
      await writeSchedulerDiagnostic('overdue_task_selected', {
        queueId: task.id,
        accountId: task.account_id,
        scheduleAt: task.schedule_at,
        latenessMs,
        startupCheck: firstSchedulerTick
      })
    }
    reportStage(immediate ? '已通过校验，开始马上发布' : '已到计划时间，开始处理发布任务', {
      message: `商品：${task.product_name}`
    })

    reportStage('锁定自动发布任务')
    const started = await runQueueCommand('upload-start', '--id', String(task.id))
    if (started.ok !== true) throw new Error(String(started.error ?? '无法锁定自动发布任务'))
    uploadStarted = true
    if (Number(started.id) !== task.id || Number(started.accountId) !== task.account_id) {
      throw new Error('上传锁定回执与当前任务或账号不匹配；任务已转待核查')
    }

    reportStage('读取账号专属浏览器配置')
    const profilePath = await getProfilePath(task.account_id)
    validateManagedProfilePath(`account-${task.account_id}`, profilePath)

    reportStage('上传视频、关联商品并提交发布')
    const prepared = await commandManagedBrowser('prepare-douyin-publish', {
      accountId: task.account_id,
      queueId: task.id,
      productId: String(started.productId ?? task.product_id),
      productName: String(started.productName ?? task.product_name),
      productUrl: String(started.productUrl ?? task.product_url),
      videoPath: String(started.videoPath ?? task.video_path),
      description: String(started.description ?? task.description ?? task.product_name),
      autoPublish: true
    }, {
      profileKey: `account-${task.account_id}`,
      profilePath
    }, (progress: ManagedBrowserProgress) => {
      if (progress.stage === '提交抖音发布') finalSubmitStageReached = true
      reportStage(progress.stage, {
        message: progress.message ?? '',
        percent: progress.percent,
        indeterminate: progress.indeterminate,
        index: progress.index,
        total: progress.total
      })
    })

    if (
      prepared.isolated === true &&
      prepared.classification === 'PUBLISH_VERIFY_OTHER_ROW_ISOLATED' &&
      String(prepared.productId ?? '') === String(started.productId ?? task.product_id)
    ) {
      const warning = String(prepared.warning ?? prepared.error ?? '百应商品核验重查耗尽，本任务已隔离；未上传视频。')
      reportStage('保存商品核验隔离记录', { message: warning })
      const isolated = await runQueueCommand('upload-isolate', '--id', String(task.id), '--error', warning)
      if (isolated.ok !== true || isolated.isolated !== true) {
        throw new Error(String(isolated.error ?? '商品核验隔离检查点未能保存'))
      }
      failureCheckpointSaved = true
      broadcastProgress({
        state: 'warning',
        queueId: task.id,
        accountId: task.account_id,
        productName: task.product_name,
        stage: '商品核验已隔离',
        message: warning,
        percent: 100,
        indeterminate: false,
        error: undefined
      })
      console.warn(`[自动发布] 任务 #${task.id} 已隔离，队列继续；${warning}`)
      return
    }

    if (
      prepared.isolated === true &&
      prepared.classification === 'CREATOR_COVER_REQUIRED_ISOLATED' &&
      Number(prepared.queueId) === task.id &&
      String(prepared.productId ?? '') === String(started.productId ?? task.product_id)
    ) {
      const warning = String(prepared.warning ?? prepared.error ?? '平台要求封面，当前任务已隔离；未记录为发布成功。')
      reportStage('平台封面要求已隔离', { message: warning })
      const isolated = await runQueueCommand('upload-isolate', '--id', String(task.id), '--error', warning)
      if (isolated.ok !== true || isolated.isolated !== true) {
        throw new Error(String(isolated.error ?? '平台封面隔离检查点未能保存'))
      }
      failureCheckpointSaved = true
      broadcastProgress({
        state: 'warning',
        queueId: task.id,
        accountId: task.account_id,
        productName: task.product_name,
        stage: '平台封面要求已隔离',
        message: warning,
        percent: 100,
        indeterminate: false,
        error: undefined
      })
      console.warn(`[自动发布] 任务 #${task.id} 因平台缺封面要求已隔离，队列继续；${warning}`)
      return
    }

    if (prepared.ok !== true) {
      if (typeof prepared.submissionAttempted === 'boolean') {
        submissionMayHaveBeenAttempted = prepared.submissionAttempted
        submissionAttemptKnown = true
      } else if (finalSubmitStageReached) {
        // CDP 在最终提交阶段断开且没有明确结果时，按可能已点击处理，禁止回到上传重试。
        submissionMayHaveBeenAttempted = true
      }
      throw new Error(String(prepared.error ?? '创作者中心自动发布未完成'))
    }
    // 自动化返回成功表示最终按钮已执行并识别到平台回执；即使写本地回执失败，也不能按未发布处理。
    submissionMayHaveBeenAttempted = true
    submissionAttemptKnown = true
    const platformStatus = String(prepared.platformStatus ?? '')
    if (!['published', 'submitted_review'].includes(platformStatus) || prepared.manualPublishRequired !== false) {
      throw new Error('创作者中心未返回可确认的自动发布回执；任务已转待核查')
    }

    reportStage('保存平台发布回执', { message: `平台状态：${platformStatus}` })
    const finished = await runQueueCommand(
      'upload-finish', '--id', String(task.id), '--status', platformStatus,
      '--product-url', String(prepared.productUrl ?? started.productUrl ?? task.product_url)
    )
    if (finished.ok !== true) throw new Error(String(finished.error ?? '平台已处理，但无法保存发布回执'))
    broadcastProgress({
      state: 'succeeded',
      queueId: task.id,
      accountId: task.account_id,
      productName: task.product_name,
      stage: platformStatus === 'published' ? '发布成功' : '已提交平台审核',
      message: '平台回执已保存到本地队列',
      percent: 100,
      indeterminate: false,
      error: undefined
    })
    console.info(`[自动发布] 任务 #${task.id} 已保存平台状态：${platformStatus}`)
  } catch (error) {
    const message = `${stage}失败：${error instanceof Error ? error.message : String(error)}`
    if (task) {
      broadcastProgress({
        state: 'failed',
        queueId: task.id,
        accountId: task.account_id,
        productName: task.product_name,
        stage: `${stage}失败`,
        message,
        percent: undefined,
        indeterminate: false,
        error: message
      })
    } else if (requestedQueueId !== undefined) {
      broadcastProgress({
        state: 'failed',
        queueId: requestedQueueId,
        stage: `${stage}失败`,
        message,
        percent: undefined,
        indeterminate: false,
        error: message
      })
    }
    if (task) {
      try {
        if (uploadStarted) {
          const failureStatus = submissionMayHaveBeenAttempted || (finalSubmitStageReached && !submissionAttemptKnown)
            ? 'submission_uncertain'
            : finalSubmitStageReached
              ? 'uploaded_waiting_publish'
              : 'needs_review'
          const checkpoint = await runQueueCommand(
            'upload-finish', '--id', String(task.id), '--status', failureStatus, '--error', message
          )
          failureCheckpointSaved = checkpoint.ok === true && checkpoint.platformStatus === failureStatus
        } else {
          const checkpoint = await runQueueCommand('auto-hold', '--id', String(task.id), '--error', message)
          failureCheckpointSaved = checkpoint.ok === true && checkpoint.held === true
        }
      } catch (writeError) {
        console.error(`[自动发布] 任务 #${task.id} 检查点写入失败：${String(writeError)}`)
      }
    }
    // 单项任务已隔离时继续调度；只有队列级故障或隔离检查点无法落库时暂停，防止原任务被重复执行。
    if (!immediate && (!task || !failureCheckpointSaved)) {
      try {
        await runQueueCommand('auto-settings', 'set', '--enabled', 'false')
      } catch (pauseError) {
        console.error(`[自动发布] 队列级故障后无法关闭全局自动发布：${String(pauseError)}`)
      }
    }
    const failureLabel = immediate
      ? '马上发布失败'
      : task && failureCheckpointSaved
        ? `任务 #${task.id} 已隔离，后续任务继续`
        : '队列级故障，自动发布已暂停'
    console.error(`[自动发布] ${failureLabel}：${message}`)
  }
}

async function tick(requestedQueueId?: number): Promise<void> {
  if (stopped || manualRetryPending > 0) return
  if (!defaultInitialization) {
    defaultInitialization = runQueueCommand('auto-settings', 'initialize-default').then((result) => {
      if (result.ok !== true) throw new Error(String(result.error ?? '初始化全自动发布默认模式失败'))
    })
  }
  try {
    await defaultInitialization
  } catch (error) {
    const message = `初始化全自动发布失败：${error instanceof Error ? error.message : String(error)}`
    console.error(`[自动发布] ${message}`)
    if (requestedQueueId !== undefined) {
      broadcastProgress({
        state: 'failed',
        queueId: requestedQueueId,
        stage: '初始化自动发布设置失败',
        message,
        error: message,
        indeterminate: false
      })
    }
    return
  }
  if (activeRun) {
    const previous = activeRun
    const previousUpdatedAt = currentProgress.updatedAt
    await previous.promise
    if (
      requestedQueueId === undefined ||
      (currentProgress.queueId === requestedQueueId &&
        currentProgress.state !== 'running' && currentProgress.updatedAt !== previousUpdatedAt)
    ) return
  }
  if (stopped || manualRetryPending > 0) return
  const run = {
    queueId: requestedQueueId ?? null,
    promise: publishOneDueTask(requestedQueueId)
  }
  activeRun = run
  try {
    await run.promise
  } catch (error) {
    console.error(`[自动发布] 检查队列失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    if (activeRun === run) activeRun = null
  }
}

/** 选品生成新视频后立即执行指定队列项；等待当前自动发布完成，避免并发争用浏览器。 */
export async function runAutoPublisherNow(queueId: number): Promise<AutoPublishProgressPayload> {
  if (!Number.isInteger(queueId) || queueId < 1) throw new Error('自动发布任务编号无效')
  await waitForManualRetries()
  await tick(queueId)
  if (currentProgress.queueId !== queueId) {
    throw new Error(currentProgress.error ?? '自动发布器未能启动该任务；请检查自动发布开关或现有待核查任务。')
  }
  return getAutoPublisherProgress()
}

/** 单次忽略定时等待和全局自动开关，仍通过原队列锁与自动发布流程执行。 */
export async function runImmediatePublishNow(queueId: number): Promise<AutoPublishProgressPayload> {
  if (!Number.isInteger(queueId) || queueId < 1) throw new Error('发布任务编号无效')
  await waitForManualRetries()
  if (activeRun) {
    const previous = activeRun
    const previousUpdatedAt = currentProgress.updatedAt
    await previous.promise
    if (
      currentProgress.queueId === queueId &&
      currentProgress.state !== 'running' &&
      currentProgress.updatedAt !== previousUpdatedAt
    ) return getAutoPublisherProgress()
  }
  if (stopped) throw new Error('发布器正在关闭，请稍后重试')

  const run = {
    queueId,
    promise: publishOneDueTask(queueId, true)
  }
  activeRun = run
  try {
    await run.promise
  } finally {
    if (activeRun === run) activeRun = null
  }
  if (currentProgress.queueId !== queueId) {
    throw new Error(currentProgress.error ?? '马上发布未能启动；请刷新队列查看任务状态')
  }
  return getAutoPublisherProgress()
}

function readManualRetryTask(value: unknown): ManualRetryTask {
  const row = asRecord(value)
  const task: ManualRetryTask = {
    id: Number(row.id),
    accountId: Number(row.accountId),
    productId: String(row.productId ?? ''),
    productName: String(row.productName ?? ''),
    productUrl: String(row.productUrl ?? ''),
    videoPath: String(row.videoPath ?? ''),
    description: String(row.description ?? ''),
    autoPublish: row.autoPublish === true
  }
  if (!Number.isInteger(task.id) || task.id < 1 || !Number.isInteger(task.accountId) || task.accountId < 1) {
    throw new Error('手动重发任务或账号 ID 无效')
  }
  if (!task.productId || !task.productName || !task.productUrl || !task.videoPath || !task.description) {
    throw new Error('手动重发任务缺少商品、视频或简介，未执行平台操作')
  }
  return task
}

function waitForManualRetries(): Promise<void> {
  if (manualRetryPending === 0) return Promise.resolve()
  return new Promise((resolve) => manualRetryWaiters.push(resolve))
}

async function publishOneManualRetryTask(queueId: number, options: ManualRetryOptions): Promise<void> {
  let task: ManualRetryTask | null = null
  let route = ''
  let stage = '读取单条重发检查点'
  let finalSubmitStageReached = false
  let submissionMayHaveBeenAttempted = false
  let submissionAttemptKnown = false
  let checkpointSaved = false
  const reportStage = (nextStage: string, message = ''): void => {
    stage = nextStage
    broadcastProgress({
      state: 'running',
      queueId,
      accountId: task?.accountId,
      productName: task?.productName,
      stage,
      message,
      percent: undefined,
      indeterminate: true,
      error: undefined,
      classification: undefined,
      canConfirmNoDraft: false
    })
  }
  reportStage(stage, '按本条原任务设置继续；不检查其它待核查任务')
  try {
    const plan = await runQueueCommand('manual-retry-plan', '--id', String(queueId))
    task = readManualRetryTask(plan.task)
    if (task.id !== queueId) throw new Error('手动重发检查点与所选队列项不一致')

    reportStage('校验账号专属浏览器', '先确认原账号登录环境，再锁定本条检查点')
    const profilePath = await getProfilePath(task.accountId)
    validateManagedProfilePath(`account-${task.accountId}`, profilePath)

    const args = [
      'manual-retry-start', '--id', String(queueId),
      '--expected-status', String(plan.previousStatus ?? '')
    ]
    if (options.confirmedNotSubmitted === true) args.push('--confirmed-not-submitted')
    if (options.confirmedNoDraft === true) args.push('--confirmed-no-draft')
    const started = await runQueueCommand(...args)
    route = String(started.route ?? '')
    const lockedTask = readManualRetryTask(started.task)
    if (lockedTask.id !== task.id || lockedTask.accountId !== task.accountId ||
        lockedTask.productId !== task.productId || lockedTask.autoPublish !== task.autoPublish) {
      throw new Error('手动重发锁定回执与原任务不一致；请刷新后核查')
    }
    task = lockedTask
    console.info(`[手动重发] 任务 #${queueId} 从 ${String(plan.previousStatus ?? '')} 进入 ${route}，原 auto_publish=${task.autoPublish}`)

    if (route === 'ready_manual') {
      broadcastProgress({
        state: 'warning', queueId, accountId: task.accountId, productName: task.productName,
        stage: '待人工发布', message: '已恢复到已上传待发布；原任务未启用自动点击发布，请检查后点击本条“发布”。',
        percent: 100, indeterminate: false, error: undefined
      })
      console.info(`[手动重发] 任务 #${queueId} 已恢复待人工发布，未点击最终发布`)
      return
    }
    if (!['resume_upload', 'upload_new', 'submit'].includes(route)) {
      throw new Error('手动重发检查点返回未知路径，未执行平台操作')
    }

    if (route === 'submit') {
      reportStage('核对已有投稿并提交', '只核验当前投稿，不重新上传或关联商品')
      submissionMayHaveBeenAttempted = true
      const submitted = await commandManagedBrowser('submit-douyin-publish', {
        accountId: task.accountId, queueId, productId: task.productId,
        productName: task.productName, productUrl: task.productUrl,
        videoPath: task.videoPath, description: task.description
      }, { profileKey: `account-${task.accountId}`, profilePath }, (progress) => {
        reportStage(`手动重发：${progress.stage}`, progress.message ?? '')
      })
      if (typeof submitted.submissionAttempted === 'boolean') {
        submissionMayHaveBeenAttempted = submitted.submissionAttempted
        submissionAttemptKnown = true
      }
      if (submitted.isolated === true &&
          submitted.classification === 'CREATOR_COVER_REQUIRED_ISOLATED' &&
          Number(submitted.queueId) === queueId &&
          String(submitted.productId ?? '') === task.productId) {
        const warning = String(submitted.warning ?? submitted.error ?? '平台要求封面，当前任务已隔离')
        const isolated = await runQueueCommand('submit-isolate', '--id', String(queueId), '--error', warning)
        checkpointSaved = isolated.ok === true && isolated.isolated === true
        if (!checkpointSaved) throw new Error('手动重发提交隔离检查点未能保存')
        broadcastProgress({
          state: 'warning', queueId, accountId: task.accountId, productName: task.productName,
          stage: '手动重发已隔离', message: warning, percent: 100,
          indeterminate: false, error: undefined
        })
        console.warn(`[手动重发] 任务 #${queueId} 提交阶段再次隔离：${warning}`)
        return
      }
      if (submitted.ok !== true) throw new Error(String(submitted.error ?? '抖音未确认最终提交'))
      submissionMayHaveBeenAttempted = true
      const platformStatus = String(submitted.platformStatus ?? '')
      if (Number(submitted.accountId) !== task.accountId || Number(submitted.queueId) !== queueId ||
          String(submitted.productId ?? '') !== task.productId ||
          !['published', 'submitted_review'].includes(platformStatus)) {
        throw new Error('平台回执与本条重发任务不一致；请核对创作者中心')
      }
      reportStage('保存手动重发回执', `平台状态：${platformStatus}`)
      const finished = await runQueueCommand(
        'submit-finish', '--id', String(queueId), '--status', platformStatus
      )
      checkpointSaved = finished.ok === true
      if (!checkpointSaved) throw new Error('平台已处理，但本地提交检查点保存失败')
      broadcastProgress({
        state: 'succeeded', queueId, accountId: task.accountId, productName: task.productName,
        stage: platformStatus === 'published' ? '手动重发成功' : '手动重发已提交审核',
        message: '已核验并保存平台回执', percent: 100, indeterminate: false, error: undefined
      })
      console.info(`[手动重发] 任务 #${queueId} 已保存平台状态：${platformStatus}`)
      return
    }

    reportStage(route === 'resume_upload' ? '恢复已有投稿' : '核实无草稿后重新上传',
      route === 'resume_upload' ? '先核对现有视频，不重新选择文件' : '已获人工确认无草稿，开始新的上传尝试')
    const prepared = await commandManagedBrowser('prepare-douyin-publish', {
      accountId: task.accountId, queueId, productId: task.productId,
      productName: task.productName, productUrl: task.productUrl,
      videoPath: task.videoPath, description: task.description,
      autoPublish: task.autoPublish, resumeAfterUpload: route === 'resume_upload',
      preserveExistingDraft: true
    }, { profileKey: `account-${task.accountId}`, profilePath }, (progress) => {
      if (progress.stage === '提交抖音发布') finalSubmitStageReached = true
      reportStage(`手动重发：${progress.stage}`, progress.message ?? '')
    })
    if (typeof prepared.submissionAttempted === 'boolean') {
      submissionMayHaveBeenAttempted = prepared.submissionAttempted
      submissionAttemptKnown = true
    }
    if (prepared.isolated === true &&
        ['PUBLISH_VERIFY_OTHER_ROW_ISOLATED', 'CREATOR_COVER_REQUIRED_ISOLATED'].includes(String(prepared.classification ?? '')) &&
        String(prepared.productId ?? '') === task.productId) {
      const warning = String(prepared.warning ?? prepared.error ?? '平台要求人工核查，本条已隔离')
      const isolated = await runQueueCommand('upload-isolate', '--id', String(queueId), '--error', warning)
      checkpointSaved = isolated.ok === true && isolated.isolated === true
      if (!checkpointSaved) throw new Error('手动重发隔离检查点未能保存')
      broadcastProgress({
        state: 'warning', queueId, accountId: task.accountId, productName: task.productName,
        stage: '手动重发已隔离', message: warning, percent: 100,
        indeterminate: false, error: undefined
      })
      console.warn(`[手动重发] 任务 #${queueId} 上传阶段再次隔离：${warning}`)
      return
    }
    if (prepared.ok !== true) throw new Error(String(prepared.error ?? '恢复投稿未完成'))
    const platformStatus = String(prepared.platformStatus ?? '')
    if (Number(prepared.accountId) !== task.accountId || String(prepared.productId ?? '') !== task.productId ||
        (task.autoPublish
          ? (!['published', 'submitted_review'].includes(platformStatus) || prepared.manualPublishRequired !== false)
          : (platformStatus !== 'uploaded_waiting_publish' || prepared.manualPublishRequired !== true))) {
      throw new Error('平台回执与本条重发任务或原自动发布设置不一致；请核查')
    }
    if (task.autoPublish) submissionMayHaveBeenAttempted = true
    reportStage('保存手动重发检查点', `平台状态：${platformStatus}`)
    const finished = await runQueueCommand(
      'upload-finish', '--id', String(queueId), '--status', platformStatus,
      '--product-url', String(prepared.productUrl ?? task.productUrl)
    )
    checkpointSaved = finished.ok === true
    if (!checkpointSaved) throw new Error('平台已处理，但本地上传检查点保存失败')
    broadcastProgress({
      state: task.autoPublish ? 'succeeded' : 'warning',
      queueId, accountId: task.accountId, productName: task.productName,
      stage: task.autoPublish
        ? platformStatus === 'published' ? '手动重发成功' : '手动重发已提交审核'
        : '待人工发布',
      message: task.autoPublish ? '已保存平台回执' : '视频与商品已准备完成；原任务未启用自动发布，请检查后点击本条“发布”。',
      percent: 100, indeterminate: false, error: undefined
    })
    console.info(`[手动重发] 任务 #${queueId} 已保存平台状态：${platformStatus}`)
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error)
    const missingPost = (route === 'resume_upload' &&
      /没有找到(?:本账号)?现有(?:的)?创作者中心视频页|未能确认已有视频预览/.test(reason)) ||
      (route === 'submit' && /没有找到本账号现有的创作者中心投稿页/.test(reason))
    const message = `${stage}失败：${reason}`
    if (route && !checkpointSaved) {
      try {
        if (route === 'submit') {
          const failureStatus = submissionMayHaveBeenAttempted ? 'submission_uncertain' : 'uploaded_waiting_publish'
          const saved = await runQueueCommand(
            'submit-finish', '--id', String(queueId), '--status', failureStatus, '--error', message
          )
          checkpointSaved = saved.ok === true
        } else if (route === 'resume_upload' || route === 'upload_new') {
          const failureStatus = submissionMayHaveBeenAttempted || (finalSubmitStageReached && !submissionAttemptKnown)
            ? 'submission_uncertain'
            : finalSubmitStageReached ? 'uploaded_waiting_publish' : 'needs_review'
          const saved = await runQueueCommand(
            'upload-finish', '--id', String(queueId), '--status', failureStatus, '--error', message
          )
          checkpointSaved = saved.ok === true
        }
      } catch (writeError) {
        console.error(`[手动重发] 任务 #${queueId} 失败检查点无法保存：${String(writeError)}`)
      }
    }
    broadcastProgress({
      state: 'failed', queueId, accountId: task?.accountId, productName: task?.productName,
      stage: `${stage}失败`,
      message: checkpointSaved || !route ? message : `${message}；失败检查点保存失败，请先核对队列状态`,
      error: message, percent: undefined, indeterminate: false,
      classification: missingPost && checkpointSaved ? 'MANUAL_RETRY_EXISTING_POST_NOT_FOUND' : undefined,
      canConfirmNoDraft: missingPost && checkpointSaved
    })
    console.error(`[手动重发] 任务 #${queueId} 失败：${message}`)
  }
}

/** 人工选中的单条任务走原账号/商品检查点；与后台自动发布共用串行锁。 */
export async function runManualRetryPublishNow(queueId: number, options: ManualRetryOptions = {}): Promise<AutoPublishProgressPayload> {
  if (!Number.isInteger(queueId) || queueId < 1) throw new Error('手动重发任务编号无效')
  if (stopped) throw new Error('发布器正在关闭，请稍后重试')
  if (!options || typeof options !== 'object' ||
      (options.confirmedNotSubmitted !== undefined && typeof options.confirmedNotSubmitted !== 'boolean') ||
      (options.confirmedNoDraft !== undefined && typeof options.confirmedNoDraft !== 'boolean')) {
    throw new Error('手动重发确认参数无效')
  }
  manualRetryPending += 1
  try {
    while (activeRun) await activeRun.promise
    const run = { queueId, promise: publishOneManualRetryTask(queueId, options) }
    activeRun = run
    try {
      await run.promise
    } finally {
      if (activeRun === run) activeRun = null
    }
    return getAutoPublisherProgress()
  } finally {
    manualRetryPending -= 1
    if (manualRetryPending === 0) {
      const waiters = manualRetryWaiters
      manualRetryWaiters = []
      for (const resolve of waiters) resolve()
    }
  }
}

async function runSchedulerCycle(): Promise<void> {
  if (stopped) return
  const startupCheck = firstSchedulerTick
  await writeSchedulerDiagnostic('scheduler_tick_started', {
    startupCheck,
    baseIntervalMs: PUBLISH_SCHEDULER_BASE_INTERVAL_MS
  })
  try {
    await tick()
  } catch (error) {
    console.error(`[发布调度] 检查到期计划失败：${error instanceof Error ? error.message : String(error)}`)
    await writeSchedulerDiagnostic('scheduler_tick_failed', {
      startupCheck,
      reason: error instanceof Error ? error.message : String(error)
    })
  } finally {
    firstSchedulerTick = false
  }
  if (stopped) return

  let jitterMinSeconds = DEFAULT_PUBLISH_JITTER_MIN_SECONDS
  let jitterMaxSeconds = DEFAULT_PUBLISH_JITTER_MAX_SECONDS
  try {
    const settings = await runQueueCommand('auto-settings', 'get')
    jitterMinSeconds = readPublishJitterSeconds(settings.publishJitterMinSeconds, DEFAULT_PUBLISH_JITTER_MIN_SECONDS)
    jitterMaxSeconds = readPublishJitterSeconds(settings.publishJitterMaxSeconds, DEFAULT_PUBLISH_JITTER_MAX_SECONDS)
    if (jitterMinSeconds > jitterMaxSeconds) {
      jitterMinSeconds = DEFAULT_PUBLISH_JITTER_MIN_SECONDS
      jitterMaxSeconds = DEFAULT_PUBLISH_JITTER_MAX_SECONDS
    }
  } catch (error) {
    console.warn(`[发布调度] 读取错峰设置失败，使用默认值：${error instanceof Error ? error.message : String(error)}`)
  }
  const delayMs = getNextPublishSchedulerDelayMs(jitterMinSeconds, jitterMaxSeconds)
  const jitterMs = delayMs - PUBLISH_SCHEDULER_BASE_INTERVAL_MS
  const nextAt = new Date(Date.now() + delayMs).toISOString()
  await writeSchedulerDiagnostic('scheduler_next_tick_scheduled', {
    startupCheck,
    baseIntervalMs: PUBLISH_SCHEDULER_BASE_INTERVAL_MS,
    jitterMinSeconds,
    jitterMaxSeconds,
    chosenJitterMs: jitterMs,
    delayMs,
    nextAt
  })
  if (stopped) return
  timer = setTimeout(() => {
    timer = null
    void runSchedulerCycle()
  }, delayMs)
}

/** 客户端启动后立即补查逾期任务；其后按本机随机错峰间隔串行轮询。 */
export function startAutoPublisher(): void {
  if (timer) return
  stopped = false
  firstSchedulerTick = true
  void runSchedulerCycle()
}

export function disposeAutoPublisher(): void {
  stopped = true
  if (timer) clearTimeout(timer)
  timer = null
}
