import { ipcMain } from 'electron'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'
import { getOrCreateCenterMachineId } from './center_client_bootstrap'

export interface RegistryCandidate {
  productId: string
  leafCategoryId: string
  title?: string
}

export interface RegistryReservation {
  productId: string
  token: string
  state: string
}

interface RegistryReserveResponse {
  reserved?: RegistryReservation[]
  rejected?: Array<{ productId: string; reason: string }>
}

export interface RegistrySnapshot {
  usedProductIds: string[]
  accountUsedCategoryIds: string[]
  categoryUsage: Record<string, number>
  mode: 'local' | 'server'
  syncConflicts?: Array<{ productId: string; reason: string }>
  warning?: string
}

interface RegistryPayload {
  ok: boolean
  error?: string
  [key: string]: unknown
}

class RegistryUnavailableError extends Error {}
class RegistryAmbiguousResponseError extends Error {}

function normalizeLocalAccountKey(value: string): string {
  const key = String(value ?? '').trim()
  const match = /^local:([1-9]\d*)(?:\|douyin:([A-Za-z0-9._-]{1,96}))?$/.exec(key)
  if (!match) {
    throw new Error('本机选品登记缺少有效的客户端账号 ID。')
  }
  return `local:${match[1]}`
}

function serverConfig(): { base: string; token: string } | null {
  const raw = process.env['TOUGUANG_REGISTRY_URL']?.trim() ?? ''
  const token = process.env['TOUGUANG_REGISTRY_TOKEN']?.trim() ?? ''
  if (!raw && !token) return null
  if (!raw || !token) throw new Error('共享选品登记地址或密钥未配置完整；已停止选品。')
  let url: URL
  try { url = new URL(raw) }
  catch { throw new Error('共享选品登记地址无效；已停止选品。') }
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname))) ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('共享选品登记必须使用 HTTPS 地址；本机隔离服务可使用 127.0.0.1。')
  }
  return { base: url.href.replace(/\/$/, ''), token }
}

function remoteAccountKey(localKey: string): string {
  normalizeLocalAccountKey(localKey)
  const douyinId = /^local:[1-9]\d*\|douyin:([A-Za-z0-9._-]{1,96})$/.exec(localKey)?.[1]
  return douyinId || `${getOrCreateCenterMachineId()}:${normalizeLocalAccountKey(localKey)}`
}

async function runServerRegistry<T>(path: string, body?: unknown): Promise<T> {
  const config = serverConfig()
  if (!config) throw new Error('共享选品登记未配置。')
  let response: Response | undefined
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      response = await fetch(`${config.base}/api/v1/registry${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: { Authorization: `Bearer ${config.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(10000)
      })
      break
    } catch {
      if (attempt === 1) throw new RegistryUnavailableError('共享选品登记服务暂时不可达；已切换本机选品登记，联网恢复后同步。')
    }
  }
  if (!response) throw new Error('共享选品登记未返回结果；已保留检查点。')
  if (response.status >= 500) throw new RegistryUnavailableError(`共享选品登记服务暂时不可用（HTTP ${response.status}）；本次未继续提交。`)
  if (!response.ok) throw new Error(`共享选品登记返回 HTTP ${response.status}；已保留检查点。`)
  try { return await response.json() as T }
  catch { throw new RegistryAmbiguousResponseError('共享选品登记响应无法解析；需要回读服务器状态后恢复。') }
}

async function getServerSnapshot(accountKey: string, candidates: RegistryCandidate[]): Promise<RegistrySnapshot> {
  const used = new Set<string>()
  const categories = new Set<string>()
  const usage: Record<string, number> = {}
  const chunks = candidates.length ? Array.from({ length: Math.ceil(candidates.length / 500) }, (_, index) => candidates.slice(index * 500, (index + 1) * 500)) : [[]]
  for (const chunk of chunks) {
    const snapshot = await runServerRegistry<Partial<RegistrySnapshot>>('/snapshot', {
      accountKey: remoteAccountKey(accountKey),
      productIds: chunk.map((item) => item.productId),
      leafCategoryIds: chunk.map((item) => item.leafCategoryId).filter(Boolean)
    })
    if (!Array.isArray(snapshot.usedProductIds) || !Array.isArray(snapshot.accountUsedCategoryIds) ||
        !snapshot.categoryUsage || typeof snapshot.categoryUsage !== 'object') {
      throw new Error('共享选品登记快照不完整；已保留检查点。')
    }
    snapshot.usedProductIds.forEach((id) => used.add(String(id)))
    snapshot.accountUsedCategoryIds.forEach((id) => categories.add(String(id)))
    Object.assign(usage, snapshot.categoryUsage)
  }
  return { usedProductIds: [...used], accountUsedCategoryIds: [...categories], categoryUsage: usage, mode: 'server' }
}

async function runLocalRegistry<T extends RegistryPayload>(args: string[], stdinJson?: unknown): Promise<T> {
  const outcome = await runEngineCommand('selection_registry_local.py', [
    '--db', getClientPaths().databasePath,
    ...args
  ], stdinJson)
  if (!outcome.ok) {
    throw new Error(outcome.error || '本机选品登记失败；本次未执行加车。')
  }
  if (!outcome.result || typeof outcome.result !== 'object' || Array.isArray(outcome.result)) {
    throw new Error('本机选品登记返回内容无效；本次未执行加车。')
  }
  const result = outcome.result as T
  if (result.ok !== true) {
    throw new Error(typeof result.error === 'string' ? result.error : '本机选品登记失败；本次未执行加车。')
  }
  return result
}

async function syncLocalRegistryToServer(accountKey: string): Promise<Array<{ productId: string; reason: string }>> {
  const localAccountKey = normalizeLocalAccountKey(accountKey)
  const exported = await runLocalRegistry<RegistryPayload & { items?: Array<Record<string, string>> }>([
    'sync-export', '--limit', '5000', '--account-key', localAccountKey
  ])
  const items = Array.isArray(exported.items) ? exported.items : []
  const conflicts: Array<{ productId: string; reason: string }> = []
  for (let offset = 0; offset < items.length; offset += 50) {
    const chunk = items.slice(offset, offset + 50)
    const response = await runServerRegistry<{ outcomes?: Array<{ localToken: string; productId: string; status: string; reason?: string }> }>(
      '/offline-sync', {
        items: chunk.map((item) => ({
          productId: String(item.productId ?? ''),
          localToken: String(item.localToken ?? ''),
          accountKey: remoteAccountKey(accountKey),
          leafCategoryId: String(item.leafCategoryId ?? ''),
          title: String(item.title ?? ''),
          state: String(item.state ?? '')
        }))
      }
    )
    if (!Array.isArray(response.outcomes) || response.outcomes.length !== chunk.length) throw new Error('共享选品离线同步回执数量不完整；本机记录保持待同步。')
    const outcomes = response.outcomes.map((outcome) => {
      if (outcome.status === 'conflict') conflicts.push({ productId: String(outcome.productId), reason: String(outcome.reason ?? 'SERVER_CONFLICT') })
      return { localToken: String(outcome.localToken ?? ''), status: String(outcome.status ?? ''), reason: String(outcome.reason ?? '') }
    })
    const applied = await runLocalRegistry<RegistryPayload & { updated?: number }>(['sync-apply'], { outcomes })
    if (Number(applied.updated) !== outcomes.length) throw new Error('本机离线同步回执未全部落账；未继续消费候选。')
  }
  return conflicts
}

async function getLocalSnapshot(accountKey: string, candidates: RegistryCandidate[]): Promise<RegistrySnapshot> {
  const localAccountKey = normalizeLocalAccountKey(accountKey)
  const result = await runLocalRegistry<RegistryPayload & RegistrySnapshot>([
    'snapshot', '--account-key', localAccountKey
  ], candidates)
  if (!Array.isArray(result.usedProductIds) || !Array.isArray(result.accountUsedCategoryIds) ||
      !result.categoryUsage || typeof result.categoryUsage !== 'object' || Array.isArray(result.categoryUsage)) {
    throw new Error('本机选品查重结果不完整；本次已停止，未执行加车。')
  }
  return {
    usedProductIds: result.usedProductIds.map(String),
    accountUsedCategoryIds: result.accountUsedCategoryIds.map(String),
    categoryUsage: result.categoryUsage as Record<string, number>,
    mode: 'local'
  }
}

export async function getRegistrySnapshot(accountKey: string, candidates: RegistryCandidate[]): Promise<RegistrySnapshot> {
  if (!serverConfig()) return getLocalSnapshot(accountKey, candidates)
  try {
    const syncConflicts = await syncLocalRegistryToServer(accountKey)
    const [server, local] = await Promise.all([
      getServerSnapshot(accountKey, candidates),
      getLocalSnapshot(accountKey, candidates)
    ])
    return {
      usedProductIds: [...new Set([...server.usedProductIds, ...local.usedProductIds])],
      accountUsedCategoryIds: [...new Set([...server.accountUsedCategoryIds, ...local.accountUsedCategoryIds])],
      categoryUsage: Object.fromEntries([...new Set([...Object.keys(server.categoryUsage), ...Object.keys(local.categoryUsage)])]
        .map((categoryId) => [categoryId, Math.max(server.categoryUsage[categoryId] ?? 0, local.categoryUsage[categoryId] ?? 0)] as const)),
      mode: 'server',
      ...(syncConflicts.length ? { syncConflicts } : {})
    }
  } catch (cause) {
    if (!(cause instanceof RegistryUnavailableError)) throw cause
    const local = await getLocalSnapshot(accountKey, candidates)
    return { ...local, warning: cause.message }
  }
}

async function getLocalBatchSnapshots(
  entries: Array<{ accountKey: string; candidates: RegistryCandidate[] }>
): Promise<Record<string, RegistrySnapshot>> {
  const normalized = entries.map((entry) => ({
    originalKey: entry.accountKey,
    accountKey: normalizeLocalAccountKey(entry.accountKey),
    candidates: Array.isArray(entry.candidates) ? entry.candidates.map((candidate) => ({
      productId: String(candidate.productId ?? ''),
      leafCategoryId: String(candidate.leafCategoryId ?? '')
    })) : []
  }))
  if (!normalized.length) return {}
  const result = await runLocalRegistry<RegistryPayload & { snapshots?: Record<string, Partial<RegistrySnapshot>> }>(
    ['snapshot-batch'], { entries: normalized.map(({ accountKey, candidates }) => ({ accountKey, candidates })) }
  )
  const rawSnapshots = result.snapshots
  if (!rawSnapshots || typeof rawSnapshots !== 'object' || Array.isArray(rawSnapshots)) {
    throw new Error('本机批量选品查重结果不完整；本次已停止，未执行加车。')
  }
  return Object.fromEntries(normalized.map(({ accountKey, originalKey }) => {
    const snapshot = rawSnapshots[accountKey]
    if (!snapshot || !Array.isArray(snapshot.usedProductIds) || !Array.isArray(snapshot.accountUsedCategoryIds) ||
        !snapshot.categoryUsage || typeof snapshot.categoryUsage !== 'object' || Array.isArray(snapshot.categoryUsage)) {
      throw new Error(`账号 ${accountKey} 的本机选品查重结果不完整；本次已停止，未执行加车。`)
    }
    return [originalKey, {
      usedProductIds: Array.isArray(snapshot.usedProductIds) ? snapshot.usedProductIds.map(String) : [],
      accountUsedCategoryIds: Array.isArray(snapshot.accountUsedCategoryIds)
        ? snapshot.accountUsedCategoryIds.map(String) : [],
      categoryUsage: snapshot.categoryUsage && typeof snapshot.categoryUsage === 'object'
        ? snapshot.categoryUsage as Record<string, number> : {},
      mode: 'local' as const
    }]
  }))
}

export async function getRegistryBatchSnapshots(
  entries: Array<{ accountKey: string; candidates: RegistryCandidate[] }>
): Promise<Record<string, RegistrySnapshot>> {
  const local = await getLocalBatchSnapshots(entries)
  if (!serverConfig()) return local
  try {
    const conflictsByAccount: Record<string, Array<{ productId: string; reason: string }>> = {}
    const snapshots: Record<string, RegistrySnapshot> = {}
    for (const entry of entries) {
      conflictsByAccount[entry.accountKey] = await syncLocalRegistryToServer(entry.accountKey)
      snapshots[entry.accountKey] = await getServerSnapshot(entry.accountKey, entry.candidates)
    }
    return Object.fromEntries(entries.map((entry) => {
      const server = snapshots[entry.accountKey]
      const offline = local[entry.accountKey]
      if (!server || !offline) throw new Error(`账号 ${entry.accountKey} 的共享登记快照不完整。`)
      return [entry.accountKey, {
        usedProductIds: [...new Set([
          ...server.usedProductIds,
          ...offline.usedProductIds,
          ...(conflictsByAccount[entry.accountKey] ?? []).map((item) => String(item.productId))
        ])],
        accountUsedCategoryIds: [...new Set([...server.accountUsedCategoryIds, ...offline.accountUsedCategoryIds])],
        categoryUsage: Object.fromEntries([...new Set([...Object.keys(server.categoryUsage), ...Object.keys(offline.categoryUsage)])]
          .map((categoryId) => [categoryId, Math.max(server.categoryUsage[categoryId] ?? 0, offline.categoryUsage[categoryId] ?? 0)] as const)),
        mode: 'server' as const,
        ...(conflictsByAccount[entry.accountKey]?.length ? { syncConflicts: conflictsByAccount[entry.accountKey] } : {})
      }]
    }))
  } catch (cause) {
    if (!(cause instanceof RegistryUnavailableError)) throw cause
    return Object.fromEntries(Object.entries(local).map(([key, snapshot]) => [key, { ...snapshot, warning: cause.message }]))
  }
}

export async function reserveRegistryCandidates(
  accountKey: string,
  requestId: string,
  items: RegistryCandidate[],
  maxReserved?: number
): Promise<{ reserved: RegistryReservation[]; rejected: Array<{ productId: string; reason: string }>; mode: 'local' | 'server' }> {
  if (serverConfig()) {
    let reservationSubmitted = false
    try {
      await syncLocalRegistryToServer(accountKey)
      const reserved: RegistryReservation[] = []
      const rejected: Array<{ productId: string; reason: string }> = []
      const limit = Number.isInteger(maxReserved) ? Math.max(0, Number(maxReserved)) : items.length
      for (let offset = 0; offset < items.length && reserved.length < limit;) {
        const take = Math.min(50, limit - reserved.length)
        const chunk = items.slice(offset, offset + take)
        offset += chunk.length
        const remoteKey = remoteAccountKey(accountKey)
        const body = { requestId, accountKey: remoteKey, items: chunk }
        reservationSubmitted = true
        let response: RegistryReserveResponse
        try {
          response = await runServerRegistry<RegistryReserveResponse>('/reservations', body)
          if (!Array.isArray(response.reserved) || !Array.isArray(response.rejected)) {
            throw new RegistryAmbiguousResponseError('共享选品预留回执不完整；需要回读服务器状态后恢复。')
          }
        } catch (cause) {
          if (!(cause instanceof RegistryUnavailableError) && !(cause instanceof RegistryAmbiguousResponseError)) throw cause
          const readbackBody = { requestId, accountKey: remoteKey, productIds: chunk.map((item) => item.productId) }
          const readback = await runServerRegistry<{ reserved?: RegistryReservation[] }>(
            '/reservations/readback', readbackBody
          )
          if (!Array.isArray(readback.reserved)) throw new Error('共享选品预留回读不完整；保留检查点，未在本机重复预留。')
          const readbackIds = new Set(readback.reserved.map((item) => String(item.productId)))
          const missing = chunk.filter((item) => !readbackIds.has(item.productId))
          if (!missing.length) {
            response = { reserved: readback.reserved, rejected: [] }
          } else {
            try {
              response = await runServerRegistry<RegistryReserveResponse>('/reservations', { ...body, items: missing })
              if (!Array.isArray(response.reserved) || !Array.isArray(response.rejected)) {
                throw new RegistryAmbiguousResponseError('共享选品预留重试回执不完整；需要再次回读服务器状态。')
              }
            } catch (retryCause) {
              if (!(retryCause instanceof RegistryUnavailableError) && !(retryCause instanceof RegistryAmbiguousResponseError)) throw retryCause
              const afterRetry = await runServerRegistry<{ reserved?: RegistryReservation[] }>(
                '/reservations/readback', readbackBody
              )
              if (!Array.isArray(afterRetry.reserved)) throw new Error('共享选品预留重试后的回读不完整；保留检查点，未在本机重复预留。')
              const confirmedIds = new Set(afterRetry.reserved.map((item) => String(item.productId)))
              response = {
                reserved: afterRetry.reserved,
                rejected: missing.filter((item) => !confirmedIds.has(item.productId)).map((item) => ({
                  productId: item.productId, reason: '服务器预留回读未确认；未执行加车，需恢复后核对'
                }))
              }
            }
            if (Array.isArray(response.reserved)) {
              response.reserved = [...new Map([
                ...readback.reserved.map((item) => [String(item.productId), item] as const),
                ...response.reserved.map((item) => [String(item.productId), item] as const)
              ]).values()]
            }
          }
        }
        if (!Array.isArray(response.reserved) || !Array.isArray(response.rejected)) throw new Error('共享选品预留响应不完整；请核查服务端待处理记录。')
        reserved.push(...response.reserved)
        rejected.push(...response.rejected)
      }
      return { reserved, rejected, mode: 'server' }
    } catch (cause) {
      if (!(cause instanceof RegistryUnavailableError)) throw cause
      if (reservationSubmitted) {
        throw new Error(`${cause.message} 预留请求已经发出，结果未确认；本次停止本机回退和加车，请联网后读取服务器预留状态再恢复。`)
      }
    }
  }
  const localAccountKey = normalizeLocalAccountKey(accountKey)
  if (!requestId.trim()) throw new Error('本机登记请求标识为空；本次未执行加车。')
  if (!items.length) throw new Error('没有完整商品 ID 和末级类目的候选，本次未执行加车。')
  const args = [
    'reserve', '--account-key', localAccountKey,
    '--request-id', requestId,
    '--items-json', JSON.stringify(items)
  ]
  if (Number.isInteger(maxReserved) && Number(maxReserved) >= 0) {
    args.push('--max-reserved', String(maxReserved))
  }
  const result = await runLocalRegistry<RegistryPayload & {
    reserved?: RegistryReservation[]
    rejected?: Array<{ productId: string; reason: string }>
  }>(args)
  return {
    reserved: Array.isArray(result.reserved) ? result.reserved.map((item) => ({ ...item, token: `local:${item.token}` })) : [],
    rejected: Array.isArray(result.rejected) ? result.rejected : [],
    mode: 'local'
  }
}

export async function beginRegistryReservation(token: string): Promise<void> {
  if (token.startsWith('local:')) {
    await runLocalRegistry(['begin', '--token', token.slice('local:'.length)])
    return
  }
  if (serverConfig()) {
    try {
      await runServerRegistry(`/reservations/${encodeURIComponent(token)}/begin`, {})
      return
    } catch (cause) {
      if (!(cause instanceof RegistryUnavailableError) && !String(cause).includes('HTTP 404')) throw cause
    }
  }
  await runLocalRegistry(['begin', '--token', token])
}

export async function finishRegistryReservation(token: string, outcome: 'ADDED' | 'NOT_ADDED' | 'UNKNOWN'): Promise<void> {
  if (token.startsWith('local:')) {
    await runLocalRegistry(['finish', '--token', token.slice('local:'.length), '--outcome', outcome])
    return
  }
  if (serverConfig()) {
    try {
      await runServerRegistry(`/reservations/${encodeURIComponent(token)}/finish`, { outcome })
      return
    } catch (cause) {
      if (!(cause instanceof RegistryUnavailableError) && !String(cause).includes('HTTP 404')) throw cause
    }
  }
  await runLocalRegistry(['finish', '--token', token, '--outcome', outcome])
}

export function registerSelectionRegistryIpc(): void {
  ipcMain.handle('selection-registry:snapshot', (_event, accountKey: string, candidates: RegistryCandidate[]) =>
    getRegistrySnapshot(String(accountKey ?? ''), Array.isArray(candidates) ? candidates.map((candidate) => ({
      productId: String(candidate?.productId ?? ''),
      leafCategoryId: String(candidate?.leafCategoryId ?? '')
    })) : []))
  ipcMain.handle('selection-registry:snapshot-batch', (_event, entries: Array<{ accountKey: string; candidates: RegistryCandidate[] }>) =>
    getRegistryBatchSnapshots(Array.isArray(entries) ? entries.map((entry) => ({
      accountKey: String(entry?.accountKey ?? ''),
      candidates: Array.isArray(entry?.candidates) ? entry.candidates : []
    })) : []))
}
