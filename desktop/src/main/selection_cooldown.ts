import { app, ipcMain } from 'electron'
import type { IpcMainInvokeEvent, WebContents } from 'electron'
import { appendFileSync, mkdirSync } from 'fs'
import { join } from 'path'
import { getClientPaths } from './app-dirs'

type DeadlineTimerHandle = ReturnType<typeof setTimeout>

export interface DeadlineTimerDependencies {
  now?: () => number
  setTimer?: (callback: () => void, delayMs: number) => DeadlineTimerHandle
  clearTimer?: (timer: DeadlineTimerHandle) => void
}

/**
 * 由调用方提供绝对 deadline。提前唤醒时重新核对时钟，只有到点才执行回调。
 * 放在主进程使用时不受渲染页面后台 setInterval 节流影响；依赖可注入用于时钟单测。
 */
export function scheduleDeadlineCallback(
  deadline: number,
  onDeadline: () => void,
  dependencies: DeadlineTimerDependencies = {}
): () => void {
  const now = dependencies.now ?? Date.now
  const setTimer = dependencies.setTimer ?? ((callback, delayMs) => setTimeout(callback, delayMs))
  const clearTimer = dependencies.clearTimer ?? ((timer) => clearTimeout(timer))
  let timer: DeadlineTimerHandle | null = null
  let cancelled = false

  const checkDeadline = (): void => {
    if (cancelled) return
    const remaining = deadline - now()
    if (remaining <= 0) {
      cancelled = true
      onDeadline()
      return
    }
    timer = setTimer(checkDeadline, Math.min(remaining, 2_147_483_647))
  }

  checkDeadline()
  return () => {
    cancelled = true
    if (timer !== null) clearTimer(timer)
    timer = null
  }
}

export interface SelectionCooldownDiagnostic {
  stage: string
  batchId?: string
  deadline?: number
  [key: string]: unknown
}

type DiagnosticWriter = (entry: SelectionCooldownDiagnostic) => void

const DIAGNOSTIC_FILE = 'cooldown-autoresume.jsonl'
const EVENT_RETRY_MS = 1_000
const MAX_EVENT_SENDS = 120
const RESUME_START_TIMEOUT_MS = 30_000

function writeCooldownDiagnostic(entry: SelectionCooldownDiagnostic): void {
  try {
    const directory = join(getClientPaths().dataDir, 'diagnostics', 'selection')
    mkdirSync(directory, { recursive: true })
    const record = {
      ...entry,
      timestamp: new Date().toISOString(),
      processId: process.pid,
      appVersion: app.getVersion(),
      packaged: app.isPackaged,
      executable: process.execPath,
      appPath: app.getAppPath()
    }
    appendFileSync(join(directory, DIAGNOSTIC_FILE), JSON.stringify(record) + '\n', 'utf8')
  } catch (cause) {
    console.error('[selection-cooldown] 无法写入自动续选诊断：', cause)
  }
}

interface CooldownSchedule {
  batchId: string
  deadline: number
  sender: WebContents
  writeDiagnostic: DiagnosticWriter
  cancelDeadline: () => void
  retryTimer: ReturnType<typeof setTimeout> | null
  settleTimer: ReturnType<typeof setTimeout> | null
  sendCount: number
  acknowledged: boolean
}

const schedulesByWebContents = new Map<number, CooldownSchedule>()

function restoreRendererThrottling(schedule: CooldownSchedule): void {
  try {
    if (!schedule.sender.isDestroyed()) schedule.sender.setBackgroundThrottling(true)
  } catch (cause) {
    schedule.writeDiagnostic({
      stage: 'renderer_throttling_restore_failed',
      batchId: schedule.batchId,
      deadline: schedule.deadline,
      error: String(cause)
    })
  }
}

function cancelSchedule(webContentsId: number, reason: string): void {
  const schedule = schedulesByWebContents.get(webContentsId)
  if (!schedule) return
  schedulesByWebContents.delete(webContentsId)
  schedule.cancelDeadline()
  if (schedule.retryTimer !== null) clearTimeout(schedule.retryTimer)
  schedule.retryTimer = null
  if (schedule.settleTimer !== null) clearTimeout(schedule.settleTimer)
  schedule.settleTimer = null
  restoreRendererThrottling(schedule)
  schedule.writeDiagnostic({
    stage: 'schedule_cleared',
    batchId: schedule.batchId,
    deadline: schedule.deadline,
    reason,
    sends: schedule.sendCount
  })
}

function sendExpiryUntilAcknowledged(schedule: CooldownSchedule): void {
  if (schedulesByWebContents.get(schedule.sender.id) !== schedule) return
  if (schedule.acknowledged) return
  if (schedule.sender.isDestroyed()) {
    schedule.writeDiagnostic({
      stage: 'expiry_event_sender_destroyed', batchId: schedule.batchId, deadline: schedule.deadline
    })
    cancelSchedule(schedule.sender.id, 'sender_destroyed')
    return
  }

  schedule.sendCount += 1
  schedule.writeDiagnostic({
    stage: 'expiry_event_sent',
    batchId: schedule.batchId,
    deadline: schedule.deadline,
    sendCount: schedule.sendCount,
    deadlineLagMs: Date.now() - schedule.deadline
  })
  try {
    schedule.sender.send('selection-cooldown:expired', {
      batchId: schedule.batchId,
      deadline: schedule.deadline,
      sendCount: schedule.sendCount
    })
  } catch (cause) {
    schedule.writeDiagnostic({
      stage: 'expiry_event_send_failed',
      batchId: schedule.batchId,
      deadline: schedule.deadline,
      sendCount: schedule.sendCount,
      error: String(cause)
    })
    cancelSchedule(schedule.sender.id, 'send_failed')
    return
  }

  if (schedule.sendCount >= MAX_EVENT_SENDS) {
    schedule.writeDiagnostic({
      stage: 'expiry_event_ack_timeout',
      batchId: schedule.batchId,
      deadline: schedule.deadline,
      sendCount: schedule.sendCount
    })
    cancelSchedule(schedule.sender.id, 'ack_timeout')
    return
  }
  schedule.retryTimer = setTimeout(() => sendExpiryUntilAcknowledged(schedule), EVENT_RETRY_MS)
}

function handleSchedule(
  event: IpcMainInvokeEvent,
  value: unknown,
  writeDiagnostic: DiagnosticWriter
): { ok: boolean; error?: string } {
  const payload = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const batchId = typeof payload.batchId === 'string' ? payload.batchId.trim() : ''
  const deadline = Number(payload.deadline)
  if (!batchId || !Number.isFinite(deadline) || deadline < 0) {
    return { ok: false, error: '冷却自动续选参数无效。' }
  }

  const sender: WebContents = event.sender
  cancelSchedule(sender.id, 'replaced_by_new_deadline')
  const schedule: CooldownSchedule = {
    batchId,
    deadline,
    sender,
    writeDiagnostic,
    cancelDeadline: () => undefined,
    retryTimer: null,
    settleTimer: null,
    sendCount: 0,
    acknowledged: false
  }
  schedulesByWebContents.set(sender.id, schedule)
  try {
    // renderer 负责选品业务恢复；短暂关闭后台节流，确保隐藏窗口也能处理到期 IPC。
    sender.setBackgroundThrottling(false)
    writeDiagnostic({
      stage: 'renderer_throttling_disabled', batchId, deadline
    })
  } catch (cause) {
    writeDiagnostic({
      stage: 'renderer_throttling_disable_failed', batchId, deadline, error: String(cause)
    })
  }
  writeDiagnostic({ stage: 'deadline_scheduled', batchId, deadline, delayMs: Math.max(0, deadline - Date.now()) })
  schedule.cancelDeadline = scheduleDeadlineCallback(deadline, () => {
    writeDiagnostic({
      stage: 'deadline_expired',
      batchId,
      deadline,
      deadlineLagMs: Date.now() - deadline
    })
    sendExpiryUntilAcknowledged(schedule)
  })
  return { ok: true }
}

function handleAcknowledge(
  event: IpcMainInvokeEvent,
  value: unknown,
  writeDiagnostic: DiagnosticWriter
): { ok: boolean; matched: boolean } {
  const payload = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const batchId = typeof payload.batchId === 'string' ? payload.batchId.trim() : ''
  const deadline = Number(payload.deadline)
  const resumeQueued = payload.resumeQueued === true
  const schedule = schedulesByWebContents.get(event.sender.id)
  const matched = Boolean(schedule && schedule.batchId === batchId && schedule.deadline === deadline)
  writeDiagnostic({ stage: 'expiry_event_acknowledged', batchId, deadline, matched, resumeQueued })
  if (matched && schedule) {
    if (schedule.retryTimer !== null) clearTimeout(schedule.retryTimer)
    schedule.retryTimer = null
    schedule.acknowledged = true
    if (resumeQueued) {
      // 保持 renderer 非节流，直到实际 startSelection 调用 cancel 或保护超时。
      schedule.settleTimer = setTimeout(() => {
        writeDiagnostic({
          stage: 'renderer_resume_start_timeout', batchId, deadline,
          timeoutMs: RESUME_START_TIMEOUT_MS
        })
        cancelSchedule(event.sender.id, 'resume_start_timeout')
      }, RESUME_START_TIMEOUT_MS)
    } else {
      cancelSchedule(event.sender.id, 'renderer_acknowledged_without_resume')
    }
  }
  return { ok: true, matched }
}

function handleTrace(value: unknown, writeDiagnostic: DiagnosticWriter): { ok: boolean } {
  const payload = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const stage = typeof payload.stage === 'string' ? payload.stage.slice(0, 80) : ''
  const batchId = typeof payload.batchId === 'string' ? payload.batchId.slice(0, 100) : ''
  const details = payload.details && typeof payload.details === 'object'
    ? payload.details as Record<string, unknown>
    : {}
  if (!stage) return { ok: false }
  // 限制诊断载荷大小，避免把页面数据或非诊断内容写入本地日志。
  let safeDetails: Record<string, unknown> = {}
  try {
    safeDetails = JSON.parse(JSON.stringify(details)) as Record<string, unknown>
    if (JSON.stringify(safeDetails).length > 4_000) safeDetails = { truncated: true }
  } catch {
    safeDetails = { unserializable: true }
  }
  writeDiagnostic({ ...safeDetails, stage, batchId })
  return { ok: true }
}

/** 注册限流续选 IPC；deadline 到期事件需 renderer 确认，未确认时每秒重发。 */
export function registerSelectionCooldownIpc(writeDiagnostic: DiagnosticWriter = writeCooldownDiagnostic): void {
  ipcMain.handle('selection-cooldown:schedule', (event, value) => handleSchedule(event, value, writeDiagnostic))
  ipcMain.handle('selection-cooldown:ack', (event, value) => handleAcknowledge(event, value, writeDiagnostic))
  ipcMain.handle('selection-cooldown:trace', (_event, value) => handleTrace(value, writeDiagnostic))
  ipcMain.handle('selection-cooldown:cancel', (event) => {
    cancelSchedule(event.sender.id, 'renderer_cancelled')
    return { ok: true }
  })
  writeDiagnostic({
    stage: 'ipc_registered',
    appVersion: app.getVersion(),
    packaged: app.isPackaged,
    executable: process.execPath,
    appPath: app.getAppPath()
  })
}

export function disposeSelectionCooldown(): void {
  for (const webContentsId of schedulesByWebContents.keys()) cancelSchedule(webContentsId, 'app_shutdown')
}
