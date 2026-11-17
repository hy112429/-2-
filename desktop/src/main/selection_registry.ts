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

export interface RegistrySnapshot {
  usedProductIds: string[]
  accountUsedCategoryIds: string[]
  categoryUsage: Record<string, number>
  mode: 'local' | 'server'
}

interface RegistryPayload {
  ok: boolean
  error?: string
  [key: string]: unknown
}

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
      if (attempt === 1) throw new Error('共享选品登记服务不可达；已保留检查点，未按未查重结果继续。')
    }
  }
  if (!response) throw new Error('共享选品登记未返回结果；已保留检查点。')
  if (!response.ok) throw new Error(`共享选品登记返回 HTTP ${response.status}；已保留检查点。`)
  try { return await response.json() as T }
  catch { throw new Error('共享选品登记响应无效；已保留检查点。') }
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

export async function getRegistrySnapshot(accountKey: string, candidates: RegistryCandidate[]): Promise<RegistrySnapshot> {
  if (serverConfig()) return getServerSnapshot(accountKey, candidates)
  const localAccountKey = normalizeLocalAccountKey(accountKey)
  const result = await runLocalRegistry<RegistryPayload & RegistrySnapshot>([
    'snapshot', '--account-key', localAccountKey
  ], candidates)
  if (!Array.isArray(result.usedProductIds) || !Array.isArray(result.accountUsedCategoryIds) ||
      !result.categoryUsage || typeof result.categoryUsage !== 'object' || Array.isArray(result.categoryUsage)) {
    throw new Error('本机选品查重结果不完整；本次已停止，未执行加车。')
  }
  return {
    usedProductIds: Array.isArray(result.usedProductIds) ? result.usedProductIds.map(String) : [],
    accountUsedCategoryIds: Array.isArray(result.accountUsedCategoryIds) ? result.accountUsedCategoryIds.map(String) : [],
    categoryUsage: result.categoryUsage && typeof result.categoryUsage === 'object'
      ? result.categoryUsage as Record<string, number>
      : {},
    mode: 'local'
  }
}

export async function getRegistryBatchSnapshots(
  entries: Array<{ accountKey: string; candidates: RegistryCandidate[] }>
): Promise<Record<string, RegistrySnapshot>> {
  if (serverConfig()) {
    const snapshots: Record<string, RegistrySnapshot> = {}
    for (const entry of entries) snapshots[entry.accountKey] = await getServerSnapshot(entry.accountKey, entry.candidates)
    return snapshots
  }
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

export async function reserveRegistryCandidates(
  accountKey: string,
  requestId: string,
  items: RegistryCandidate[],
  maxReserved?: number
): Promise<{ reserved: RegistryReservation[]; rejected: Array<{ productId: string; reason: string }>; mode: 'local' | 'server' }> {
  if (serverConfig()) {
    const reserved: RegistryReservation[] = []
    const rejected: Array<{ productId: string; reason: string }> = []
    const limit = Number.isInteger(maxReserved) ? Math.max(0, Number(maxReserved)) : items.length
    for (let offset = 0; offset < items.length && reserved.length < limit;) {
      const take = Math.min(50, limit - reserved.length)
      const chunk = items.slice(offset, offset + take)
      offset += chunk.length
      const response = await runServerRegistry<{ reserved?: RegistryReservation[]; rejected?: Array<{ productId: string; reason: string }> }>(
        '/reservations', { requestId, accountKey: remoteAccountKey(accountKey), items: chunk }
      )
      if (!Array.isArray(response.reserved) || !Array.isArray(response.rejected)) throw new Error('共享选品预留响应不完整；请核查服务端待处理记录。')
      reserved.push(...response.reserved)
      rejected.push(...response.rejected)
    }
    return { reserved, rejected, mode: 'server' }
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
    reserved: Array.isArray(result.reserved) ? result.reserved : [],
    rejected: Array.isArray(result.rejected) ? result.rejected : [],
    mode: 'local'
  }
}

export async function beginRegistryReservation(token: string): Promise<void> {
  if (serverConfig()) {
    await runServerRegistry(`/reservations/${encodeURIComponent(token)}/begin`, {})
    return
  }
  await runLocalRegistry(['begin', '--token', token])
}

export async function finishRegistryReservation(token: string, outcome: 'ADDED' | 'NOT_ADDED' | 'UNKNOWN'): Promise<void> {
  if (serverConfig()) {
    await runServerRegistry(`/reservations/${encodeURIComponent(token)}/finish`, { outcome })
    return
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
