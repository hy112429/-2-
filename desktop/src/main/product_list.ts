import { ipcMain } from 'electron'
import { getClientPaths } from './app-dirs'
import { runScript } from './engine'

interface ProductListQuery {
  accountId?: number | null
  status?: string
  query?: string
  limit?: number
  offset?: number
}

function validateIdentity(accountId: unknown, productId: unknown): { accountId: number; productId: string } {
  const id = Number(accountId)
  const product = String(productId ?? '').trim()
  if (!Number.isInteger(id) || id < 1 || !/^\d{8,20}$/.test(product)) {
    throw new Error('商品账号或商品 ID 无效')
  }
  return { accountId: id, productId: product }
}

async function runProductCommand(args: string[], stdinJson?: unknown): Promise<Record<string, unknown>> {
  const handle = runScript('selection_recovery.py', {
    args: ['--db', getClientPaths().databasePath, ...args], stdinJson, onEvent: () => undefined
  })
  const outcome = await handle.promise
  if (!outcome.ok) throw new Error(outcome.error ?? '商品列表操作失败')
  if (!outcome.result || typeof outcome.result !== 'object') throw new Error('商品列表接口没有返回有效数据')
  return outcome.result as Record<string, unknown>
}

export function registerProductListIpc(): void {
  ipcMain.handle('product-list:list', async (_event, raw: ProductListQuery) => {
    const params = raw && typeof raw === 'object' ? raw : {}
    const args = ['product', 'list']
    if (params.accountId !== undefined && params.accountId !== null && Number(params.accountId) > 0) {
      if (!Number.isInteger(Number(params.accountId))) throw new Error('账号 ID 无效')
      args.push('--account-id', String(Number(params.accountId)))
    }
    if (params.status) args.push('--status', String(params.status))
    if (params.query) args.push('--query', String(params.query).slice(0, 200))
    args.push('--limit', String(Math.min(100, Math.max(1, Math.trunc(Number(params.limit) || 20)))))
    args.push('--offset', String(Math.max(0, Math.trunc(Number(params.offset) || 0))))
    return runProductCommand(args)
  })

  ipcMain.handle('product-list:update', async (_event, raw: {
    accountId: number; productId: string; title: string; sku?: string; categoryLabels?: string[]
  }) => {
    const identity = validateIdentity(raw?.accountId, raw?.productId)
    return runProductCommand(
      ['product', 'update', '--account-id', String(identity.accountId), '--product-id', identity.productId],
      { title: String(raw.title ?? ''), sku: String(raw.sku ?? ''), categoryLabels: raw.categoryLabels ?? [] }
    )
  })

  ipcMain.handle('product-list:delete', async (_event, raw: { accountId: number; productId: string }) => {
    const identity = validateIdentity(raw?.accountId, raw?.productId)
    return runProductCommand(['product', 'delete', '--account-id', String(identity.accountId), '--product-id', identity.productId])
  })

  ipcMain.handle('product-list:cartOutcome', async (_event, raw: {
    accountId: number; productId: string; status: 'cart_added' | 'selection_failed' | 'needs_review'; reason?: string
  }) => {
    const identity = validateIdentity(raw?.accountId, raw?.productId)
    const status = raw?.status
    if (!['cart_added', 'selection_failed', 'needs_review'].includes(status)) throw new Error('选品车状态无效')
    return runProductCommand([
      'product', 'cart-outcome', '--account-id', String(identity.accountId), '--product-id', identity.productId,
      '--status', status, '--reason', String(raw.reason ?? '').slice(0, 500)
    ])
  })
}
