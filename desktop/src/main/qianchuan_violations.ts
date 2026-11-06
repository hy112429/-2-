import { dialog, ipcMain } from 'electron'
import { copyFileSync, mkdirSync } from 'fs'
import { basename, isAbsolute, join, relative, resolve } from 'path'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'
import { existingManagedBrowserConnection } from './external_browser'
import { releaseQianchuanAccount, reserveQianchuanAccount } from './qianchuan_account_lock'

type Data = Record<string, unknown>

function idOf(value: unknown, label: string): number {
  const id = Number(value)
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error(`${label}无效`)
  return id
}

async function command(action: string, args: string[] = [], allowBusinessFailure = false): Promise<Data> {
  const outcome = await runEngineCommand('qianchuan_violations.py', [
    '--db', getClientPaths().databasePath, action, ...args
  ])
  if (!outcome.ok) throw new Error(outcome.error || '违规素材操作失败')
  const result = outcome.result as Data | undefined
  if (!result || (result.ok === false && !allowBusinessFailure)) throw new Error(String(result?.error || '违规素材操作失败'))
  return result
}

async function connectionArgs(qcId: number): Promise<string[]> {
  const connection = await existingManagedBrowserConnection(`qianchuan-${qcId}`)
  if (!connection) throw new Error(`千川账户 ${qcId} 的专属浏览器未连接，请先打开并登录`)
  return ['--qianchuan-account-id', String(qcId), '--profile-path', connection.profilePath,
    '--cdp-port', String(connection.port)]
}

function reply<T>(action: () => Promise<T>): Promise<T | Data> {
  return action().catch((error) => ({ ok: false, error: String(error) }))
}

export function registerQianchuanViolationIpc(): void {
  ipcMain.handle('qianchuan:violations:scan', (_event, value: unknown) => reply(async () => {
    const qcId = idOf(value, '千川账户 ID')
    if (!reserveQianchuanAccount(qcId)) throw new Error('该千川账户正在处理素材，请稍后刷新')
    try { return await command('scan', await connectionArgs(qcId)) }
    finally { releaseQianchuanAccount(qcId) }
  }))

  ipcMain.handle('qianchuan:violations:prepare', (_event, input: { qianchuanAccountId: unknown; ids: unknown }) => reply(async () => {
    const qcId = idOf(input?.qianchuanAccountId, '千川账户 ID')
    const values = Array.isArray(input?.ids) ? input.ids : []
    const ids = [...new Set(values.map(value => String(value).trim()))]
    if (ids.length < 1 || ids.length > 50 || ids.length !== values.length ||
        ids.some(id => !/^[A-Za-z0-9_-]{8,64}$/.test(id))) throw new Error('请选择 1–50 条有唯一 ID 的违规素材')
    const exportDir = join(getClientPaths().dataDir, 'exports', 'qianchuan-violations')
    mkdirSync(exportDir, { recursive: true })
    return command('prepare', ['--qianchuan-account-id', String(qcId), '--ids', ids.join(','), '--export-dir', exportDir])
  }))

  ipcMain.handle('qianchuan:violations:batch', (_event, rawId: unknown) => reply(() =>
    command('batch', ['--batch-id', String(idOf(rawId, '删除批次 ID'))])
  ))

  ipcMain.handle('qianchuan:violations:history', (_event, rawId: unknown) => reply(() =>
    command('history', ['--qianchuan-account-id', String(idOf(rawId, '千川账户 ID'))])
  ))

  ipcMain.handle('qianchuan:violations:export', (_event, rawId: unknown) => reply(async () => {
    const detail = await command('batch', ['--batch-id', String(idOf(rawId, '删除批次 ID'))])
    const batch = detail.batch as Data
    const original = String(batch.manifest_path || '')
    const outside = relative(resolve(getClientPaths().dataDir, 'exports', 'qianchuan-violations'), resolve(original))
    if (!original || outside.startsWith('..') || isAbsolute(outside)) {
      throw new Error('删除清单路径无效')
    }
    const selected = await dialog.showSaveDialog({ title: '导出千川违规素材删除清单', defaultPath: basename(original),
      filters: [{ name: 'CSV', extensions: ['csv'] }] })
    if (selected.canceled || !selected.filePath) return { ok: true, cancelled: true }
    copyFileSync(original, selected.filePath)
    return { ok: true, path: selected.filePath }
  }))

  ipcMain.handle('qianchuan:violations:execute', (event, input: { batchId: unknown; count: unknown; confirmation: unknown }) => reply(async () => {
    const batchId = idOf(input?.batchId, '删除批次 ID')
    const detail = await command('batch', ['--batch-id', String(batchId)])
    const batch = detail.batch as Data
    const items = detail.items as Data[]
    const qcId = idOf(batch.qianchuan_account_id, '千川账户 ID')
    if (batch.status !== 'prepared' || !items.length || items.length > 50 ||
        items.some(item => item.status !== 'pending')) throw new Error('批次状态已变化，请重新预览删除清单')
    if (input.count !== items.length || input.confirmation !== `删除 ${items.length} 条`) {
      throw new Error('二次确认文字或待删条数不匹配')
    }
    if (!reserveQianchuanAccount(qcId)) throw new Error('该千川账户正在处理素材')
    try {
      const browserArgs = await connectionArgs(qcId)
      const diagnosticsDir = join(getClientPaths().dataDir, 'diagnostics', 'qianchuan-violations')
      mkdirSync(diagnosticsDir, { recursive: true })
      for (let index = 0; index < items.length; index += 1) {
        const itemId = idOf(items[index].id, '删除项目 ID')
        const progress = { batchId, completed: index, total: items.length, itemId }
        if (!event.sender.isDestroyed()) event.sender.send('qianchuan:violations:progress', progress)
        const result = await command('delete-one', ['--batch-id', String(batchId), '--item-id', String(itemId),
          ...browserArgs, '--diagnostics-dir', diagnosticsDir], true)
        if (!event.sender.isDestroyed()) event.sender.send('qianchuan:violations:progress', {
          ...progress, completed: index + 1, status: result.status, reason: result.reason
        })
        if (result.status !== 'deleted') break
      }
      return command('batch', ['--batch-id', String(batchId)])
    } finally { releaseQianchuanAccount(qcId) }
  }))
}
