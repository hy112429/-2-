import { ipcMain, shell } from 'electron'
import { readFileSync } from 'fs'

/**
 * 素材库辅助 IPC（AssetLibraryView 用）：
 *  - assets:getThumb   缩略图文件 → base64 dataURL（渲染进程避开 file:// 跨源限制）
 *  - assets:openFolder 打开素材所在文件夹并选中该文件（shell.showItemInFolder）
 */
export function registerAssetsIpc(): void {
  ipcMain.handle('assets:getThumb', (_event, filePath: string) => {
    try {
      const buf = readFileSync(filePath)
      return `data:image/jpeg;base64,${buf.toString('base64')}`
    } catch {
      return null
    }
  })

  ipcMain.handle('assets:openFolder', (_event, filePath: string) => {
    try {
      shell.showItemInFolder(filePath)
      return { ok: true }
    } catch (error) {
      return { ok: false, error: String(error) }
    }
  })
}