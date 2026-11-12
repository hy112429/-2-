import { classifySelectionError } from './selection_error_ux'

export interface SelectionBatchAccount {
  id: number
  name: string
  status: string
}

export type SelectionAccountState = '排队中' | '执行中' | '完成' | '部分完成' | '失败' | '未执行'

export interface SelectionAccountResult {
  status: '完成' | '部分完成'
  message?: string
  /** 兼容现有调用方的单账号结果标记；账号间调度不依赖此字段停止。 */
  continueBatch?: boolean
}

export interface SelectionBatchOutcome {
  accountId: number
  accountName: string
  status: '完成' | '部分完成' | '失败' | '未执行'
  message?: string
}

export interface SelectionRecoveryAccountChoice {
  accountId: number
  state: string
}

export interface SelectionAutoResumeState {
  cooldownArmed: boolean
  cooldownRemaining: number
  busy: boolean
  batchState: string | null | undefined
  rateLimitedAccountId: number | null
  pendingAccountIds: readonly number[]
}

export interface SelectionCooldownResumeIntent {
  batchId: string
  accountId: number
  deadline: number
  retryAt?: number
  attempts: number
  phase: 'pending' | 'starting' | 'blocked'
}

export interface SelectionCooldownResumeEvaluation {
  intent: SelectionCooldownResumeIntent
  batchId: string | null | undefined
  pendingAccountIds: readonly number[]
  busy: boolean
  now: number
}

export type SelectionCooldownResumeDecision =
  | 'start'
  | 'waiting_for_deadline'
  | 'waiting_for_busy_task'
  | 'already_starting'
  | 'blocked'
  | 'stale_batch'
  | 'stale_account'

/**
 * 冷却恢复意图一旦从有效限流检查点建立，就独立于页面的 armed/busy 标志。
 * 这样 runSelectionBatch 将账号从 needs_retry 推进到 in_progress 时，不会把同一次恢复误判成无效。
 */
export function evaluateSelectionCooldownResume(
  state: SelectionCooldownResumeEvaluation
): SelectionCooldownResumeDecision {
  const { intent } = state
  if (!intent.batchId || state.batchId !== intent.batchId) return 'stale_batch'
  if (!state.pendingAccountIds.includes(intent.accountId)) return 'stale_account'
  if (intent.phase === 'blocked') return 'blocked'
  if (intent.phase === 'starting') return 'already_starting'
  const readyAt = Math.max(intent.deadline, intent.retryAt ?? 0)
  if (state.now < readyAt) return 'waiting_for_deadline'
  if (state.busy) return 'waiting_for_busy_task'
  return 'start'
}

export interface RecoverySettingsDiff {
  changed: boolean
  snapshotChanged: boolean
  changedContextAccountIds: number[]
  changedContextFields: Array<{ accountId: number; fields: string[] }>
}

export type RecoverySettingsChangeDecision =
  | 'unchanged'
  | 'auto_continue_current_saved_settings'
  | 'manual_confirmation'

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(stableJson).join(',') + ']'
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    return '{' + Object.keys(record).sort().map((key) => JSON.stringify(key) + ':' + stableJson(record[key])).join(',') + '}'
  }
  return JSON.stringify(value) ?? 'null'
}

function normalizedRecoveryPath(value: unknown): string {
  const normalized = String(value ?? '').trim().replace(/\//g, '\\')
  if (/^[a-z]:\\+$/i.test(normalized)) return normalized.slice(0, 2).toLowerCase() + '\\'
  return normalized.replace(/[\\]+$/, '').toLowerCase()
}

function recoveryContextSignature(value: unknown): unknown {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value ?? null
  const context = value as Record<string, unknown>
  return {
    profileKey: String(context.profileKey ?? ''),
    profilePath: normalizedRecoveryPath(context.profilePath),
    resolvedProfilePath: normalizedRecoveryPath(context.resolvedProfilePath),
    downloadPath: normalizedRecoveryPath(context.downloadPath),
    settings: context.settings ?? null
  }
}

/** 比较可刷新检查点的有效快照；路径按 Windows 语义规范化，排除重建时的大小写/分隔符差异。 */
export function compareRecoverySettings(
  previousSnapshot: unknown,
  currentSnapshot: unknown,
  previousContexts: Record<string, unknown>,
  currentContexts: Record<string, unknown>,
  pendingAccountIds: readonly number[]
): RecoverySettingsDiff {
  const snapshotChanged = stableJson(previousSnapshot) !== stableJson(currentSnapshot)
  const contextFields = ['profileKey', 'profilePath', 'resolvedProfilePath', 'downloadPath', 'settings'] as const
  const changedContextFields = pendingAccountIds.flatMap((accountId) => {
    const previous = recoveryContextSignature(previousContexts[String(accountId)]) as Record<string, unknown> | null
    const current = recoveryContextSignature(currentContexts[String(accountId)]) as Record<string, unknown> | null
    const fields = contextFields.filter((field) =>
      stableJson(previous?.[field] ?? null) !== stableJson(current?.[field] ?? null)
    )
    return fields.length ? [{ accountId, fields: [...fields] }] : []
  })
  const changedContextAccountIds = changedContextFields.map((item) => item.accountId)
  return {
    changed: snapshotChanged || changedContextAccountIds.length > 0,
    snapshotChanged,
    changedContextAccountIds,
    changedContextFields
  }
}

/** 自动冷却恢复采用数据库当前保存设置；手动恢复保留原确认流程。 */
export function decideRecoverySettingsChange(
  isAutomaticRecovery: boolean,
  settingsChanged: boolean
): RecoverySettingsChangeDecision {
  if (!settingsChanged) return 'unchanged'
  return isAutomaticRecovery ? 'auto_continue_current_saved_settings' : 'manual_confirmation'
}

export interface SelectionBatchFinalAccount {
  state: string
  error?: string
}

/**
 * 登录确认假阴性、搜索框恢复耗尽和筛选应用重试耗尽属于本轮明确失败；限流保留 needs_retry，
 * 其他中断仍沿用既有恢复语义。
 */
export function getSelectionBatchFinalState(
  accounts: readonly SelectionBatchFinalAccount[]
): 'completed' | 'needs_retry' | 'failed' {
  if (accounts.every((account) => account.state === 'completed')) return 'completed'
  if (accounts.some((account) => account.state === 'needs_retry')) return 'needs_retry'
  const unfinished = accounts.filter((account) => account.state !== 'completed')
  const terminalFailureClasses = new Set([
    'ACCOUNT_SWITCH_LOGIN',
    'LOGIN_OR_PAGE_STATE_UNCONFIRMED',
    'SEARCHBOX_ZERO_EXHAUSTED',
    'SEARCHBOX_CONTROL_EXHAUSTED',
    'CATEGORY_FILTER_APPLY_FAILED'
  ])
  if (unfinished.length > 0 && unfinished.every((account) =>
    account.state === 'failed' && terminalFailureClasses.has(classifySelectionError(account.error) ?? '')
  )) return 'failed'
  return 'needs_retry'
}

/** 对明确的“页面已打开但工作台未识别”只做一次短时复查，不改变登录/验证码闸门。 */
export async function retryUnconfirmedLoginCheck<T>(
  check: () => Promise<T>,
  shouldRetry: (value: T) => boolean,
  options: {
    wait?: (milliseconds: number) => Promise<void>
    onRetry?: () => void
  } = {}
): Promise<{ value: T; attempts: number }> {
  let value = await check()
  if (!shouldRetry(value)) return { value, attempts: 1 }
  options.onRetry?.()
  await (options.wait ?? ((milliseconds) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds))))(500)
  value = await check()
  return { value, attempts: 2 }
}

/** 只将百应/创作者页面已打开但工作台未被识别的 unknown 视作短暂假阴性。 */
export function isUnconfirmedLoginFalseNegative(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  const result = value as Record<string, unknown>
  const platforms = [result.baiying, result.creatorCenter]
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
  if (!platforms.length || platforms.some((item) => item.state !== 'logged_in' && item.state !== 'unknown')) {
    return false
  }
  const unknowns = platforms.filter((item) => item.state === 'unknown')
  if (!unknowns.length) return false
  return unknowns.some((item) => {
    const message = String(item.message ?? '')
    const classification = classifySelectionError(message)
    if (classification === 'ACCOUNT_SWITCH_CAPTCHA' || classification === 'ACCOUNT_SWITCH_DISCONNECT' ||
      classification === 'BAIYING_RATE_LIMIT') return false
    return /页面已?(?:打开|开).{0,50}(?:未识别到|未确认|无法确认).{0,50}(?:登录|工作台)|未识别到足以确认登录.{0,30}工作台/i.test(message)
  })
}

/** 限流冷却结束后，仅允许从仍含限流检查点的 needs_retry 批次自动恢复。 */
export function canAutoResumeSelection(state: SelectionAutoResumeState): boolean {
  return state.cooldownArmed && state.cooldownRemaining <= 0 && !state.busy &&
    state.batchState === 'needs_retry' && state.rateLimitedAccountId !== null &&
    state.pendingAccountIds.includes(state.rateLimitedAccountId)
}

/** 可注入队列调度器的自动恢复入口，方便验证冷却归零后是否触发。 */
export function scheduleAutoResumeSelection(
  state: SelectionAutoResumeState,
  resume: () => void,
  enqueue: (callback: () => void) => unknown = (callback) => { void Promise.resolve().then(callback) }
): boolean {
  if (!canAutoResumeSelection(state)) return false
  enqueue(resume)
  return true
}

/** 恢复时只返回用户选中的未完成账号；未选账号保持原检查点。 */
export function filterPendingRecoveryAccounts<TAccount extends SelectionRecoveryAccountChoice>(
  accounts: readonly TAccount[],
  selectedAccountIds: ReadonlySet<number>
): TAccount[] {
  return accounts.filter((account) => account.state !== 'completed' && selectedAccountIds.has(account.accountId))
}

export interface SelectionCandidateChoice {
  productId: string
  selected: boolean
}

export interface AccountResourcePath {
  accountId: number
  accountName: string
  browserProfilePath: string
  downloadPath: string
}

export interface SelectionCategoryNode {
  id: string
  name?: string
  children?: readonly SelectionCategoryNode[]
}

export interface SelectionCategoryPlan {
  queryCount: number
  paths: string[]
  conflicts: string[]
  catalogLoaded: boolean
}

export interface SelectionCategorySelection {
  level1: string[]
  level2: string[]
  level3: string[]
}

const CATEGORY_PATH_SEPARATOR = '__RC_CASCADER_SPLIT__'

/** 本地预估与百应类目拆分一致的末级查询路径，并标出隐式扩大选择范围。 */
export function buildSelectionCategoryPlan(
  selection: { level1?: readonly string[]; level2?: readonly string[]; level3?: readonly string[] },
  tree: readonly SelectionCategoryNode[]
): SelectionCategoryPlan {
  const selectedRoots = new Set((selection.level1 ?? []).map(String).filter(Boolean))
  const selectedSeconds = new Set((selection.level2 ?? []).map(String).filter(Boolean))
  const selectedThirds = new Set((selection.level3 ?? []).map(String).filter(Boolean))
  if (!tree.length) {
    return { queryCount: 1, paths: [], conflicts: [], catalogLoaded: false }
  }
  if (!selectedRoots.size) {
    return { queryCount: 1, paths: ['未指定类目（使用百应当前类目条件）'], conflicts: [], catalogLoaded: true }
  }

  const paths = new Map<string, string>()
  const conflicts: string[] = []
  const matchedRoots = new Set<string>()
  const matchedSeconds = new Set<string>()
  const matchedThirds = new Set<string>()
  const matches = (nodeId: string, selected: ReadonlySet<string>): boolean => {
    const tailParts = nodeId.split(CATEGORY_PATH_SEPARATOR)
    const tail = tailParts[tailParts.length - 1] ?? nodeId
    return [...selected].some((value) => value === nodeId ||
      (value.split(CATEGORY_PATH_SEPARATOR).slice(-1)[0] ?? value) === tail)
  }
  const label = (node: SelectionCategoryNode): string => `${node.name || node.id}（${node.id}）`
  const addPath = (
    root: SelectionCategoryNode,
    second: SelectionCategoryNode,
    third?: SelectionCategoryNode
  ): void => {
    const key = [root.id, second.id, third?.id ?? ''].join('\u0000')
    paths.set(key, [label(root), label(second), ...(third ? [label(third)] : [])].join(' / '))
  }

  for (const root of tree) {
    if (!selectedRoots.has(root.id)) continue
    matchedRoots.add(root.id)
    const seconds = (root.children ?? []).filter((node) => Boolean(node.id))
    if (!seconds.length) conflicts.push(`一级类目 ${label(root)} 在本机目录中没有可查询的下级类目`)
    const explicitSeconds = seconds.filter((node) => matches(node.id, selectedSeconds))
    explicitSeconds.forEach((node) => matchedSeconds.add(node.id))
    if (selectedSeconds.size && !explicitSeconds.length) {
      conflicts.push(`一级类目 ${label(root)} 没有匹配已选的二级/三级路径`)
    }
    const targetSeconds = explicitSeconds.length ? explicitSeconds : seconds

    for (const second of targetSeconds) {
      const thirds = (second.children ?? []).filter((node) => Boolean(node.id))
      const explicitThirds = thirds.filter((node) => matches(node.id, selectedThirds))
      explicitThirds.forEach((node) => matchedThirds.add(node.id))
      if (selectedThirds.size && thirds.length && !explicitThirds.length) {
        conflicts.push(`二级类目 ${label(second)} 没有匹配已选的三级路径`)
      }
      const targetThirds = explicitThirds.length ? explicitThirds : thirds
      if (targetThirds.length) targetThirds.forEach((third) => addPath(root, second, third))
      else addPath(root, second)
    }
  }

  const missingRoots = [...selectedRoots].filter((id) => !matchedRoots.has(id))
  if (missingRoots.length) conflicts.push(`已选一级类目未匹配本机目录：${missingRoots.join('、')}`)
  const missingSeconds = [...selectedSeconds].filter((id) => !matchesAnyNode(id, matchedSeconds))
  if (missingSeconds.length) conflicts.push(`已选二级类目未匹配已选一级类目：${missingSeconds.join('、')}`)
  const missingThirds = [...selectedThirds].filter((id) => !matchesAnyNode(id, matchedThirds))
  if (missingThirds.length) conflicts.push(`已选三级类目未匹配已选二级类目：${missingThirds.join('、')}`)

  const plannedPaths = [...paths.values()]
  return {
    queryCount: plannedPaths.length,
    paths: plannedPaths,
    conflicts: [...new Set(conflicts)],
    catalogLoaded: true
  }

  function matchesAnyNode(selectedId: string, nodeIds: ReadonlySet<string>): boolean {
    const selectedTail = selectedId.split(CATEGORY_PATH_SEPARATOR).slice(-1)[0] ?? selectedId
    return [...nodeIds].some((nodeId) => nodeId === selectedId ||
      (nodeId.split(CATEGORY_PATH_SEPARATOR).slice(-1)[0] ?? nodeId) === selectedTail)
  }
}

/** 从检查点已选商品的来源路径构造严格的恢复筛选范围，不继承未选中的类目。 */
export function resolveRecoveryCategorySelection(
  candidates: readonly {
    cardIndex: number
    productId?: string
    sourceCategorySelection?: unknown
  }[],
  selectedIndexes: readonly number[],
  tree: readonly SelectionCategoryNode[]
): { selection?: SelectionCategorySelection; error?: string } {
  if (!tree.length) return { error: '本机类目目录不可用，无法验证恢复商品的来源路径。' }
  if (!selectedIndexes.length || selectedIndexes.some((index) => !Number.isInteger(index)) ||
    new Set(selectedIndexes).size !== selectedIndexes.length) {
    return { error: '恢复检查点没有有效且唯一的已选商品索引。' }
  }

  const byIndex = new Map<number, typeof candidates[number]>()
  for (const candidate of candidates) {
    if (!selectedIndexes.includes(candidate.cardIndex)) continue
    if (byIndex.has(candidate.cardIndex)) {
      return { error: `恢复检查点商品索引 ${candidate.cardIndex} 重复，无法安全定位原商品。` }
    }
    byIndex.set(candidate.cardIndex, candidate)
  }

  const sourcePaths = new Map<string, SelectionCategorySelection>()
  const selection: SelectionCategorySelection = { level1: [], level2: [], level3: [] }
  const addId = (level: keyof SelectionCategorySelection, value: string): void => {
    const tail = value.split(CATEGORY_PATH_SEPARATOR).at(-1) ?? value
    const existingIndex = selection[level].findIndex((item) =>
      (item.split(CATEGORY_PATH_SEPARATOR).at(-1) ?? item) === tail
    )
    if (existingIndex < 0) selection[level].push(value)
    else if (value.length > selection[level][existingIndex].length) selection[level][existingIndex] = value
  }

  for (const index of selectedIndexes) {
    const candidate = byIndex.get(index)
    if (!candidate || !String(candidate.productId ?? '').trim()) {
      return { error: `恢复检查点找不到索引 ${index} 对应的原商品 ID。` }
    }
    const raw = candidate.sourceCategorySelection
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      return { error: `原商品 ${candidate.productId} 缺少来源类目路径，已停止恢复。` }
    }
    const record = raw as Record<string, unknown>
    const source = {} as SelectionCategorySelection
    for (const level of ['level1', 'level2', 'level3'] as const) {
      const values = record[level]
      const unique = Array.isArray(values)
        ? [...new Set(values.map((value) => String(value ?? '').trim()).filter(Boolean))]
        : []
      if (unique.length !== 1) {
        return { error: `原商品 ${candidate.productId} 的来源类目不唯一，已停止恢复。` }
      }
      source[level] = unique
    }

    const sourcePlan = buildSelectionCategoryPlan(source, tree)
    if (!sourcePlan.catalogLoaded || sourcePlan.conflicts.length || sourcePlan.queryCount !== 1) {
      return { error: `原商品 ${candidate.productId} 的来源类目无法映射到唯一查询路径，已停止恢复。` }
    }
    const key = (['level1', 'level2', 'level3'] as const)
      .map((level) => source[level][0].split(CATEGORY_PATH_SEPARATOR).at(-1) ?? source[level][0])
      .join('\u0000')
    sourcePaths.set(key, source)
    for (const level of ['level1', 'level2', 'level3'] as const) addId(level, source[level][0])
  }

  const mergedPlan = buildSelectionCategoryPlan(selection, tree)
  if (!mergedPlan.catalogLoaded || mergedPlan.conflicts.length || mergedPlan.queryCount !== sourcePaths.size) {
    return { error: '合并后的恢复商品来源类目无法形成精确且无冲突的查询计划，已停止恢复。' }
  }
  return { selection }
}

/** 根据持久化检查点判断应重试加车，还是从已加车阶段继续。 */
export function getSelectionRecoveryMode(
  stage: string,
  hasCandidateCheckpoint: boolean,
  completed: readonly Record<string, unknown>[]
): { resumeAfterCart: boolean; resumeCart: boolean } {
  const hasCartFailure = completed.some((item) => item.cartAdded !== true && item.skipped !== true)
  const isAfterCartStage = stage === 'quota_cart' || stage === 'video_pending'
  return {
    resumeAfterCart: isAfterCartStage && completed.length > 0 && !hasCartFailure,
    resumeCart: hasCandidateCheckpoint && (
      stage === 'cart_pending' || (isAfterCartStage && hasCartFailure)
    )
  }
}

/** 百应限流和登录失效保留检查点；验证码、断连等人工闸门仍停止本账号。 */
export function getSelectionErrorRecoveryState(reason: string): 'needs_retry' | 'failed' {
  return ['BAIYING_RATE_LIMIT', 'ACCOUNT_SWITCH_LOGIN'].includes(classifySelectionError(reason) ?? '')
    ? 'needs_retry'
    : 'failed'
}

/** 为批次拒绝重复的 Chrome profile 或素材输出目录，避免账号登录态和文件相互覆盖。 */
export function findAccountResourcePathConflicts(
  resources: readonly AccountResourcePath[]
): Map<number, string[]> {
  const conflicts = new Map<number, Set<string>>()
  const normalize = (value: string): string =>
    value.trim().replace(/\//g, '\\').replace(/[\\/]+$/, '').toLowerCase()

  const check = (field: 'browserProfilePath' | 'downloadPath', label: string): void => {
    const owners = new Map<string, AccountResourcePath[]>()
    for (const resource of resources) {
      const path = normalize(resource[field])
      if (!path) continue
      const rows = owners.get(path) ?? []
      rows.push(resource)
      owners.set(path, rows)
    }
    for (const rows of owners.values()) {
      if (rows.length < 2) continue
      const names = rows.map((row) => row.accountName).join('、')
      for (const row of rows) {
        const messages = conflicts.get(row.accountId) ?? new Set<string>()
        messages.add(label + '与账号「' + names + '」重复')
        conflicts.set(row.accountId, messages)
      }
    }
  }

  check('browserProfilePath', '浏览器缓存路径')
  check('downloadPath', '账号下载路径')
  return new Map([...conflicts].map(([id, messages]) => [id, [...messages]]))
}

/** 人工审阅仅能作用于产生该预览的账号；其他账号始终使用自己的自动精选结果。 */
export function selectCandidateIdsForAccount<TCandidate extends SelectionCandidateChoice>(
  targetAccountId: number,
  previewAccountId: number | null,
  reviewedProductIds: ReadonlySet<string> | undefined,
  candidates: readonly TCandidate[]
): ReadonlySet<string> {
  if (previewAccountId === targetAccountId && reviewedProductIds !== undefined) {
    return new Set(reviewedProductIds)
  }
  return new Set(candidates.filter((candidate) => candidate.selected).map((candidate) => candidate.productId))
}

/**
 * 按账号 ID 顺序串行执行启用账号；单账号失败保留在该账号结果，不阻断后续账号。
 */
export async function runSequentialSelectionBatch<TAccount extends SelectionBatchAccount>(
  accounts: readonly TAccount[],
  runAccount: (account: TAccount) => Promise<SelectionAccountResult | void>,
  onStatus: (account: TAccount, status: SelectionAccountState) => void,
  priorityAccountId?: number | null,
  stopAfterIncomplete = false
): Promise<SelectionBatchOutcome[]> {
  const ordered = [...accounts]
    .filter((account) => account.status === 'active')
    .sort((left, right) => {
      if (priorityAccountId !== undefined && priorityAccountId !== null) {
        const leftPriority = left.id === priorityAccountId ? 0 : 1
        const rightPriority = right.id === priorityAccountId ? 0 : 1
        if (leftPriority !== rightPriority) return leftPriority - rightPriority
      }
      return left.id - right.id
    })

  for (const account of ordered) onStatus(account, '排队中')

  const outcomes: SelectionBatchOutcome[] = []
  for (const [index, account] of ordered.entries()) {
    onStatus(account, '执行中')
    try {
      const result = await runAccount(account)
      const status = result?.status ?? '完成'
      onStatus(account, status)
      outcomes.push({
        accountId: account.id,
        accountName: account.name,
        status,
        message: result?.message
      })
      if (stopAfterIncomplete && status !== '完成') {
        for (const remaining of ordered.slice(index + 1)) {
          onStatus(remaining, '未执行')
          outcomes.push({
            accountId: remaining.id,
            accountName: remaining.name,
            status: '未执行',
            message: `前序账号「${account.name}」尚未达到选品目标，已保留本账号检查点。`
          })
        }
        break
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      onStatus(account, '失败')
      outcomes.push({
        accountId: account.id,
        accountName: account.name,
        status: '失败',
        message
      })
      if (stopAfterIncomplete) {
        for (const remaining of ordered.slice(index + 1)) {
          onStatus(remaining, '未执行')
          outcomes.push({
            accountId: remaining.id,
            accountName: remaining.name,
            status: '未执行',
            message: `前序账号「${account.name}」执行失败，已保留本账号检查点。`
          })
        }
        break
      }
    }
  }
  return outcomes
}

/** 同一页面的长任务只允许一个实例运行；重复触发会复用当前 Promise。 */
export function createSingleFlight<T>(operation: () => Promise<T>): () => Promise<T> {
  let inFlight: Promise<T> | null = null
  return () => {
    if (inFlight) return inFlight
    const current = operation()
    inFlight = current
    const clear = (): void => {
      if (inFlight === current) inFlight = null
    }
    void current.then(clear, clear)
    return current
  }
}
