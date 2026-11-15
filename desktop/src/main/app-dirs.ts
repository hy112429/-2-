import { app, BrowserWindow, dialog, ipcMain } from 'electron'
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'path'

/**
 * 应用级数据路径与本地设置。
 * 开发态沿用原 D 盘目录，除显式隔离测试模式外；安装版使用独立 userData，
 * 测试模式只在 D:\\software\\codex 的独立子目录写入。
 */

const LEGACY_CLIENT_ROOT = 'D:\\software\\投广客户端'
const ISOLATED_TEST_ALLOWED_ROOT = 'D:\\software\\codex'

/** 显式隔离测试模式只允许在开发态写入 D:\\software\\codex 子目录。 */
export function getIsolatedTestRoot(): string | undefined {
  if (app.isPackaged) return undefined
  const configured = process.env['TOUGUANG_CLIENT_TEST_ROOT']?.trim()
  if (!configured) return undefined
  const root = resolve(configured)
  const allowedRoot = resolve(ISOLATED_TEST_ALLOWED_ROOT)
  const relativeRoot = relative(allowedRoot, root)
  if (!relativeRoot || relativeRoot === '..' || relativeRoot.startsWith(`..${sep}`) || isAbsolute(relativeRoot)) {
    throw new Error(`客户端隔离测试目录必须是 ${allowedRoot} 下的独立子目录。`)
  }
  return root
}

export interface ClientPaths {
  rootDir: string
  dataDir: string
  databasePath: string
  databaseExists: boolean
  downloadsDir: string
  browserProfilesDir: string
}

export function getClientPaths(): ClientPaths {
  const testRoot = getIsolatedTestRoot()
  const rootDir = testRoot
    ? join(testRoot, 'client-data')
    : app.isPackaged
      ? join(app.getPath('userData'), 'client-data')
      : LEGACY_CLIENT_ROOT
  const dataDir = join(rootDir, 'data')
  const downloadsDir = join(rootDir, 'downloads')
  const browserProfilesDir = join(rootDir, 'browser-profiles')
  for (const directory of [dataDir, downloadsDir, browserProfilesDir]) {
    mkdirSync(directory, { recursive: true })
  }
  return {
    rootDir,
    dataDir,
    databasePath: join(dataDir, 'ad_client.db'),
    databaseExists: existsSync(join(dataDir, 'ad_client.db')),
    downloadsDir,
    browserProfilesDir
  }
}

function settingsPath(): string {
  return join(app.getPath('userData'), 'app-settings.json')
}

interface AppSettings {
  downloadDir?: string
  lastPublishVideoDir?: string
  lastProfileDir?: string
  lastAccountDownloadDir?: string
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
  // 用户已选目录优先返回，即使默认目录不存在也不能丢失自定义路径。
  return readSettings().downloadDir ?? getClientPaths().downloadsDir
}

function existingDirectory(value: string | undefined): string | undefined {
  if (!value) return undefined
  try {
    return statSync(value).isDirectory() ? value : undefined
  } catch {
    return undefined
  }
}

function rememberDirectory(key: keyof AppSettings, directory: string): void {
  writeSettings({ ...readSettings(), [key]: directory })
}

export function registerAppDirsIpc(): void {
  ipcMain.handle('app:getPaths', () => getClientPaths())
  ipcMain.handle('dir:getDownloadDir', () => getDownloadDir())

  // 本地发布计划仅接收 MP4 文件，不触发任何平台上传。
  ipcMain.handle('dir:choosePublishVideoFile', async () => {
    const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
    if (!win) return null
    const settings = readSettings()
    const result = await dialog.showOpenDialog(win, {
      title: '选择本地 MP4 视频',
      defaultPath: existingDirectory(settings.lastPublishVideoDir) ?? existingDirectory(settings.downloadDir) ?? existingDirectory(getClientPaths().downloadsDir) ?? app.getPath('home'),
      properties: ['openFile'],
      filters: [{ name: 'MP4 视频', extensions: ['mp4'] }]
    })
    if (result.canceled || result.filePaths.length === 0) return null
    const filePath = result.filePaths[0]
    rememberDirectory('lastPublishVideoDir', dirname(filePath))
    return filePath
  })

  // 系统目录选择器 → 存到设置 → 返回新路径（取消返回 null）
  ipcMain.handle('dir:chooseDownloadDir', async () => {
    const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
    if (!win) return null
    const settings = readSettings()
    const result = await dialog.showOpenDialog(win, {
      title: '选择下载目录',
      defaultPath: existingDirectory(settings.downloadDir) ?? existingDirectory(getClientPaths().downloadsDir) ?? app.getPath('home'),
      properties: ['openDirectory', 'createDirectory']
    })
    if (result.canceled || result.filePaths.length === 0) return null
    const dir = result.filePaths[0]
    writeSettings({ ...readSettings(), downloadDir: dir })
    for (const window of BrowserWindow.getAllWindows()) {
      if (!window.isDestroyed() && !window.webContents.isDestroyed()) {
        window.webContents.send('app:downloadDirChanged', dir)
      }
    }
    return dir
  })

  ipcMain.handle('selection:chooseProfileDir', async () => {
    const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
    if (!win) return null
    const settings = readSettings()
    const result = await dialog.showOpenDialog(win, {
      title: '高级设置：选择浏览器登录资料目录（必须包含 Default 文件夹）',
      defaultPath: existingDirectory(settings.lastProfileDir) ?? app.getPath('home'),
      properties: ['openDirectory', 'createDirectory']
    })
    if (result.canceled || result.filePaths.length === 0) return null
    const dir = result.filePaths[0]
    rememberDirectory('lastProfileDir', dir)
    return dir
  })

  ipcMain.handle('selection:chooseDownloadDir', async () => {
    const win = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
    if (!win) return null
    const settings = readSettings()
    const result = await dialog.showOpenDialog(win, {
      title: '选择账号下载目录',
      defaultPath: existingDirectory(settings.lastAccountDownloadDir) ?? existingDirectory(settings.downloadDir) ?? existingDirectory(getClientPaths().downloadsDir) ?? app.getPath('home'),
      properties: ['openDirectory', 'createDirectory']
    })
    if (result.canceled || result.filePaths.length === 0) return null
    const dir = result.filePaths[0]
    rememberDirectory('lastAccountDownloadDir', dir)
    return dir
  })

  ipcMain.handle('selection:validateProfileDir', (_event, value: unknown) => {
    if (typeof value !== 'string' || !value.trim()) return { ok: false, error: '请选择浏览器登录资料文件夹，或在客户端点“恢复默认”' }
    const target = resolve(value)
    const defaultDir = join(target, 'Default')
    try {
      if (!statSync(target).isDirectory()) return { ok: false, error: '浏览器登录资料路径不是文件夹，请点“恢复默认”' }
    } catch {
      return { ok: false, error: '浏览器登录资料文件夹不存在，请点“恢复默认”；视频下载位置请填写“账号下载路径”' }
    }
    try {
      return statSync(defaultDir).isDirectory()
        ? { ok: true, path: target }
        : { ok: false, error: '这个文件夹不是浏览器登录资料，请点“恢复默认”；视频下载位置请填写“账号下载路径”' }
    } catch {
      return { ok: false, error: '这个文件夹不是浏览器登录资料，请点“恢复默认”；视频下载位置请填写“账号下载路径”' }
    }
  })

  ipcMain.handle('selection:canonicalizePath', (_event, value: unknown) => {
    if (typeof value !== 'string' || !value.trim()) return { ok: false, error: '路径不能为空' }
    try {
      return { ok: true, path: resolve(value) }
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })
}
