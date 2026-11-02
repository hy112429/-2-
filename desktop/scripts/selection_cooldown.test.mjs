import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/main/selection_cooldown.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const handlers = new Map()
const diagnostics = []
const ipcMain = {
  handle: (channel, handler) => handlers.set(channel, handler)
}
const app = {
  getVersion: () => 'test-version',
  isPackaged: false,
  getAppPath: () => 'test-app-path'
}
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(
  moduleValue.exports,
  (specifier) => {
    if (specifier === 'electron') return { app, ipcMain }
    if (specifier === './app-dirs') return { getClientPaths: () => ({ dataDir: 'unused-by-test' }) }
    if (specifier === 'fs') return fs
    if (specifier === 'path') return path
    throw new Error(`Unexpected module: ${specifier}`)
  },
  moduleValue
)

const { scheduleDeadlineCallback, registerSelectionCooldownIpc } = moduleValue.exports
const selectionBatchSource = await readFile(new URL('../src/renderer/src/utils/selection_batch.ts', import.meta.url), 'utf8')
const selectionBatchCompiled = ts.transpileModule(selectionBatchSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const selectionBatchModule = { exports: {} }
new Function('exports', 'require', 'module', selectionBatchCompiled)(
  selectionBatchModule.exports,
  (specifier) => {
    if (specifier === './selection_error_ux') return { classifySelectionError: () => null }
    throw new Error(`Unexpected selection batch module: ${specifier}`)
  },
  selectionBatchModule
)
const { scheduleAutoResumeSelection } = selectionBatchModule.exports

test('主进程 deadline 到点后续选；模拟后台漏 tick 和提前唤醒', () => {
  let now = 1_000
  let timerCallback
  const delays = []
  let resumed = 0
  const resumeState = {
    cooldownArmed: true,
    cooldownRemaining: 60,
    busy: false,
    batchState: 'needs_retry',
    rateLimitedAccountId: 1,
    pendingAccountIds: [1]
  }
  const cancel = scheduleDeadlineCallback(61_000, () => {
    resumeState.cooldownRemaining = 0
    scheduleAutoResumeSelection(resumeState, () => { resumed += 1 }, (callback) => callback())
  }, {
    now: () => now,
    setTimer: (callback, delayMs) => {
      timerCallback = callback
      delays.push(delayMs)
      return delays.length
    },
    clearTimer: () => undefined
  })

  assert.deepEqual(delays, [60_000])
  // 渲染页在后台没有逐秒 tick；主进程唤醒后以绝对 deadline 再核对。
  now = 60_000
  timerCallback()
  assert.deepEqual(delays, [60_000, 1_000])
  assert.equal(resumed, 0)
  now = 61_000
  timerCallback()
  assert.equal(resumed, 1)
  cancel()
  assert.equal(resumed, 1)
})

test('deadline 已经过期时立即通知，取消中的定时器不再续选', () => {
  let now = 10_000
  let timerCallback
  let resumed = 0
  const cancel = scheduleDeadlineCallback(11_000, () => { resumed += 1 }, {
    now: () => now,
    setTimer: (callback) => { timerCallback = callback; return 1 },
    clearTimer: () => undefined
  })
  cancel()
  now = 11_000
  timerCallback()
  assert.equal(resumed, 0)

  scheduleDeadlineCallback(10_999, () => { resumed += 1 }, { now: () => now })
  assert.equal(resumed, 1)
})

test('主进程 IPC 只接受有效批次 deadline，并按批次发送到期通知', () => {
  registerSelectionCooldownIpc((entry) => diagnostics.push(entry))
  const sent = []
  const throttling = []
  const sender = {
    id: 7,
    isDestroyed: () => false,
    send: (...args) => sent.push(args),
    setBackgroundThrottling: (enabled) => throttling.push(enabled)
  }
  const schedule = handlers.get('selection-cooldown:schedule')
  const acknowledge = handlers.get('selection-cooldown:ack')
  const trace = handlers.get('selection-cooldown:trace')
  const cancel = handlers.get('selection-cooldown:cancel')
  assert.deepEqual(schedule({ sender }, { batchId: '', deadline: Date.now() }), {
    ok: false,
    error: '冷却自动续选参数无效。'
  })
  const deadline = Date.now() - 1
  assert.deepEqual(schedule({ sender }, { batchId: 'batch-1', deadline }), { ok: true })
  assert.equal(sent.length, 1)
  assert.equal(sent[0][0], 'selection-cooldown:expired')
  assert.deepEqual(sent[0][1], { batchId: 'batch-1', deadline, sendCount: 1 })
  assert.deepEqual(throttling, [false])
  assert.deepEqual(acknowledge({ sender }, { batchId: 'batch-1', deadline, resumeQueued: true }), { ok: true, matched: true })
  assert.deepEqual(throttling, [false], '恢复已入队时继续关闭后台节流，直到批次真正启动')
  assert.deepEqual(cancel({ sender }), { ok: true })
  assert.deepEqual(throttling, [false, true], '实际启动/取消冷却后恢复原后台节流设置')
  assert.deepEqual(trace({}, {
    batchId: 'batch-1', stage: 'renderer_expiry_event_received', details: { eligible: true, busy: false }
  }), { ok: true })
  assert.ok(diagnostics.some((entry) => entry.stage === 'renderer_expiry_event_received' && entry.eligible === true))
})

test('到期 IPC 未确认时重发；恢复入队确认后停止重发并等待真正启动', async () => {
  registerSelectionCooldownIpc((entry) => diagnostics.push(entry))
  const sent = []
  const sender = {
    id: 8,
    isDestroyed: () => false,
    send: (...args) => sent.push(args),
    setBackgroundThrottling: () => undefined
  }
  const schedule = handlers.get('selection-cooldown:schedule')
  const acknowledge = handlers.get('selection-cooldown:ack')
  const cancel = handlers.get('selection-cooldown:cancel')
  const deadline = Date.now() + 40
  try {
    assert.deepEqual(schedule({ sender }, { batchId: 'batch-retry', deadline }), { ok: true })
    await new Promise((resolve) => setTimeout(resolve, 1_150))
    assert.ok(sent.length >= 2, '未收到 renderer ACK 时应至少发送两次')
    const sendsAtAck = sent.length
    assert.deepEqual(acknowledge({ sender }, { batchId: 'batch-retry', deadline, resumeQueued: true }), {
      ok: true, matched: true
    })
    await new Promise((resolve) => setTimeout(resolve, 1_100))
    assert.equal(sent.length, sendsAtAck, 'ACK 后停止重复发送，到 startSelection 再取消保持态')
  } finally {
    cancel({ sender })
  }
})
