import { contextBridge, ipcRenderer } from 'electron'
import { electronAPI } from '@electron-toolkit/preload'

/**
 * 预加载脚本：渲染进程（网页）与主进程（Node）之间唯一的合法通道。
 *
 * 安全原则：渲染进程不能直接用 Node，也不能拿到原始 ipcRenderer，
 * 只能调用这里**显式暴露**的方法。每暴露一个方法就等于开一个口子，
 * 所以下面按「登录相关」和「内置浏览器」两组严格限定。
 */

// ---------------------------------------------------------------- 内置浏览器视图

const browser = {
  /** 登录目标清单（抖音主站 / 巨量百应） */
  getTargets: (): Promise<Array<{ id: string; name: string; url: string }>> =>
    ipcRenderer.invoke('login:getTargets'),

  /** 打开登录页（会创建/显示内置浏览器视图） */
  open: (targetId: string): Promise<{ ok: boolean; url: string }> =>
    ipcRenderer.invoke('browser:open', targetId),

  /** 关闭视图（**不会**清登录态，cookie 仍保留） */
  close: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('browser:close'),

  /**
   * 上报视图位置。
   * WebContentsView 是覆盖在页面之上的独立图层，位置只能由渲染进程测出来告诉主进程。
   */
  setBounds: (rect: { x: number; y: number; width: number; height: number }): void => {
    ipcRenderer.send('browser:setBounds', rect)
  },

  goBack: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('browser:goBack'),
  goForward: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('browser:goForward'),
  reload: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('browser:reload'),
  getState: (): Promise<BrowserViewState> => ipcRenderer.invoke('browser:getState'),

  /** 订阅视图状态变化，返回取消订阅函数 */
  onStateChanged: (cb: (s: BrowserViewState) => void): (() => void) =>
    subscribe('browser:stateChanged', cb),

  /** 主进程要求重新上报位置（窗口尺寸变化时触发） */
  onNeedBounds: (cb: () => void): (() => void) => subscribe('browser:needBounds', cb),

  /** 页面加载失败 */
  onLoadFailed: (cb: (e: { code: number; desc: string; url: string }) => void): (() => void) =>
    subscribe('browser:loadFailed', cb)
}

// ---------------------------------------------------------------- 登录态

const login = {
  /** 查询登录状态。返回值**只含 cookie 名字，不含值** */
  check: (): Promise<LoginState> => ipcRenderer.invoke('login:check'),

  /** 登出：清掉抖音/百应相关 cookie */
  clear: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('login:clear'),

  /**
   * 导出登录态给 Python 引擎用。
   * ⚠️ 返回值含 cookie 明文，只在内存里流转，不要写日志或存文件。
   */
  exportForEngine: (targetId: string): Promise<EngineSession> =>
    ipcRenderer.invoke('login:exportForEngine', targetId),

  onStateChanged: (cb: (s: LoginState) => void): (() => void) =>
    subscribe('login:stateChanged', cb)
}

// ---------------------------------------------------------------- Python 引擎（T-11）

/**
 * 引擎子进程控制。进度/终态以事件流 engine:event 回传：
 *   run() 立刻拿到 {taskId}；期间 onEvent 会收到带同一 taskId 的进度事件；
 *   收尾事件 type 为 result（成功）或 error（失败/取消）。
 */
const engine = {
  /** 测试连通性：跑 engine/ping.py，返回结构化结论 */
  ping: (): Promise<EnginePingResult> => ipcRenderer.invoke('engine:ping'),

  /** 启动一个引擎任务，立刻返回 {taskId}；进度与终态走 onEvent */
  run: (params: EngineRunParams): Promise<EngineRunReply> =>
    ipcRenderer.invoke('engine:run', params),

  /** 取消任务（Windows 上按进程树强杀） */
  cancel: (taskId: string): Promise<{ ok: boolean; alreadyEnded?: boolean }> =>
    ipcRenderer.invoke('engine:cancel', taskId),

  /** 订阅引擎事件流，返回取消订阅函数 */
  onEvent: (cb: (e: EngineEventPayload) => void): (() => void) =>
    subscribe('engine:event', cb)
}

// ---------------------------------------------------------------- 本地设置（下载目录）

/** 应用本地设置：下载目录的读取与选择 */
const dir = {
  /** 当前下载目录（未设置时返回默认值） */
  getDownloadDir: (): Promise<string> => ipcRenderer.invoke('dir:getDownloadDir'),

  /** 弹出系统目录选择器；返回新目录，取消返回 null */
  chooseDownloadDir: (): Promise<string | null> => ipcRenderer.invoke('dir:chooseDownloadDir')
}

// ---------------------------------------------------------------- 素材库辅助（缩略图 / 打开文件夹）

/** 素材库本地辅助：缩略图 base64、打开所在文件夹 */
const assets = {
  /** 读缩略图文件 → dataURL（JPEG）；失败返回 null */
  getThumb: (filePath: string): Promise<string | null> =>
    ipcRenderer.invoke('assets:getThumb', filePath),

  /** 打开素材所在文件夹并选中文件 */
  openFolder: (filePath: string): Promise<{ ok: boolean; error?: string }> =>
    ipcRenderer.invoke('assets:openFolder', filePath)
}

// ---------------------------------------------------------------- 百应批量抓取（T-15）

/** 百应批量抓取：按商品ID列表驱动登录视图逐页抓取缓存 */
const baiying = {
  /** 启动批量抓取，立刻返回 {taskId}；进度与终态走 onBatchEvent */
  batchStart: (params: {
    productIds: string[]
    intervalMs?: number
  }): Promise<BaiyingBatchStartReply> => ipcRenderer.invoke('baiying:batchStart', params),

  /** 停止批量抓取（当前商品完成后结束） */
  batchCancel: (taskId: string): Promise<{ ok: boolean; alreadyEnded?: boolean }> =>
    ipcRenderer.invoke('baiying:batchCancel', taskId),

  /** 订阅批量抓取事件流，返回取消订阅函数 */
  onBatchEvent: (cb: (e: BaiyingBatchEvent) => void): (() => void) =>
    subscribe('baiying:event', cb)
}

// ---------------------------------------------------------------- 头条页面批量解析（Step 10）

/** 头条批量解析：内置浏览器逐页打开头条链接，探针捕获 vod 直链 */
const toutiao = {
  /** 启动批量解析，立刻返回 {taskId}；进度与终态走 onBatchEvent */
  batchStart: (params: { links: string[]; intervalMs?: number }): Promise<BaiyingBatchStartReply> =>
    ipcRenderer.invoke('toutiao:batchStart', params),

  /** 停止批量解析（当前链接完成后结束） */
  batchCancel: (taskId: string): Promise<{ ok: boolean; alreadyEnded?: boolean }> =>
    ipcRenderer.invoke('toutiao:batchCancel', taskId),

  /** 订阅批量解析事件流，返回取消订阅函数 */
  onBatchEvent: (cb: (e: ToutiaoBatchEvent) => void): (() => void) => subscribe('toutiao:event', cb)
}

// ---------------------------------------------------------------- 账号级登录态（Step 11）

/**
 * 账号级登录态：每抖音账号独立 Chromium 分区（persist:acct-{id}），
 * 切换即换分区天然不串号；safeStorage 加密备份；指纹隔离。
 */
const account = {
  /** 打开浏览器登录（该账号独立分区） */
  openBrowser: (accountId: number): Promise<{ ok: boolean; url: string; error?: string }> =>
    ipcRenderer.invoke('account:openBrowser', accountId),

  /** 关闭浏览器视图（登录态保留） */
  closeBrowser: (): Promise<{ ok: boolean }> => ipcRenderer.invoke('account:closeBrowser'),

  /** 打开精选页（巨量百应选品广场，SRS FR-G4-01） */
  openJingxuan: (accountId: number): Promise<{ ok: boolean; url: string; error?: string }> =>
    ipcRenderer.invoke('account:openJingxuan', accountId),

  /** 拉取账号公开信息（昵称/粉丝数/作品数，写库由渲染进程调引擎完成） */
  pullInfo: (params: { accountId: number; douyinId: string }): Promise<AccountUserInfo | { error: string } | null> =>
    ipcRenderer.invoke('account:pullInfo', params),

  /** 查询该账号登录态（只含 cookie 名，不含值） */
  checkLogin: (accountId: number): Promise<AccountLoginState> =>
    ipcRenderer.invoke('account:checkLogin', accountId),

  /** 导出登录态给 Python 引擎（明文仅内存流转，不落盘） */
  exportForEngine: (accountId: number): Promise<AccountEngineSession> =>
    ipcRenderer.invoke('account:exportForEngine', accountId),

  /** 清除该账号登录态（含加密备份） */
  clearLogin: (accountId: number): Promise<{ ok: boolean }> =>
    ipcRenderer.invoke('account:clearLogin', accountId),

  /** 打开该账号素材文件夹 */
  openMaterialFolder: (accountName: string): Promise<{ ok: boolean; error?: string }> =>
    ipcRenderer.invoke('account:openMaterialFolder', accountName),

  /** 上报浏览器视图位置（WebContentsView 是覆盖图层，位置由渲染进程测出） */
  setBounds: (rect: BoundsRect): void => {
    ipcRenderer.send('account:setBounds', rect)
  },

  getState: (): Promise<AccountViewState> => ipcRenderer.invoke('account:getState'),
  onStateChanged: (cb: (s: AccountViewState) => void): (() => void) =>
    subscribe('account:stateChanged', cb),
  onLoginStateChanged: (cb: (s: AccountLoginState) => void): (() => void) =>
    subscribe('account:loginStateChanged', cb),
  onNeedBounds: (cb: () => void): (() => void) => subscribe('account:needBounds', cb),
  onLoadFailed: (cb: (e: { code: number; desc: string; url: string }) => void): (() => void) =>
    subscribe('account:loadFailed', cb)
}

/** 千川账户登录态：独立分区（persist:qc-{id}），与抖音账号分区互不共用（A-12 多开隔离） */
const qianchuan = {
  /** 登录千川（该千川账户独立分区） */
  openLogin: (qcId: number): Promise<{ ok: boolean; url: string; error?: string }> =>
    ipcRenderer.invoke('qianchuan:openLogin', qcId),

  checkLogin: (qcId: number): Promise<AccountLoginState> =>
    ipcRenderer.invoke('qianchuan:checkLogin', qcId),

  exportForEngine: (qcId: number): Promise<AccountEngineSession> =>
    ipcRenderer.invoke('qianchuan:exportForEngine', qcId),

  clearLogin: (qcId: number): Promise<{ ok: boolean }> =>
    ipcRenderer.invoke('qianchuan:clearLogin', qcId)
}

/** 去水印辅助：抽视频帧 + 暂存用户框选的水印模板 */
const watermark = {
  /** 抽视频首帧 → PNG dataURL（供界面框选水印） */
  getFrame: (videoPath: string): Promise<{ ok: boolean; dataUrl?: string; error?: string }> =>
    ipcRenderer.invoke('watermark:getFrame', videoPath),

  /** 抽视频 N 个时间点的帧（半自动标注水印轨迹用） */
  getFrames: (
    videoPath: string,
    n: number
  ): Promise<{ ok: boolean; frames?: WmFrame[]; error?: string }> =>
    ipcRenderer.invoke('watermark:getFrames', videoPath, n),

  /** 把框选区域 dataURL 存为临时模板 PNG，返回路径（供 remove_watermark.py --template） */
  saveTemplate: (dataUrl: string): Promise<{ ok: boolean; path?: string; error?: string }> =>
    ipcRenderer.invoke('watermark:saveTemplate', dataUrl)
}

/** 去水印辅助用的一帧（时间/帧号/分辨率/dataURL） */
export interface WmFrame {
  t: number
  frame: number
  w: number
  h: number
  dataUrl: string
}

// ---------------------------------------------------------------- 工具

function subscribe<T>(channel: string, cb: (payload: T) => void): () => void {
  const listener = (_event: unknown, payload: T): void => cb(payload)
  ipcRenderer.on(channel, listener)
  return () => {
    ipcRenderer.removeListener(channel, listener)
  }
}

const api = {
  appInfo: {
    name: '投广管理客户端',
    version: '0.0.1'
  },
  browser,
  login,
  engine,
  dir,
  assets,
  baiying,
  toutiao,
  account,
  qianchuan,
  watermark
}

// ---------------------------------------------------------------- 类型（供 index.d.ts 复用）

export interface BrowserViewState {
  open: boolean
  targetId?: string
  url?: string
  title?: string
  loading?: boolean
  canGoBack?: boolean
  canGoForward?: boolean
}

export interface TargetLoginState {
  id: string
  name: string
  loggedIn: boolean
  matchedCookies: string[]
  cookieCount: number
  cookieNames: string[]
}

export interface LoginState {
  encryptionAvailable: boolean
  encryptionBackend: string
  targets: TargetLoginState[]
  /** 凭证加密备份的状态（只含元信息，不含 cookie 值） */
  credentialBackup: CredentialBackupMeta
  checkedAt: string
}

/** 凭证加密备份的可见元信息（B+ / T-16） */
export interface CredentialBackupMeta {
  exists: boolean
  updatedAt?: string
  cookieCount?: number
  error?: string
}

export interface EngineSession {
  cookieHeader: string
  userAgent: string
  cookieCount: number
  cookieNames: string[]
  targetId: string
  exportedAt: string
}

/** 视图位置矩形（CSS 像素） */
export interface BoundsRect {
  x: number
  y: number
  width: number
  height: number
}

/** 账号级会话主体：抖音账号（acct）或千川账户（qc），分区互不共用 */
export interface AccountSubject {
  kind: 'acct' | 'qc'
  id: number
  name: string
}

/** 账号级登录态（只含 cookie 名，不含值） */
export interface AccountLoginState {
  kind: 'acct' | 'qc'
  id: number
  loggedIn: boolean
  matchedCookies: string[]
  cookieCount: number
  cookieNames: string[]
  backupExists: boolean
  backupError?: string
  checkedAt: string
}

/** 账号级浏览器视图状态 */
export interface AccountViewState {
  open: boolean
  subject?: AccountSubject
  url?: string
  title?: string
  loading?: boolean
  canGoBack?: boolean
  canGoForward?: boolean
}

/** 「拉取信息」返回的账号公开信息 */
export interface AccountUserInfo {
  nickname: string
  followerCount: number
  awemeCount: number
}

/** 账号级导出给 Python 引擎的会话（cookieHeader 为明文，仅内存流转） */
export interface AccountEngineSession {
  cookieHeader: string
  userAgent: string
  cookieCount: number
  cookieNames: string[]
  subject: AccountSubject
  exportedAt: string
}

/** 引擎事件（主进程 → 渲染进程，engine:event） */
export interface EngineEventPayload {
  /** 所属任务 ID，与 run() 返回的 taskId 对应 */
  taskId: string
  type: 'stage' | 'log' | 'error' | 'result' | 'task'
  stage?: string
  percent?: number
  level?: 'info' | 'error'
  message?: string
  data?: unknown
  /** 队列任务事件（download_queue.py）：tid = 队列任务 ID */
  tid?: string
  /** 队列任务状态：pending / running / retrying / success / failed / skipped */
  status?: string
  /** 队列任务重试次数 */
  attempts?: number
  /** 队列任务落盘路径（success / skipped 时携带） */
  file_path?: string
}

/** engine:run 入参 */
export interface EngineRunParams {
  script: string
  args?: string[]
  /** 给引擎的 JSON（如登录态），走 stdin，不落盘 */
  stdinJson?: unknown
}

/** engine:run 返回值：立刻返回 taskId；终态走事件流 */
export interface EngineRunReply {
  taskId?: string
  ok?: boolean
  error?: string
}

/** engine:ping 返回值 */
export interface EnginePingResult {
  ok: boolean
  python?: string
  platform?: string
  requests?: string
  engine_dir?: string
  error?: string
}

/** baiying:batchStart 返回值 */
export interface BaiyingBatchStartReply {
  ok?: boolean
  taskId?: string
  error?: string
}

/** 批量抓取事件（主进程 → 渲染进程，baiying:event） */
export interface BaiyingBatchEvent {
  taskId: string
  type: 'stage' | 'product' | 'result' | 'error'
  /** stage：当前处理序号（0 起） */
  index?: number
  /** stage/result：总数 */
  total?: number
  /** stage：整体进度百分比 */
  percent?: number
  /** stage/product：当前商品 ID */
  productId?: string
  /** product：成功 / 失败 */
  status?: 'success' | 'failed'
  /** 人类可读说明 */
  message?: string
  /** result：成功 / 失败数 */
  success?: number
  failed?: number
  /** result：是否被用户取消 */
  canceled?: boolean
}

/** 头条批量解析事件（主进程 → 渲染进程，toutiao:event） */
export interface ToutiaoBatchEvent {
  taskId: string
  type: 'stage' | 'item' | 'result' | 'error'
  /** stage/item：当前序号（0 起） */
  index?: number
  /** stage/result：总数 */
  total?: number
  /** stage：整体进度百分比 */
  percent?: number
  /** stage/item：当前链接 */
  input?: string
  /** item：成功 / 失败 */
  status?: 'success' | 'failed'
  /** 人类可读说明 */
  message?: string
  /** result：成功 / 失败数 */
  success?: number
  failed?: number
  /** result：捕获到的直链列表 [{input, ok, vod_url, error}] */
  items?: Array<{ input: string; ok: boolean; vod_url: string | null; error: string | null }>
  /** result：是否被用户取消 */
  canceled?: boolean
}

// contextIsolated 为 true 时用 contextBridge 安全注入；否则直接挂 window（兜底）
if (process.contextIsolated) {
  try {
    contextBridge.exposeInMainWorld('electron', electronAPI)
    contextBridge.exposeInMainWorld('api', api)
  } catch (error) {
    console.error(error)
  }
} else {
  // @ts-ignore 类型声明见 index.d.ts
  window.electron = electronAPI
  // @ts-ignore 类型声明见 index.d.ts
  window.api = api
}