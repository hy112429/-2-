import { dialog, ipcMain, shell } from 'electron'
import { existsSync } from 'node:fs'
import { isAbsolute, relative, resolve, sep } from 'node:path'
import { getClientPaths } from './app-dirs'
import { runEngineCommand } from './engine'

export function registerPublishArtifactsIpc(): void {
  ipcMain.handle('publish:exportReceipts', async () => {
    const chosen = await dialog.showSaveDialog({
      title: '导出发布回执', defaultPath: '发布回执.csv',
      filters: [{ name: 'CSV', extensions: ['csv'] }]
    })
    if (chosen.canceled || !chosen.filePath) return { ok: false, cancelled: true }
    const outcome = await runEngineCommand('publish_queue.py', [
      '--db', getClientPaths().databasePath, 'receipt-export', '--output', chosen.filePath
    ])
    if (!outcome.ok) return { ok: false, error: outcome.error ?? '导出失败' }
    return outcome.result
  })

  ipcMain.handle('publish:openScreenshot', async (_event, rawPath: unknown) => {
    if (typeof rawPath !== 'string' || !rawPath.trim()) return { ok: false, error: '截图路径无效' }
    const root = resolve(getClientPaths().dataDir, 'diagnostics', 'publish')
    const path = resolve(rawPath)
    const inside = relative(root, path)
    if (!inside || inside === '..' || inside.startsWith(`..${sep}`) || isAbsolute(inside) ||
        !path.toLowerCase().endsWith('.png') || !existsSync(path)) {
      return { ok: false, error: '截图不在本机发布诊断目录或文件已不存在' }
    }
    const error = await shell.openPath(path)
    return error ? { ok: false, error } : { ok: true }
  })
}
