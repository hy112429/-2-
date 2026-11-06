import { ipcMain } from 'electron'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'

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
  mode: 'local'
}

interface RegistryPayload {
  ok: boolean
  error?: string
  [key: string]: unknown
}

function normalizeLocalAccountKey(value: string): string {
  const key = String(value ?? '').trim()
  if (!/^local:[1-9]\d*$/.test(key)) {
    throw new Error('本机选品登记缺少有效的客户端账号 ID。')
  }
  return key
}

async function runLocalRegistry<T extends RegistryPayload>(args: string[]): Promise<T> {
  const outcome = await runEngineCommand('selection_registry_local.py', [
    '--db', getClientPaths().databasePath,
    ...args
  ])
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

export async function getRegistrySnapshot(accountKey: string, _candidates: RegistryCandidate[]): Promise<RegistrySnapshot> {
  const localAccountKey = normalizeLocalAccountKey(accountKey)
  const result = await runLocalRegistry<RegistryPayload & RegistrySnapshot>([
    'snapshot', '--account-key', localAccountKey
  ])
  return {
    usedProductIds: Array.isArray(result.usedProductIds) ? result.usedProductIds.map(String) : [],
    accountUsedCategoryIds: Array.isArray(result.accountUsedCategoryIds) ? result.accountUsedCategoryIds.map(String) : [],
    categoryUsage: result.categoryUsage && typeof result.categoryUsage === 'object'
      ? result.categoryUsage as Record<string, number>
      : {},
    mode: 'local'
  }
}

export async function reserveRegistryCandidates(
  accountKey: string,
  requestId: string,
  items: RegistryCandidate[],
  maxReserved?: number
): Promise<{ reserved: RegistryReservation[]; rejected: Array<{ productId: string; reason: string }>; mode: 'local' }> {
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
  await runLocalRegistry(['begin', '--token', token])
}

export async function finishRegistryReservation(token: string, outcome: 'ADDED' | 'NOT_ADDED' | 'UNKNOWN'): Promise<void> {
  await runLocalRegistry(['finish', '--token', token, '--outcome', outcome])
}

export function registerSelectionRegistryIpc(): void {
  ipcMain.handle('selection-registry:snapshot', (_event, accountKey: string, candidates: RegistryCandidate[]) =>
    getRegistrySnapshot(String(accountKey ?? ''), Array.isArray(candidates) ? candidates.map((candidate) => ({
      productId: String(candidate?.productId ?? ''),
      leafCategoryId: String(candidate?.leafCategoryId ?? '')
    })) : []))
}
