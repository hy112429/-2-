import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/renderer/src/utils/selection_batch.ts', import.meta.url), 'utf8')
const settingsViewSource = await readFile(new URL('../src/renderer/src/components/SelectionSettingsView.vue', import.meta.url), 'utf8')
const errorUxSource = await readFile(new URL('../src/renderer/src/utils/selection_error_ux.ts', import.meta.url), 'utf8')
const errorUxCompiled = ts.transpileModule(errorUxSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const errorUxModule = { exports: {} }
new Function('exports', 'require', 'module', errorUxCompiled)(errorUxModule.exports, () => undefined, errorUxModule)
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(
  moduleValue.exports,
  (specifier) => specifier === './selection_error_ux' ? errorUxModule.exports : undefined,
  moduleValue
)
const {
  runSequentialSelectionBatch,
  createSingleFlight,
  selectCandidateIdsForAccount,
  findAccountResourcePathConflicts,
  getSelectionErrorRecoveryState,
  getSelectionBatchFinalState,
  isUnconfirmedLoginFalseNegative,
  retryUnconfirmedLoginCheck,
  canAutoResumeSelection,
  scheduleAutoResumeSelection,
  evaluateSelectionCooldownResume,
  compareRecoverySettings,
  decideRecoverySettingsChange,
  getSelectionRecoveryMode,
  filterPendingRecoveryAccounts,
  buildSelectionCategoryPlan,
  resolveRecoveryCategorySelection
} = moduleValue.exports

test('恢复批次只执行选中的未完成账号且不改写其他检查点', async () => {
  const batchAccounts = [
    { accountId: 1, accountName: '主号A', state: 'completed', stage: 'completed' },
    { accountId: 2, accountName: '矩阵号B', state: 'failed', stage: 'cart_pending' },
    { accountId: 3, accountName: '矩阵号C', state: 'queued', stage: 'queued' }
  ]

  const selected = filterPendingRecoveryAccounts(batchAccounts, new Set([2]))
  const started = []
  const outcomes = await runSequentialSelectionBatch(
    selected.map((account) => ({ id: account.accountId, name: account.accountName, status: 'active' })),
    async (account) => {
      started.push(account.id)
      return { status: '完成' }
    },
    () => undefined
  )

  assert.deepEqual(selected.map((account) => account.accountId), [2])
  assert.deepEqual(started, [2])
  assert.deepEqual(outcomes.map((outcome) => outcome.accountId), [2])
  assert.equal(batchAccounts[1].stage, 'cart_pending')
  assert.equal(batchAccounts[2].state, 'queued')
})

test('恢复账号筛选忽略已完成账号和批次外账号', () => {
  const batchAccounts = [
    { accountId: 1, state: 'completed' },
    { accountId: 2, state: 'needs_retry' }
  ]

  assert.deepEqual(
    filterPendingRecoveryAccounts(batchAccounts, new Set([1, 99])).map((account) => account.accountId),
    []
  )
})

test('验证码、登录和浏览器断连错误停在 failed，不进入限流自动续跑', () => {
  const switchReasons = [
    '阶段失败：百应要求手动完成滑块验证。',
    'ACCOUNT_SWITCH_LOGIN：当前账号创作者中心尚未登录',
    'ACCOUNT_SWITCH_DISCONNECT：托管 Chrome 已关闭，CDP 连接失效'
  ]
  for (const reason of switchReasons) {
    const state = getSelectionErrorRecoveryState(reason)
    assert.equal(state, 'failed')
    assert.equal(canAutoResumeSelection({
      cooldownArmed: true,
      cooldownRemaining: 0,
      busy: false,
      batchState: state,
      rateLimitedAccountId: 1,
      pendingAccountIds: [1, 2]
    }), false, reason)
  }
  assert.equal(getSelectionErrorRecoveryState('数据库暂时不可用'), 'failed')
})

test('百应 11001 限流错误会保存为可恢复检查点', () => {
  assert.equal(getSelectionErrorRecoveryState('material_list 返回业务错误 11001；已进入 60 秒查询冷却'), 'needs_retry')
  assert.equal(getSelectionErrorRecoveryState('BAIYING_RATE_LIMIT：百应暂时限流'), 'needs_retry')
  assert.equal(getSelectionErrorRecoveryState('百应提示：选品太多，请稍后再试'), 'needs_retry')
  assert.equal(getSelectionErrorRecoveryState('接口请求过多，请等待冷却后继续'), 'needs_retry')
  assert.equal(getSelectionErrorRecoveryState('Too many requests, please wait'), 'needs_retry')
  assert.equal(canAutoResumeSelection({
    cooldownArmed: true,
    cooldownRemaining: 0,
    busy: false,
    batchState: 'needs_retry',
    rateLimitedAccountId: 1,
    pendingAccountIds: [1]
  }), true)
})

test('页面已打开但工作台短暂未识别时只复查一次并可继续', async () => {
  const states = [
    {
      baiying: { state: 'logged_in', message: '百应已登录' },
      creatorCenter: { state: 'unknown', message: '页面已开但未确认登录工作台' }
    },
    {
      baiying: { state: 'logged_in', message: '百应已登录' },
      creatorCenter: { state: 'logged_in', message: '创作者中心工作台已确认登录' }
    }
  ]
  let calls = 0
  const delays = []
  let retryNotified = 0
  const result = await retryUnconfirmedLoginCheck(
    async () => states[calls++],
    isUnconfirmedLoginFalseNegative,
    { wait: async (milliseconds) => delays.push(milliseconds), onRetry: () => { retryNotified += 1 } }
  )

  assert.equal(result.attempts, 2)
  assert.equal(calls, 2)
  assert.deepEqual(delays, [500])
  assert.equal(retryNotified, 1)
  assert.equal(result.value.creatorCenter.state, 'logged_in')
})

test('真实登录/验证码/断连和普通 unknown 不触发登录假阴性重试', async () => {
  const cases = [
    { state: 'login_required', message: '需要登录' },
    { state: 'verification_required', message: '需要人工验证码' },
    { state: 'unknown', message: '页面已开但未确认登录工作台；需要验证码' },
    { state: 'unknown', message: '托管 Chrome 已断开' },
    { state: 'unknown', message: '登录接口暂时不可用' }
  ]
  for (const creatorCenter of cases) {
    assert.equal(isUnconfirmedLoginFalseNegative({
      baiying: { state: 'logged_in', message: '百应已登录' },
      creatorCenter
    }), false, creatorCenter.message)
    let calls = 0
    const checked = await retryUnconfirmedLoginCheck(
      async () => {
        calls += 1
        return {
          baiying: { state: 'logged_in', message: '百应已登录' },
          creatorCenter
        }
      },
      isUnconfirmedLoginFalseNegative,
      { wait: async () => undefined }
    )
    assert.equal(checked.attempts, 1, creatorCenter.message)
    assert.equal(calls, 1, creatorCenter.message)
  }
  assert.equal(isUnconfirmedLoginFalseNegative({
    baiying: { state: 'logged_in', message: '百应已登录' },
    creatorCenter: { state: 'unknown', message: '页面已打开，但未识别到足以确认登录工作台的界面' }
  }), true)
})

test('两类中途错误最终失败结束；限流和验证码断连保留原待恢复语义', () => {
  assert.equal(getSelectionBatchFinalState([
    { state: 'failed', error: 'ACCOUNT_SWITCH_LOGIN：页面已开但未确认登录工作台' }
  ]), 'failed')
  assert.equal(getSelectionBatchFinalState([
    { state: 'failed', error: 'FILTER_APPLY_RETRIES_EXHAUSTED：CATEGORY_FILTER_APPLY_FAILED' }
  ]), 'failed')
  assert.equal(getSelectionBatchFinalState([
    { state: 'needs_retry', error: 'BAIYING_RATE_LIMIT：11001' }
  ]), 'needs_retry')
  assert.equal(getSelectionBatchFinalState([
    { state: 'failed', error: 'ACCOUNT_SWITCH_CAPTCHA：需要人工完成验证' }
  ]), 'needs_retry')
  assert.equal(getSelectionBatchFinalState([
    { state: 'completed' }, { state: 'completed' }
  ]), 'completed')
})

test('冷却归零后只为仍待恢复的限流批次排入自动续跑', () => {
  const queued = []
  let resumed = 0
  const eligible = {
    cooldownArmed: true,
    cooldownRemaining: 0,
    busy: false,
    batchState: 'needs_retry',
    rateLimitedAccountId: 1,
    pendingAccountIds: [1, 2]
  }

  assert.equal(scheduleAutoResumeSelection(eligible, () => { resumed += 1 }, (callback) => queued.push(callback)), true)
  assert.equal(resumed, 0, '恢复通过队列触发，避免与当前页面状态更新竞争')
  assert.equal(queued.length, 1)
  queued.shift()()
  assert.equal(resumed, 1)
  assert.equal(canAutoResumeSelection({ ...eligible, cooldownRemaining: 1 }), false)
  assert.equal(canAutoResumeSelection({ ...eligible, busy: true }), false)
  assert.equal(canAutoResumeSelection({ ...eligible, batchState: 'completed' }), false)
  assert.equal(canAutoResumeSelection({ ...eligible, rateLimitedAccountId: 3 }), false)
})

test('冷却意图在 busy 期间保留，busy 解除后自动续选且忽略过渡态标志', () => {
  const intent = {
    batchId: 'batch-cooldown', accountId: 1, deadline: 60_000,
    attempts: 0, phase: 'pending'
  }
  const busyDecision = evaluateSelectionCooldownResume({
    intent, batchId: 'batch-cooldown', pendingAccountIds: [1], busy: true, now: 60_000
  })
  assert.equal(busyDecision, 'waiting_for_busy_task')

  // startSelection 清掉 UI 冷却标志、批次/账号状态进入过渡态时，仍由同一有效意图续跑。
  const unblockedDecision = evaluateSelectionCooldownResume({
    intent, batchId: 'batch-cooldown', pendingAccountIds: [1], busy: false, now: 60_000,
    cooldownArmed: false, batchState: 'in_progress', rateLimitedAccountId: null
  })
  assert.equal(unblockedDecision, 'start')
  assert.match(settingsViewSource, /renderer_expiry_waiting_for_busy_task/)
  assert.match(settingsViewSource, /if \(decision === 'start'\) \{[\s\S]{0,160}scheduleCooldownAutoResume\(\)/)
  assert.match(settingsViewSource, /if \(queued\) acknowledgeSelectionCooldownEvent\(event, true\)/)
  assert.match(settingsViewSource, /selectionCooldownArmed,[\s\S]{0,80}busy,[\s\S]{0,80}cooldownAutoResumeIntent/)
  assert.match(settingsViewSource, /inFlightCheckpointStillResumable = Boolean\(matchingIntent && pendingTarget\)/)
  const resumeMarkerIndex = settingsViewSource.indexOf("code: resumingExistingBatch ? 'INFO_BATCH_RESUMED'")
  const intentConsumedIndex = settingsViewSource.indexOf('cooldownAutoResumeIntent.value = null', resumeMarkerIndex)
  assert.ok(resumeMarkerIndex >= 0 && intentConsumedIndex > resumeMarkerIndex,
    '自动恢复意图必须在 INFO_BATCH_RESUMED 已尝试落日志后才消费')
})

test('冷却恢复已启动时防重复，账号检查点失效时明确判过期', () => {
  const base = {
    batchId: 'batch-cooldown', pendingAccountIds: [1], busy: false, now: 60_000
  }
  assert.equal(evaluateSelectionCooldownResume({
    ...base,
    intent: { batchId: 'batch-cooldown', accountId: 1, deadline: 60_000, attempts: 0, phase: 'starting' }
  }), 'already_starting')
  assert.equal(evaluateSelectionCooldownResume({
    ...base,
    pendingAccountIds: [],
    intent: { batchId: 'batch-cooldown', accountId: 1, deadline: 60_000, attempts: 0, phase: 'pending' }
  }), 'stale_account')
  assert.match(settingsViewSource, /renderer_auto_resume_batch_resumed/)
  assert.match(settingsViewSource, /renderer_auto_resume_retry_scheduled/)
})

test('自动恢复遇设置变化时按当前保存设置直续，手动恢复仍走确认流程', () => {
  assert.equal(
    decideRecoverySettingsChange(true, true),
    'auto_continue_current_saved_settings'
  )
  assert.equal(decideRecoverySettingsChange(false, true), 'manual_confirmation')
  assert.equal(decideRecoverySettingsChange(true, false), 'unchanged')

  assert.match(settingsViewSource, /if \(isAutomaticRecovery\) \{[\s\S]{0,240}await loadGlobal\(\)/)
  assert.match(settingsViewSource, /traceSelectionCooldown\('settings_changed_auto_continue'/)
  assert.match(settingsViewSource, /if \(isAutomaticRecovery\) \{\s*scopeConfirmationToResume = \{\s*fingerprint: recoveryScopeFingerprint/)
  assert.match(settingsViewSource, /if \(!confirmSelectionScope\(recoveryScopeFingerprint/)
  assert.doesNotMatch(settingsViewSource, /stage:\s*'等待确认恢复设置'/)
})

test('恢复上下文差异忽略 Windows 路径大小写和分隔符漂移', () => {
  const previousContexts = {
    '1': {
      profileKey: 'account-1',
      profilePath: 'D:/Profiles/Account-1/',
      resolvedProfilePath: 'D:/Profiles/Account-1',
      downloadPath: 'D:/Downloads/Account-1/',
      settings: { selectionMode: 'general', filters: { commission: 5 } }
    }
  }
  const equivalentContexts = {
    '1': {
      profileKey: 'account-1',
      profilePath: 'd:\\profiles\\account-1',
      resolvedProfilePath: 'd:\\profiles\\account-1\\',
      downloadPath: 'd:\\downloads\\account-1',
      settings: { filters: { commission: 5 }, selectionMode: 'general' }
    }
  }
  const same = compareRecoverySettings(
    { selectionMode: 'general' }, { selectionMode: 'general' },
    previousContexts, equivalentContexts, [1]
  )
  assert.deepEqual(same, {
    changed: false,
    snapshotChanged: false,
    changedContextAccountIds: [],
    changedContextFields: []
  })

  const pathChanged = compareRecoverySettings(
    { selectionMode: 'general' }, { selectionMode: 'general' },
    previousContexts, { ...equivalentContexts, '1': { ...equivalentContexts['1'], downloadPath: 'd:\\other' } }, [1]
  )
  assert.equal(pathChanged.snapshotChanged, false)
  assert.deepEqual(pathChanged.changedContextAccountIds, [1])
  assert.deepEqual(pathChanged.changedContextFields, [{ accountId: 1, fields: ['downloadPath'] }])

  const changed = compareRecoverySettings(
    { selectionMode: 'general' }, { selectionMode: 'category' },
    previousContexts, { ...equivalentContexts, '1': { ...equivalentContexts['1'], downloadPath: 'd:\\other' } }, [1]
  )
  assert.equal(changed.changed, true)
  assert.equal(changed.snapshotChanged, true)
  assert.deepEqual(changed.changedContextAccountIds, [1])
})

const recoveryCategoryTree = [
  { id: '9', name: '美妆', children: [
    { id: '1000002395', name: '彩妆香水', children: [
      { id: '1000002395__RC_CASCADER_SPLIT__1000007909', name: '奢品彩妆', children: [] }
    ] }
  ] },
  { id: '13', name: '食品饮料', children: [
    { id: '1000000724', name: '休闲食品', children: [
      { id: '1000000724__RC_CASCADER_SPLIT__1000009094', name: '冰淇淋', children: [] },
      { id: '1000000724__RC_CASCADER_SPLIT__1000000728', name: '坚果炒货', children: [] }
    ] }
  ] },
  { id: '16', name: '鞋靴箱包', children: [
    { id: '1000000958', name: '箱包', children: [
      { id: '1000000958__RC_CASCADER_SPLIT__1000000963', name: '功能箱包', children: [] }
    ] }
  ] }
]

const accounts = [
  { id: 3, name: '账号C', status: 'active' },
  { id: 2, name: '账号B', status: 'active' },
  { id: 4, name: '新增账号D', status: 'active' },
  { id: 5, name: '已停用账号', status: 'disabled' },
  { id: 1, name: '账号A', status: 'active' }
]

test('批次按账号 ID 串行处理启用账号并跳过停用账号', async () => {
  const events = []
  const outcomes = await runSequentialSelectionBatch(
    accounts,
    async (account) => {
      events.push(`start:${account.id}`)
      await new Promise((resolve) => setTimeout(resolve, 5))
      events.push(`end:${account.id}`)
      return { status: '完成' }
    },
    (account, status) => events.push(`status:${account.id}:${status}`)
  )

  assert.deepEqual(outcomes.map((item) => item.accountId), [1, 2, 3, 4])
  assert.equal(events.includes('start:5'), false)
  assert.ok(events.indexOf('end:1') < events.indexOf('start:2'))
  assert.ok(events.indexOf('end:3') < events.indexOf('start:4'))
})

test('账号 A 遇 11001 保留失败原因，后续账号 B 仍立即执行', async () => {
  const batchAccounts = [accounts.find((account) => account.id === 1), accounts.find((account) => account.id === 2)]
  const started = []
  const statuses = []
  const checkpoints = new Map()
  const outcomes = await runSequentialSelectionBatch(
    batchAccounts,
    async (account) => {
      started.push(account.id)
      if (account.id === 1) {
        const reason = 'material_list 返回业务错误 11001；保留当前账号检查点'
        checkpoints.set(account.id, {
          state: getSelectionErrorRecoveryState(reason),
          stage: 'candidate_fetch',
          error: reason
        })
        throw new Error(reason)
      }
      return { status: '完成' }
    },
    (account, status) => statuses.push(`${account.id}:${status}`)
  )

  assert.deepEqual(started, [1, 2])
  assert.deepEqual(outcomes.map((item) => item.status), ['失败', '完成'])
  assert.match(outcomes[0].message, /11001/)
  assert.equal(getSelectionErrorRecoveryState(outcomes[0].message), 'needs_retry')
  assert.deepEqual(checkpoints.get(1), {
    state: 'needs_retry',
    stage: 'candidate_fetch',
    error: outcomes[0].message
  })
  assert.ok(statuses.includes('2:执行中'))
  assert.equal(outcomes.some((item) => item.status === '未执行'), false)
})

test('账号 A 遇验证码、登录或断连时保留失败原因，账号 B 仍立即执行', async () => {
  const cases = [
    ['ACCOUNT_SWITCH_CAPTCHA：百应要求手动完成滑块验证。', 'failed'],
    ['ACCOUNT_SWITCH_LOGIN：当前账号创作者中心尚未登录。', 'failed'],
    ['ACCOUNT_SWITCH_DISCONNECT：托管 Chrome 已关闭，CDP 连接失效。', 'failed']
  ]
  for (const [reason, checkpointState] of cases) {
    const started = []
    const checkpoints = new Map()
    const outcomes = await runSequentialSelectionBatch(
      [accounts.find((account) => account.id === 1), accounts.find((account) => account.id === 2)],
      async (account) => {
        started.push(account.id)
        if (account.id === 1) {
          checkpoints.set(account.id, { state: getSelectionErrorRecoveryState(reason), error: reason })
          throw new Error(reason)
        }
        return { status: '完成' }
      },
      () => undefined
    )

    assert.deepEqual(started, [1, 2], reason)
    assert.deepEqual(checkpoints.get(1), { state: checkpointState, error: reason })
    assert.deepEqual(outcomes.map((item) => item.status), ['失败', '完成'])
  }
})

test('冷却恢复先重试限流账号，再按原规则继续其他账号', async () => {
  const started = []
  const outcomes = await runSequentialSelectionBatch(
    [
      { id: 2, name: '账号B', status: 'active' },
      { id: 4, name: '账号D', status: 'active' },
      { id: 3, name: '账号C', status: 'active' }
    ],
    async (account) => {
      started.push(account.id)
      return { status: '完成' }
    },
    () => undefined,
    4
  )

  assert.deepEqual(started, [4, 2, 3])
  assert.deepEqual(outcomes.map((outcome) => outcome.accountId), [4, 2, 3])
})

test('账号 A 部分完成且未显式允许续批时，账号 B 仍执行并保留 A 的说明', async () => {
  const batchAccounts = [accounts.find((account) => account.id === 1), accounts.find((account) => account.id === 2)]
  const started = []
  const outcomes = await runSequentialSelectionBatch(
    batchAccounts,
    async (account) => {
      started.push(account.id)
      if (account.id === 1) return { status: '部分完成', message: '当前账号选品失败，已保存检查点' }
      return { status: '完成' }
    },
    () => undefined
  )

  assert.deepEqual(started, [1, 2])
  assert.deepEqual(outcomes.map((item) => item.status), ['部分完成', '完成'])
  assert.equal(outcomes[0].message, '当前账号选品失败，已保存检查点')
})

test('重复触发批次复用当前任务，不会再次调用执行函数', async () => {
  let calls = 0
  let finish
  const runOnce = createSingleFlight(() => {
    calls += 1
    return new Promise((resolve) => { finish = resolve })
  })

  const first = runOnce()
  const second = runOnce()
  assert.equal(first, second)
  assert.equal(calls, 1)
  finish('完成')
  assert.equal(await first, '完成')

  const third = runOnce()
  assert.equal(calls, 2)
  finish('第二批完成')
  assert.equal(await third, '第二批完成')
})

test('人工勾选只作用于预览所属账号，其他账号使用各自自动精选', () => {
  const candidates = [
    { productId: '商品A-1', selected: true },
    { productId: '商品A-2', selected: false }
  ]
  const reviewed = new Set(['商品A-2'])

  assert.deepEqual(
    [...selectCandidateIdsForAccount(1, 1, reviewed, candidates)],
    ['商品A-2']
  )
  assert.deepEqual(
    [...selectCandidateIdsForAccount(2, 1, reviewed, candidates)],
    ['商品A-1']
  )
})

test('账号资源路径大小写或分隔符相同会被识别为冲突', () => {
  const conflicts = findAccountResourcePathConflicts([
    { accountId: 1, accountName: '账号A', browserProfilePath: 'D:/profiles/a/', downloadPath: 'D:/downloads/a' },
    { accountId: 2, accountName: '账号B', browserProfilePath: 'd:\\profiles\\A', downloadPath: 'D:\\downloads\\b' },
    { accountId: 3, accountName: '账号C', browserProfilePath: 'D:\\profiles\\c', downloadPath: 'D:\\downloads\\a\\' }
  ])

  assert.equal(conflicts.get(1)?.length, 2)
  assert.equal(conflicts.get(2)?.length, 1)
  assert.equal(conflicts.get(3)?.length, 1)
})

test('加车失败检查点恢复时按原候选 ID 重试加车', () => {
  const checkpoint = [{ productId: '原商品', cartAdded: false }]
  assert.deepEqual(getSelectionRecoveryMode('quota_cart', true, checkpoint), {
    resumeAfterCart: false,
    resumeCart: true
  })
  assert.deepEqual(getSelectionRecoveryMode('video_pending', true, checkpoint), {
    resumeAfterCart: false,
    resumeCart: true
  })
})

test('已确认加车的检查点继续后续阶段且不重新加车', () => {
  const checkpoint = [{ productId: '原商品', cartAdded: true }]
  assert.deepEqual(getSelectionRecoveryMode('quota_cart', true, checkpoint), {
    resumeAfterCart: true,
    resumeCart: false
  })
  assert.deepEqual(getSelectionRecoveryMode('video_pending', true, checkpoint), {
    resumeAfterCart: true,
    resumeCart: false
  })
})

test('冲突的完整类目设置可按 cart_pending 已选商品来源收窄到唯一末级路径', () => {
  const savedSelection = {
    level1: ['9', '13', '16'],
    level2: ['1000000724'],
    level3: ['1000000724__RC_CASCADER_SPLIT__1000009094']
  }
  const originalPlan = buildSelectionCategoryPlan(savedSelection, recoveryCategoryTree)
  assert.ok(originalPlan.conflicts.length > 0)

  const result = resolveRecoveryCategorySelection([{
    cardIndex: 14,
    productId: '3794586512230842386',
    sourceCategorySelection: {
      level1: ['13'],
      level2: ['1000000724'],
      level3: ['1000000724__RC_CASCADER_SPLIT__1000009094']
    }
  }], [14], recoveryCategoryTree)

  assert.deepEqual(result.selection, {
    level1: ['13'],
    level2: ['1000000724'],
    level3: ['1000000724__RC_CASCADER_SPLIT__1000009094']
  })
  assert.equal(buildSelectionCategoryPlan(result.selection, recoveryCategoryTree).queryCount, 1)
  assert.deepEqual(savedSelection.level1, ['9', '13', '16'])
})

test('恢复多个候选时保留每个已选商品的有效来源末级类目', () => {
  const result = resolveRecoveryCategorySelection([
    {
      cardIndex: 14,
      productId: 'product-ice-cream',
      sourceCategorySelection: {
        level1: ['13'], level2: ['1000000724'],
        level3: ['1000000724__RC_CASCADER_SPLIT__1000009094']
      }
    },
    {
      cardIndex: 18,
      productId: 'product-nuts',
      sourceCategorySelection: {
        level1: ['13'], level2: ['1000000724'],
        level3: ['1000000724__RC_CASCADER_SPLIT__1000000728']
      }
    }
  ], [14, 18], recoveryCategoryTree)

  assert.deepEqual(result.selection?.level3, [
    '1000000724__RC_CASCADER_SPLIT__1000009094',
    '1000000724__RC_CASCADER_SPLIT__1000000728'
  ])
  assert.equal(buildSelectionCategoryPlan(result.selection, recoveryCategoryTree).queryCount, 2)
})

test('缺失、重复或与目录冲突的候选来源路径会阻止恢复查询', () => {
  const valid = {
    cardIndex: 14,
    productId: 'original-product',
    sourceCategorySelection: {
      level1: ['13'], level2: ['1000000724'],
      level3: ['1000000724__RC_CASCADER_SPLIT__1000009094']
    }
  }

  assert.match(resolveRecoveryCategorySelection([
    { cardIndex: 14, productId: 'original-product' }
  ], [14], recoveryCategoryTree).error, /缺少来源类目/)
  assert.match(resolveRecoveryCategorySelection([valid], [14, 15], recoveryCategoryTree).error, /找不到索引/)
  assert.match(resolveRecoveryCategorySelection([
    { ...valid, sourceCategorySelection: {
      level1: ['16'], level2: ['1000000724'],
      level3: ['1000000724__RC_CASCADER_SPLIT__1000009094']
    } }
  ], [14], recoveryCategoryTree).error, /无法映射到唯一查询路径/)
})
