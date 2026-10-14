import { app, BrowserWindow, dialog, ipcMain } from 'electron'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { dirname, join } from 'path'

/**
 * 应用级本地设置：目前只有「下载目录」。
 * 存到 userData/app-settings.json（JSON，不带敏感信息）。
 * 默认目录与文档一致：D:\software\投广客户端\downloads
 */

const DEFAULT_DOWNLOAD_DIR = 'D:\\software\\投广客户端\\downloads'

function settingsPath(): string {
  return join(app.getPath('userData'), 'app-settings.json')
}

interface AppSettings {
  downloadDir?: string
}

function readSettings(): AppSettings {
  try {
    return JSON.parse(readFileSync(settingsPath(), 'utf-8')) as AppSettings
  } catch {
    return {}
  }
}

function writeSettings(next: AppSettings): void {
  mkdirSync(dirname(settingsPath()), { recursive: true })
  writeFileSync(settingsPath(), JSON.stringify(next, null, 2), 'utf-8')
}

/** 当前下载目录（未设置时用默认值） */
export function getDownloadDir(): string {
  if (!existsSync(DEFAULT_DOWNLOAD_DIR)) {
    // 默认目录不存在时仍然返回路径，实际写入前由调用方负责创建
    return DEFAULT_DOWNLOAD_DIR
  }
  return readSettings().downloadDir ?? DEFAULT_DOWNLOAD_DIR
}

export function registerAppDirsIpc(): void {
  ipcMain.handle('dir:getDownloadDir', () => getDownloadDir())

  // 系统目录选择器 → 存到设置 → 返回新路径（取消返回 null）
  ipcMain.handle('dir:chooseDownloadDir', async () => {
    const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
    if (!win) return null
    const result = await dialog.showOpenDialog(win, {
      title: '选择下载目录',
      properties: ['openDirectory', 'createDirectory']
    })
    if (result.canceled || result.filePaths.length === 0) return null
    const dir = result.filePaths[0]
    writeSettings({ ...readSettings(), downloadDir: dir })
    return dir
  })
}