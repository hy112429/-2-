import { app, BrowserWindow, ipcMain } from 'electron'
import { dirname, join } from 'path'
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'fs'

const SCHEDULE_CHANNEL = 'selection-schedule:due'
const CHECK_INTERVAL_MS = 15_000

export interface SelectionScheduleDue {
  date: string
  time: string
  triggeredAt: number
}

interface StoredSelectionSchedule {
  enabled: boolean
  time: string
  /** 只记录成功完成或明确登录阻断的日期；普通尝试不消耗当天计划。 */
  lastSuccessfulDate?: string
  lastLoginBlockedDate?: string
  pendingDue?: SelectionScheduleDue | null
}

export interface SelectionScheduleSnapshot {
  enabled: boolean
  time: string
  launchAtLogin: boolean
  canLaunchAtLogin: boolean
  pendingDue: SelectionScheduleDue | null
}

let timer: NodeJS.Timeout | null = null
let schedulerActive = false
let store: StoredSelectionSchedule = { enabled: false, time: '09:00', pendingDue: null }

function storePath(): string {
  return join(app.getPath('userData'), 'selection-schedule.json')
}

function writeDiagnostic(stage: string, details: Record<string, unknown> = {}): void {
  try {
    const directory = join(app.getPath('userData'), 'diagnostics', 'selection')
    mkdirSync(directory, { recursive: true })
    appendFileSync(join(directory, 'daily-schedule.jsonl'), JSON.stringify({
      stage,
      timestamp: new Date().toISOString(),
      processId: process.pid,
      appVersion: app.getVersion(),
      packaged: app.isPackaged,
      ...details
    }) + '\n', 'utf8')
  } catch (cause) {
    console.error('[selection-schedule] 无法写入定时选品诊断：', cause)
  }
}

function validTime(value: unknown): value is string {
  if (typeof value !== 'string' || !/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return false
  return true
}

function readStore(): StoredSelectionSchedule {
  try {
    const parsed = JSON.parse(readFileSync(storePath(), 'utf-8')) as Partial<StoredSelectionSchedule>
    return {
      enabled: parsed.enabled === true,
      time: validTime(parsed.time) ? parsed.time : '09:00',
      lastSuccessfulDate: typeof parsed.lastSuccessfulDate === 'string' ? parsed.lastSuccessfulDate : undefined,
      lastLoginBlockedDate: typeof parsed.lastLoginBlockedDate === 'string' ? parsed.lastLoginBlockedDate : undefined,
      pendingDue: parsed.pendingDue &&
        typeof parsed.pendingDue.date === 'string' &&
        validTime(parsed.pendingDue.time) &&
        Number.isFinite(parsed.pendingDue.triggeredAt)
        ? parsed.pendingDue
        : null
    }
  } catch {
    return { enabled: false, time: '09:00', pendingDue: null }
  }
}

function persistStore(): void {
  const path = storePath()
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, JSON.stringify(store, null, 2), 'utf-8')
}

function localDateKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function localTimeKey(date: Date): string {
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

function canLaunchAtLogin(): boolean {
  return process.platform === 'win32' && app.isPackaged
}

function snapshot(): SelectionScheduleSnapshot {
  let launchAtLogin = false
  if (canLaunchAtLogin()) {
    try {
      launchAtLogin = app.getLoginItemSettings().openAtLogin
    } catch {
      launchAtLogin = false
    }
  }
  return {
    enabled: store.enabled,
    time: store.time,
    launchAtLogin,
    canLaunchAtLogin: canLaunchAtLogin(),
    pendingDue: store.enabled ? store.pendingDue ?? null : null
  }
}

function broadcastDue(due: SelectionScheduleDue): void {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed() && !window.webContents.isDestroyed()) {
      window.webContents.send(SCHEDULE_CHANNEL, due)
    }
  }
}

function tick(): void {
  if (!schedulerActive) return
  const now = new Date()
  const today = localDateKey(now)
  let changed = false

  if (!store.enabled && store.pendingDue) {
    store.pendingDue = null
    changed = true
  }

  // 未完成的计划跨日保留并继续续跑；只有成功完成或明确需要重新登录才结束当天的运行。
  if (store.enabled && !store.pendingDue &&
      store.lastSuccessfulDate !== today && store.lastLoginBlockedDate !== today &&
      localTimeKey(now) >= store.time) {
    const due = { date: today, time: store.time, triggeredAt: now.getTime() }
    store.pendingDue = due
    changed = true
    writeDiagnostic('due_created', { date: due.date, time: due.time, triggeredAt: due.triggeredAt })
  }

  if (changed) persistStore()
  if (store.pendingDue) broadcastDue(store.pendingDue)
}

export function registerSelectionScheduleIpc(): void {
  store = readStore()
  ipcMain.handle('selection-schedule:get', () => snapshot())
  ipcMain.handle('selection-schedule:save', (_event, input: unknown) => {
    if (!input || typeof input !== 'object') return { ok: false, error: '定时选品设置无效。' }
    const value = input as { enabled?: unknown; time?: unknown; launchAtLogin?: unknown }
    if (typeof value.enabled !== 'boolean') return { ok: false, error: '请确认是否启用每日定时选品。' }
    if (!validTime(value.time)) return { ok: false, error: '请选择有效的每日开始时间。' }
    const launchAtLogin = value.enabled === true && value.launchAtLogin === true
    if (launchAtLogin && !canLaunchAtLogin()) {
      return { ok: false, error: '随 Windows 登录启动仅在已安装的 Windows 客户端中可设置。' }
    }
    try {
      if (canLaunchAtLogin()) {
        app.setLoginItemSettings({ openAtLogin: launchAtLogin, path: app.getPath('exe') })
      }
    } catch (error) {
      return { ok: false, error: `设置 Windows 登录启动失败：${error instanceof Error ? error.message : String(error)}` }
    }
    store.enabled = value.enabled
    store.time = value.time
    if (store.enabled) {
      // 保存启用设置视为用户确认继续；不沿用旧版本“只要尝试过就占用当天”的标记。
      store.lastLoginBlockedDate = undefined
    }
    if (!store.enabled) store.pendingDue = null
    persistStore()
    tick()
    return { ok: true, schedule: snapshot() }
  })
  ipcMain.handle('selection-schedule:ack', (_event, date: unknown, outcome: unknown) => {
    if (typeof date !== 'string' || store.pendingDue?.date !== date) {
      writeDiagnostic('due_ack_ignored', {
        date: typeof date === 'string' ? date : null,
        outcome: typeof outcome === 'string' ? outcome : 'unknown',
        pendingDate: store.pendingDue?.date ?? null
      })
      return { ok: false, matched: false, cleared: false }
    }
    const allowedOutcomes = new Set(['completed', 'login-blocked', 'disabled'])
    const result = typeof outcome === 'string' && allowedOutcomes.has(outcome) ? outcome : 'unknown'
    if (!allowedOutcomes.has(result)) {
      writeDiagnostic('due_ack_deferred', {
        date,
        time: store.pendingDue.time,
        outcome: result,
        reason: 'non_terminal_outcome_keeps_retry_active'
      })
      return { ok: true, matched: true, cleared: false }
    }
    const resolvedDate = localDateKey(new Date())
    if (result === 'completed') store.lastSuccessfulDate = resolvedDate
    if (result === 'login-blocked') store.lastLoginBlockedDate = resolvedDate
    writeDiagnostic('due_acknowledged', { date, time: store.pendingDue.time, outcome: result })
    store.pendingDue = null
    persistStore()
    tick()
    return { ok: true, matched: true, cleared: true }
  })
  ipcMain.handle('selection-schedule:trace', (_event, input: unknown) => {
    if (!input || typeof input !== 'object') return { ok: false }
    const value = input as { date?: unknown; stage?: unknown; attempt?: unknown; delayMs?: unknown }
    if (typeof value.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.date) || typeof value.stage !== 'string') {
      return { ok: false }
    }
    const allowedStages = new Set([
      'attempt_started', 'retry_scheduled', 'login_blocked', 'run_completed', 'schedule_disabled', 'attempt_error'
    ])
    if (!allowedStages.has(value.stage)) return { ok: false }
    writeDiagnostic(`renderer_${value.stage}`, {
      date: value.date,
      attempt: Number.isInteger(value.attempt) ? value.attempt : undefined,
      delayMs: Number.isFinite(value.delayMs) ? value.delayMs : undefined
    })
    return { ok: true }
  })
}

export function startSelectionScheduleScheduler(): void {
  if (timer) return
  schedulerActive = true
  tick()
  timer = setInterval(tick, CHECK_INTERVAL_MS)
}

export function disposeSelectionScheduleScheduler(): void {
  if (timer) clearInterval(timer)
  timer = null
  schedulerActive = false
}
