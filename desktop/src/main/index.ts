import { app, shell, BrowserWindow, protocol } from 'electron'
import { join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import { registerLoginIpc, disposeLogin } from './login'
import { registerEngineIpc, disposeEngine } from './engine'
import { registerAppDirsIpc } from './app-dirs'
import { registerAssetsIpc } from './assets'
import { registerBaiyingBatchIpc, disposeBaiyingBatch } from './baiying_batch'
import { registerToutiaoBatchIpc, disposeToutiaoBatch } from './toutiao_batch'
import { registerAccountLoginIpc, disposeAccountLogin } from './account_login'
import { registerWatermarkIpc, registerFrameProtocol } from './watermark'

// ---- 窗口基础参数（需求 3）----
const WINDOW_TITLE = '投广管理客户端'
const WINDOW_WIDTH = 1280

// 自定义协议必须在 app ready 之前声明（去水印帧图 frame://）
protocol.registerSchemesAsPrivileged([
  { scheme: 'frame', privileges: { standard: true, secure: true, supportFetchAPI: false } }
])
const WINDOW_HEIGHT = 800

/** 创建主窗口 */
function createWindow(): void {
  const mainWindow = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    // 最小尺寸：比默认尺寸小一圈，保证布局不被压塌
    minWidth: 1024,
    minHeight: 640,
    title: WINDOW_TITLE,
    // show: false + ready-to-show 再显示：避免先闪一个白屏再出现内容
    show: false,
    // 隐藏 Windows 原生菜单栏（Alt 才会露出）。
    // 需求里的「顶部菜单」是我们自己在页面里画的，不是原生菜单。
    autoHideMenuBar: true,
    webPreferences: {
      // 预加载脚本：渲染进程和主进程之间唯一的合法通道
      preload: join(__dirname, '../preload/index.js'),
      // sandbox: false 是为了让预加载脚本能用 Node API。
      // 安全性靠 contextIsolation（默认开启）+ 只暴露白名单接口来保证。
      sandbox: false
    }
  })

  // 页面里的 <title> 不允许覆盖窗口标题，锁死为「投广管理客户端」
  mainWindow.on('page-title-updated', (event) => {
    event.preventDefault()
  })

  mainWindow.on('ready-to-show', () => {
    mainWindow.show()
  })

  // 页面里 target="_blank" 的链接一律交给系统浏览器，避免在客户端里再开一个窗口
  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  // 开发时加载 Vite 开发服务器（有热更新）；打包后加载构建产物
  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  // 让 Windows 任务栏正确识别本应用（通知、任务栏分组都依赖它）
  electronApp.setAppUserModelId('com.touguang.client')

  // 注册登录相关 IPC（内置浏览器视图 / 登录态查询 / 导出给 Python 引擎）
  // 必须在 whenReady 之后，因为里面要用到 session 和 safeStorage
  registerLoginIpc()

  // 注册 Python 引擎 IPC（子进程拉起 / 进度转发 / 取消）
  registerEngineIpc()

  // 注册应用本地设置 IPC（下载目录选择与持久化）
  registerAppDirsIpc()

  // 注册素材库辅助 IPC（缩略图读取 / 打开所在文件夹）
  registerAssetsIpc()

  // 注册百应批量抓取 IPC（T-15：按商品ID列表驱动登录视图逐页抓取）
  registerBaiyingBatchIpc()

  // 注册头条页面批量解析 IPC（Step 10：内置浏览器逐页打开头条链接，探针捕获 vod 直链）
  registerToutiaoBatchIpc()

  // 注册账号级登录态 IPC（Step 11：一账号一分区 + 千川多开隔离 + 指纹 + 加密备份）
  registerAccountLoginIpc()

  // 注册去水印辅助 IPC（抽帧 + 暂存模板）
  registerWatermarkIpc()
  // 注册 frame:// 协议（渲染进程加载去水印帧图）
  registerFrameProtocol()

  // 开发时保留 F12 开发者工具、Ctrl+R 刷新等快捷键；生产环境自动禁用
  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  createWindow()

  // macOS 习惯：点 Dock 图标且没有窗口时重新创建一个
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

// 退出前释放内置浏览器视图 + 杀掉所有引擎子进程 + 停掉批量抓取
app.on('before-quit', () => {
  disposeLogin()
  disposeEngine()
  disposeBaiyingBatch()
  disposeToutiaoBatch()
  disposeAccountLogin()
})

// Windows / Linux：关掉所有窗口就退出应用（macOS 不是这样）
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})