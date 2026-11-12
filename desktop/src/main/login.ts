import { BrowserWindow, WebContentsView, ipcMain, safeStorage, session, shell } from 'electron'
import type { Cookie } from 'electron'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { dirname, join } from 'path'
import { attachProbe } from './probe'
import { closeAutomationLaunchedBrowser, commandManagedBrowser, inspectManagedBrowserPages, openManagedBrowser, validateManagedProfilePath, type ManagedBrowserProgress } from './external_browser'
import { getClientPaths } from './app-dirs'
import {
  beginRegistryReservation,
  finishRegistryReservation,
  registerSelectionRegistryIpc,
  reserveRegistryCandidates,
  type RegistryCandidate
} from './selection_registry'
import { randomUUID } from 'crypto'
import { fireCartVideoDownload } from './baiying_batch'
import { runScript } from './engine'

const MAX_SELECTION_CANDIDATE_FALLBACKS = 50

/**
 * 抖音 / 百应 登录态管理与后台探针视图
 *
 * 设计要点
 *  1. 用 WebContentsView（Electron 30+ 正式 API）承载后台探针/自动化，不用于登录页展示
 *  2. 持久化靠 persist: 分区 —— 见 LOGIN_PARTITION 的注释
 *  3. 加密靠系统密钥（Windows DPAPI），由 Chromium 自己完成，我们不做二次明文落盘
 *  4. 交给 Python 引擎时走 stdin，cookie 值不落盘
 */

/**
 * 登录会话分区。
 *
 * `persist:` 前缀是关键 —— 有它 Chromium 才会把 cookie 落到磁盘，
 * 并在下次启动时自动恢复；没有前缀就是纯内存态，关掉应用就没了。
 *
 * 落盘位置：{userData}/Partitions/douyin-login/Cookies（SQLite）
 *
 * 加密说明：Chromium 写入前会把 cookie 值用「系统级密钥」加密再落盘
 *  - Windows：DPAPI（密钥绑定当前登录的 Windows 用户）
 *  - macOS：Keychain
 *  - Linux：kwallet / gnome-libsecret（无桌面环境时可能退化为固定密码）
 * 所以磁盘上不是明文。但要注意：**同一 Windows 用户下的任何程序都能解密**，
 * 这是 OS 级加密的边界，不是本应用的缺陷。
 */
export const LOGIN_PARTITION = 'persist:douyin-login'

/**
 * 登录凭证的加密备份文件（B+ 方案，T-16）。
 *
 * 背景：Electron 的 persist 分区把 cookie 明文落在 Chromium 自己的库里
 * （平台行为，无法通过注入密钥改变，方案 A 已验证失败）。
 * 所以这里在**应用层**把目标域 cookie 用 safeStorage（Windows DPAPI）加密后
 * 再落一份到我们自己的文件 —— 磁盘上的凭证明文不存在于业务链路，
 * 导出给引擎时也从内存读取，不依赖这份备份。
 *
 * 位置与 ad_client.db 同级（D 盘数据目录）。
 */
function credentialBackupPath(): string {
  return join(getClientPaths().dataDir, 'login_credential.enc')
}

/** 加密备份的可见元信息（不含任何 cookie 值） */
export interface CredentialBackupMeta {
  exists: boolean
  updatedAt?: string
  cookieCount?: number
  error?: string
}

/**
 * 登录目标。
 *
 * ⚠️ 百应入口实测（2026-10-12）：
 *   - 用户实际使用的入口：`buyin.jinritemai.com/dashboard?universal_page_params_id=…`（工作台）
 *   - 旧根路径 `buyin.jinritemai.com/` 与 `buyin.douyinec.com/` 均 404/落地页
 *   - domains 必须覆盖 jinritemai.com + douyinec.com + douyin.com，否则 cookie 导出会漏
 *
 * ⚠️ 分区（partition）设计（2026-10-14，罗盘接入）：
 *   - 多数目标共用 LOGIN_PARTITION（抖音/百应/小红书/头条——它们本来就是同一登录面板、同一账号生态）
 *   - 「罗盘」必须独立分区 `persist:compass-login`：罗盘登录态是 jinritemai.com 域 cookie，
 *     若混入共用分区会让百应的「已登录」判定与 cookie 导出把罗盘凭证一并带走（串号）。
 *     独立分区后各行其是，探针/批量等链路也互不污染。
 */
export const LOGIN_TARGETS = [
  {
    id: 'douyin',
    name: '抖音主站',
    url: 'https://www.douyin.com/',
    domains: ['douyin.com'],
    partition: LOGIN_PARTITION
  },
  {
    id: 'baiying',
    name: '巨量百应',
    url: 'https://buyin.jinritemai.com/dashboard?universal_page_params_id=a95eb4c6-75c7-4108-8334-12f7527a451c',
    domains: ['jinritemai.com', 'douyinec.com', 'douyin.com'],
    partition: LOGIN_PARTITION
  },
  {
    id: 'xiaohongshu',
    name: '小红书',
    url: 'https://www.xiaohongshu.com/',
    domains: ['xiaohongshu.com', 'xhslink.com'],
    partition: LOGIN_PARTITION
  },
  {
    id: 'toutiao',
    name: '今日头条/西瓜',
    url: 'https://www.toutiao.com/',
    domains: ['toutiao.com', 'ixigua.com', 'toutiaovod.com'],
    partition: LOGIN_PARTITION
  },
  {
    id: 'compass',
    name: '罗盘（电商罗盘）',
    url: 'https://compass.jinritemai.com/',
    domains: ['jinritemai.com', 'douyin.com'],
    partition: 'persist:compass-login'
  }
] as const

/**
 * 判定「已登录」的候选 cookie 名。
 *
 * 为什么用「候选名单」而不是一个固定值：抖音的登录态 cookie 名会随版本变化，
 * 写死一个容易失效。这里列出常见的几个，命中任一即视为已登录。
 *
 * 怎么自己确认哪个是真正生效的：登录前点一次「检查登录状态」，登录后再点一次，
 * 对比两次返回的 cookieNames，**新出现的那个就是登录凭证**。
 */
const LOGIN_COOKIE_CANDIDATES = ['sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'web_session']

// ---------------------------------------------------------------- 状态

let loginView: WebContentsView | null = null
let hostWindow: BrowserWindow | null = null
let viewAttached = false
let currentTargetId: string = LOGIN_TARGETS[0].id
/** 当前视图创建时用的分区 —— 分区在 WebContentsView 创建时定死，切目标换分区必须重建视图 */
let viewPartition: string = LOGIN_TARGETS[0].partition

// WebContentsView 是覆盖在页面之上的独立图层，位置必须由渲染进程告诉我们。
// 初始给个 0 尺寸，等渲染进程把真实位置发过来再显示，避免先闪一下。
let viewBounds = { x: 0, y: 0, width: 0, height: 0 }

// ---------------------------------------------------------------- 内部工具

/**
 * 取某登录目标专用的 session（换成对应分区）。
 *
 * 为什么按目标分区：罗盘是 jinritemai.com 域，与百应同域但需要独立分区隔离，
 * 否则罗盘登录态会混进百应的 cookie 导出 / 登录态判定（串号）。
 * 其余目标共用 LOGIN_PARTITION，行为与改造前完全一致。
 */
function targetSession(t: (typeof LOGIN_TARGETS)[number]): Electron.Session {
  return session.fromPartition(t.partition)
}

/** 同步获取（不使用 await）的分区名，供 view 创建时使用 */
function findTarget(id: string): (typeof LOGIN_TARGETS)[number] {
  return LOGIN_TARGETS.find((t) => t.id === id) ?? LOGIN_TARGETS[0]
}

function getHostWindow(): BrowserWindow | null {
  if (hostWindow && !hostWindow.isDestroyed()) return hostWindow
  const all = BrowserWindow.getAllWindows()
  return all.length > 0 ? all[0] : null
}

/** 筛选出属于某个目标的 cookie（按域名后缀匹配） */
function filterCookiesByTarget(all: Cookie[], domains: readonly string[]): Cookie[] {
  return all.filter((c) => {
    // Electron 类型里 domain 是可选的，先兜底再比较
    const domain = c.domain ?? ''
    return domains.some((d) => domain === d || domain.endsWith(d))
  })
}

/** 按 domain+path+name 去重 */
function dedupeCookies(list: Cookie[]): Cookie[] {
  const seen = new Set<string>()
  const out: Cookie[] = []
  for (const c of list) {
    const key = `${c.domain ?? ''}|${c.path ?? '/'}|${c.name}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(c)
  }
  return out
}

/** 拼成 HTTP 的 Cookie 请求头 */
function buildCookieHeader(list: Cookie[]): string {
  return list.map((c) => `${c.name}=${c.value}`).join('; ')
}

// ---------------------------------------------------------------- 外站链接外开（2026-10-14，用户要求）

/**
 * 平台域白名单：这些域内的常规跳转继续留在登录视图（登录/扫码/探针/批量链路需要），
 * 其余域名一律视为「外站」，用系统浏览器打开，避免把外站页面塞进软件内造成串号/混淆。
 */
const INTERNAL_NAV_DOMAINS = [
  'douyin.com',
  'jinritemai.com',
  'douyinec.com',
  'oceanengine.com',
  'xiaohongshu.com',
  'xhslink.com',
  'toutiao.com',
  'ixigua.com',
  'toutiaovod.com',
  'kwaixiaodian.com',
  'amemv.com'
]

/** 判断跳转目标是否为外站（非平台域白名单 = 外站；解析失败按外站处理） */
function isExternalNav(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase()
    return !INTERNAL_NAV_DOMAINS.some((d) => host === d || host.endsWith(`.${d}`))
  } catch {
    return true
  }
}

// ---------------------------------------------------------------- WebContentsView 管理

function ensureView(targetId: string = currentTargetId): WebContentsView {
  const target = findTarget(targetId)
  const wantPartition = target.partition

  // 目标分区与当前视图分区不同（如共用分区 ↔ 罗盘独立分区）→ 销毁重建，避免串号
  if (loginView && !loginView.webContents.isDestroyed()) {
    if (viewPartition === wantPartition) return loginView
    detachView()
    loginView.webContents.close()
    loginView = null
  }

  const view = new WebContentsView({
    webPreferences: {
      // 必须在创建时指定，之后再改无效
      partition: wantPartition,
      // 登录页是外部网站，绝不能给它任何 Node 能力
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  view.setBackgroundColor('#ffffff')

  const wc = view.webContents

  // 页面里的 target="_blank" 一律用系统浏览器打开（外站外开），不再在视图内加载
  wc.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url)
    return { action: 'deny' }
  })

  // 主框架跳到外站域名 → 拦截并转系统浏览器打开；平台域内跳转（登录/扫码/回跳）不受影响
  wc.on('will-navigate', (event, url) => {
    if (isExternalNav(url)) {
      event.preventDefault()
      void shell.openExternal(url)
    }
  })

  const refresh = (): void => {
    emitBrowserState()
  }

  wc.on('did-navigate', refresh)
  wc.on('did-navigate-in-page', refresh)
  wc.on('did-start-loading', refresh)
  wc.on('did-stop-loading', refresh)

  // 每次页面加载完成都自动查一次登录态，这样扫码成功后不用手动点按钮
  wc.on('did-finish-load', () => {
    emitBrowserState()
    void emitLoginState()
  })

  wc.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    // -3 是主动中断（ABORTED），用户点停止时也会触发，不算错误
    if (!isMainFrame || code === -3) return
    sendToRenderer('browser:loadFailed', { code, desc, url })
  })

  loginView = view
  viewPartition = wantPartition

  // L2 抓包探针（临时）：记录百应页面网络请求到 %TEMP%\baiying_probe.jsonl
  attachProbe(wc)

  return view
}

/** 把视图挂到主窗口并显示 */
function attachView(): void {
  const win = getHostWindow()
  if (!win) return

  const view = ensureView()

  if (!viewAttached) {
    win.contentView.addChildView(view)
    viewAttached = true

    // 窗口尺寸变化时，让渲染进程重新上报位置
    win.on('resize', () => sendToRenderer('browser:needBounds', {}))
  }

  hostWindow = win
  view.setBounds(viewBounds)
  view.setVisible(true)
}

/**
 * 确保登录视图存在（含探针挂载），供「百应批量抓取」「头条批量」驱动导航用。
 * 固定绑定到百应目标：批量驱动的是百应/头条页面，必须落在共用分区，
 * 不能受用户上次打开的「罗盘」目标影响（罗盘是独立分区，见 LOGIN_TARGETS 注释）。
 * 批量抓取**不调用 attachView** —— 视图保持未挂载状态，页面在后台加载，
 * 不影响用户在前台界面里查看抓取进度。
 */
export function ensureLoginView(): WebContentsView {
  return ensureView('baiying')
}

/**
 * 批量抓取专用：把登录视图以指定区域挂到窗口并显示。
 * 背景：视图若保持 0×0 未挂载，页面视口为零，详情组件可能因懒加载/
 * IntersectionObserver 不触发而**不发起 pack_detail**（实测证据：只有应用壳请求）。
 * 挂到窗口给真实视口后，用户也能看到浏览器正在被驱动。
 */
export function attachLoginViewAt(bounds: { x: number; y: number; width: number; height: number }): void {
  const win = getHostWindow()
  const view = ensureView()
  view.setBounds(bounds)
  if (win && !viewAttached) {
    win.contentView.addChildView(view)
    viewAttached = true
    win.on('resize', () => sendToRenderer('browser:needBounds', {}))
  }
  view.setVisible(true)
}

/** 批量抓取结束：把视图从窗口摘除（登录态保留） */
export function detachLoginView(): void {
  detachView()
}

/** 从主窗口移除视图（cookie 不受影响，它存在 session 里） */
function detachView(): void {
  const win = getHostWindow()
  const view = loginView
  if (!win || win.isDestroyed() || !view || view.webContents.isDestroyed()) return
  // 幂等：无论 viewAttached 标记如何，先强制隐藏再移除。
  // 之前实现被 viewAttached 守卫短路，导致第二次 close（如 WorkArea watch 兜底）
  // 不再执行 removeChildView，残留可见浏览器视图挡在界面上方。
  try {
    view.setVisible(false)
    win.contentView.removeChildView(view)
  } catch {
    /* 视图可能已被移除，忽略 */
  }
  viewAttached = false
}

/** 发给渲染进程（带窗口存活检查） */
function sendToRenderer(channel: string, payload: unknown): void {
  const win = getHostWindow()
  if (win && !win.isDestroyed()) {
    win.webContents.send(channel, payload)
  }
}

/** 上报当前浏览器视图状态 */
function emitBrowserState(): void {
  const view = loginView
  if (!view || view.webContents.isDestroyed()) {
    sendToRenderer('browser:stateChanged', { open: false })
    return
  }
  const wc = view.webContents
  sendToRenderer('browser:stateChanged', {
    open: viewAttached,
    targetId: currentTargetId,
    url: wc.getURL(),
    title: wc.getTitle(),
    loading: wc.isLoading(),
    canGoBack: wc.navigationHistory.canGoBack(),
    canGoForward: wc.navigationHistory.canGoForward()
  })
}

// ---------------------------------------------------------------- 登录态查询

export interface TargetLoginState {
  id: string
  name: string
  loggedIn: boolean
  matchedCookies: string[]
  cookieCount: number
  /** 只给 cookie 名字，**绝不返回值** —— 避免凭据被误传到界面或日志 */
  cookieNames: string[]
}

export interface LoginState {
  encryptionAvailable: boolean
  encryptionBackend: string
  targets: TargetLoginState[]
  /** 凭证加密备份的状态（B+ / T-16，只含元信息，不含 cookie 值） */
  credentialBackup: CredentialBackupMeta
  checkedAt: string
}

/**
 * 查询登录状态。
 *
 * 安全约定：返回值里**只包含 cookie 的名字和数量，不包含 cookie 的值**。
 * 值的导出只走单独一个通道（login:exportForEngine），且不落盘。
 */
export async function checkLoginState(): Promise<LoginState> {
  // 每个目标查自己分区的 cookie（罗盘是独立分区，其余共用 LOGIN_PARTITION）
  const targets: TargetLoginState[] = await Promise.all(
    LOGIN_TARGETS.map(async (t) => {
      const all = await targetSession(t).cookies.get({})
      const mine = filterCookiesByTarget(all, t.domains)
      const names = Array.from(new Set(mine.map((c) => c.name))).sort()
      const matched = LOGIN_COOKIE_CANDIDATES.filter((n) => names.includes(n))
      return {
        id: t.id,
        name: t.name,
        loggedIn: matched.length > 0,
        matchedCookies: matched,
        cookieCount: mine.length,
        cookieNames: names
      }
    })
  )

  // B+：任一目标已登录 → 把目标域 cookie 加密备份一份到磁盘（用该目标自己的分区）
  const loggedInTarget = targets.find((t) => t.loggedIn)
  const credentialBackup = loggedInTarget
    ? await saveCredentialBackup(
        targetSession(findTarget(loggedInTarget.id)),
        loggedInTarget
      )
    : { exists: false } satisfies CredentialBackupMeta

  return {
    encryptionAvailable: safeStorage.isEncryptionAvailable(),
    encryptionBackend: describeEncryptionBackend(),
    targets,
    credentialBackup,
    checkedAt: new Date().toISOString()
  }
}

function describeEncryptionBackend(): string {
  switch (process.platform) {
    case 'win32':
      return 'Windows DPAPI（密钥绑定当前 Windows 用户）'
    case 'darwin':
      return 'macOS Keychain（钥匙串）'
    default:
      return 'Linux 密钥服务（kwallet / gnome-libsecret）'
  }
}

// ---------------------------------------------------------------- 凭证加密备份（B+ / T-16）

/** 把 cookie 压成可序列化的精简 JSON（只留引擎需要的字段） */
function serializeCookies(list: Cookie[]): string {
  return JSON.stringify(
    list.map((c) => ({
      name: c.name,
      value: c.value,
      domain: c.domain ?? '',
      path: c.path ?? '/'
    }))
  )
}

/**
 * 登录成功后把目标域 cookie 用 safeStorage 加密落盘。
 * 失败不抛异常（登录流程不能被备份拖垮），原因放进返回值供 UI 显示。
 */
async function saveCredentialBackup(
  ses: Electron.Session,
  loggedInTarget: TargetLoginState
): Promise<CredentialBackupMeta> {
  try {
    if (!safeStorage.isEncryptionAvailable()) {
      return { exists: false, error: 'safeStorage 不可用（无法加密保存凭证）' }
    }
    const target = LOGIN_TARGETS.find((t) => t.id === loggedInTarget.id) ?? LOGIN_TARGETS[0]
    const all = await ses.cookies.get({})
    const mine = dedupeCookies(filterCookiesByTarget(all, target.domains))
    const enc = safeStorage.encryptString(serializeCookies(mine))
    const backupPath = credentialBackupPath()
    mkdirSync(dirname(backupPath), { recursive: true })
    writeFileSync(backupPath, enc)
    return {
      exists: true,
      updatedAt: new Date().toISOString(),
      cookieCount: mine.length
    }
  } catch (error) {
    return { exists: false, error: String(error) }
  }
}

/** 删除加密备份（登出时调用） */
function removeCredentialBackup(): void {
  try {
    rmSync(credentialBackupPath(), { force: true })
  } catch {
    /* 删除失败不致命 */
  }
}

/** 查询登录态并推送给渲染进程 */
async function emitLoginState(): Promise<void> {
  try {
    sendToRenderer('login:stateChanged', await checkLoginState())
  } catch {
    // 查询失败不致命，静默即可（界面仍可手动点「检查登录状态」）
  }
}

// ---------------------------------------------------------------- 交给 Python 引擎

export interface EngineSession {
  cookieHeader: string
  userAgent: string
  cookieCount: number
  cookieNames: string[]
  targetId: string
  exportedAt: string
}

/**
 * 导出登录态给 Python 引擎用。
 *
 * ⚠️ 为什么必须连 User-Agent 一起导出：
 * 抖音的风控会把登录态和 UA 关联。如果登录用的是桌面 Chrome 的 UA，
 * 而 Python 请求时用 iPhone UA，服务端可能判定会话异常并失效。
 * 所以引擎发请求时必须同时带上这里返回的 cookieHeader 和 userAgent。
 *
 * ⚠️ 这个函数的返回值含 cookie 明文，只在内存里流转，**不要写进日志或文件**。
 */
export async function exportForEngine(targetId: string): Promise<EngineSession> {
  const target = findTarget(targetId)
  const ses = targetSession(target)
  const all = await ses.cookies.get({})
  const mine = dedupeCookies(filterCookiesByTarget(all, target.domains))

  const view = ensureView()
  const userAgent = view.webContents.getUserAgent()

  return {
    cookieHeader: buildCookieHeader(mine),
    userAgent,
    cookieCount: mine.length,
    cookieNames: mine.map((c) => c.name).sort(),
    targetId: target.id,
    exportedAt: new Date().toISOString()
  }
}

/** 清空登录态（登出）：按目标逐个清各自分区，罗盘独立分区一并清干净 */
async function clearLoginState(): Promise<void> {
  for (const t of LOGIN_TARGETS) {
    const ses = targetSession(t)
    const all = await ses.cookies.get({})
    for (const c of filterCookiesByTarget(all, t.domains)) {
      const scheme = c.secure ? 'https' : 'http'
      const rawDomain = c.domain ?? ''
      const host = rawDomain.startsWith('.') ? rawDomain.slice(1) : rawDomain
      if (!host) continue
      try {
        await ses.cookies.remove(`${scheme}://${host}${c.path ?? '/'}`, c.name)
      } catch {
        // 单条删除失败不影响整体
      }
    }
    await ses.clearStorageData({ storages: ['localstorage', 'cachestorage'] })
  }
  // B+：登出时一并删除加密备份，避免残留过期凭证副本
  removeCredentialBackup()
  await emitLoginState()
}

// ---------------------------------------------------------------- IPC 注册

async function cacheVerifiedCartCommissions(
  accountId: number,
  completed: Array<Record<string, unknown>>
): Promise<string[]> {
  const warnings: string[] = []
  for (const item of completed) {
    const productId = String(item.productId ?? '').trim()
    const sources = item.metricSources as Record<string, unknown> | undefined
    const metrics = item.dataMetrics as Record<string, unknown> | undefined
    const rate = metrics?.commissionRate
    if (item.cartAdded !== true || !/^\d{8,20}$/.test(productId) ||
        sources?.commissionRate !== '商品详情' || typeof rate !== 'number' ||
        !Number.isFinite(rate) || rate <= 0 || rate > 100) continue
    try {
      const outcome = await runScript('baiying_cache.py', {
        internal: true,
        args: [
          '--db', getClientPaths().databasePath, 'upsert',
          '--product-id', productId, '--account-id', String(accountId),
          '--cos-ratio', String(rate)
        ],
        onEvent: () => undefined
      }).promise
      if (!outcome.ok) throw new Error(outcome.error ?? '佣金率缓存失败')
    } catch (error) {
      const warning = `${productId}的佣金率未能保存：${error instanceof Error ? error.message : String(error)}`
      warnings.push(warning)
      console.warn(`[自动选品] ${warning}`)
    }
  }
  return warnings
}

export function registerLoginIpc(): void {
  registerSelectionRegistryIpc()
  // 登录目标清单（渲染进程用它渲染下拉框，避免两边各写一份导致漂移）
  ipcMain.handle('login:getTargets', () =>
    LOGIN_TARGETS.map((t) => ({ id: t.id, name: t.name, url: t.url }))
  )

  // 所有登录页均在客户端托管的外部 Chrome profile 打开，并通过 CDP 连接自动化。
  ipcMain.handle('browser:open', async (_e, targetId: string, options?: { profileKey?: string; profilePath?: string }) => {
    const target = findTarget(targetId)
    currentTargetId = target.id
    detachView()
    const result = await openManagedBrowser(target.url, {
      profileKey: options?.profileKey ?? 'general',
      profilePath: options?.profilePath
    })
    emitBrowserState()
    return result
  })

  ipcMain.handle('browser:validateProfilePath', (_e, options: { profileKey: string; profilePath?: string }) => {
    if (!options || typeof options.profileKey !== 'string') return { ok: false, error: '缺少账号 profile 标识' }
    try {
      return {
        ok: true,
        path: validateManagedProfilePath(options.profileKey, options.profilePath)
      }
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })

  ipcMain.handle('browser:inspectBaiyingSelection', async (_e, options: { profileKey: string; profilePath?: string; includeCategoryTree?: boolean }) => {
    if (!options || typeof options.profileKey !== 'string') throw new Error('缺少百应账号浏览器 profile')
    return commandManagedBrowser('inspect-selection', { includeCategoryTree: options.includeCategoryTree === true }, options)
  })

  ipcMain.handle('browser:checkAccountLogins', async (_e, params: { accountId: number; profilePath?: string; closeAfterAutomation?: boolean }) => {
    if (!params || !Number.isInteger(params.accountId) || params.accountId < 1) {
      throw new Error('登录预检账号 ID 无效')
    }
    const options = {
      profileKey: `account-${params.accountId}`,
      profilePath: params.profilePath
    }
    const result = await commandManagedBrowser('check-account-logins', {
      accountId: params.accountId
    }, options)
    const loginConfirmed = [result['baiying'], result['creatorCenter']].every((item) =>
      item && typeof item === 'object' && (item as Record<string, unknown>)['state'] === 'logged_in'
    )
    if (params.closeAfterAutomation === true && loginConfirmed) {
      try {
        await closeAutomationLaunchedBrowser(options)
      } catch (error) {
        // 登录状态结果优先返回；窗口清理失败只记日志，不把已确认登录误报为检查失败。
        console.warn(`[登录预检] 账号 ${params.accountId} 的浏览器未能自动关闭：${String(error)}`)
      }
    }
    return result
  })

  ipcMain.handle('browser:inspectAccountPages', async (_e, params: { accountId: number; profilePath?: string }) => {
    if (!params || !Number.isInteger(params.accountId) || params.accountId < 1) {
      throw new Error('浏览器页面检查账号 ID 无效')
    }
    return inspectManagedBrowserPages({
      profileKey: `account-${params.accountId}`,
      profilePath: params.profilePath
    })
  })

  ipcMain.handle('browser:runBaiyingSelection', async (event, params: {
    profileKey: string
    profilePath?: string
    settings: Record<string, unknown>
    accountId: number
    accountKey?: string
    downloadPath: string
    selectedIndexes?: number[]
    candidateSnapshot?: unknown[]
    allowCandidateFallback?: boolean
    recoverExistingCart?: boolean
    refreshCandidatesById?: boolean
    analyzeBeforeCart?: boolean
    maxSelectedProducts?: number
    stopAfterCart?: boolean
    previewOnly?: boolean
    closeAfterAutomation?: boolean
  }) => {
    if (!params || typeof params.profileKey !== 'string' || !Number.isInteger(params.accountId) || params.accountId < 1) {
      throw new Error('百应选品参数无效')
    }
    if (!params.settings || typeof params.settings !== 'object' || typeof params.downloadPath !== 'string') {
      throw new Error('百应选品设置或下载目录无效')
    }
    const browserOptions = { profileKey: params.profileKey, profilePath: params.profilePath }
    const onProgress = (progress: ManagedBrowserProgress): void => {
      if (!event.sender.isDestroyed()) {
        event.sender.send('browser:selectionProgress', { accountId: params.accountId, ...progress })
      }
    }
    const runManagedSelection = async (data: Record<string, unknown>): Promise<Record<string, unknown>> => {
      const result = await commandManagedBrowser('run-selection', data, browserOptions, onProgress)
      if (params.closeAfterAutomation === true) {
        try {
          await closeAutomationLaunchedBrowser(browserOptions)
        } catch (error) {
          // 不用浏览器清理失败覆盖已完成的加车结果；下次账号切换时可继续复用该 profile。
          console.warn(`[自动选品] 账号 ${params.accountId} 的浏览器未能自动关闭：${String(error)}`)
        }
      }
      return result
    }
    if (params.previewOnly === true) {
      return runManagedSelection(params)
    }

    const accountKey = `local:${params.accountId}`
    const snapshot = Array.isArray(params.candidateSnapshot) ? params.candidateSnapshot as Array<Record<string, unknown>> : []
    const selected = Array.isArray(params.selectedIndexes) ? params.selectedIndexes : []
    if (!snapshot.length || !selected.length) throw new Error('本次加车缺少候选快照；本机登记前已停止平台操作。')
    const rowByIndex = new Map<number, Record<string, unknown>>()
    for (const candidate of snapshot) {
      const cardIndex = Number(candidate.cardIndex)
      if (Number.isInteger(cardIndex) && !rowByIndex.has(cardIndex)) rowByIndex.set(cardIndex, candidate)
    }
    const selectedRows = selected.map((cardIndex) => rowByIndex.get(cardIndex)).filter(
      (candidate): candidate is Record<string, unknown> => Boolean(candidate)
    )
    if (selectedRows.length !== selected.length || new Set(selected).size !== selected.length) {
      throw new Error('候选索引与商品快照无法一一对应；本机登记前已停止平台操作。')
    }
    if (params.recoverExistingCart === true) {
      if (selectedRows.length !== 1 || selectedRows.some((candidate) => candidate.cartAdded !== true)) {
        throw new Error('详情恢复只允许读取一个已确认加车的原商品；本次未执行加车。')
      }
      // 商品已在前序阶段确认加车；这里只恢复详情素材，不重新占用登记或发起第二次加车。
      const result = await runManagedSelection(params)
      const completed = Array.isArray(result.completed) ? result.completed as Array<Record<string, unknown>> : []
      return { ...result, commissionCacheWarnings: await cacheVerifiedCartCommissions(params.accountId, completed) }
    }
    const maxSelected = Number(params.maxSelectedProducts)
    const fallbackRows = params.allowCandidateFallback === true
      ? snapshot.filter((candidate) =>
        !selected.includes(candidate.cardIndex as number) &&
        candidate.cartAttempted !== true && candidate.cartAdded !== true &&
        candidate.skipped !== true && candidate.analysisRejected !== true
      )
      : []
    const orderedRows = [...selectedRows, ...fallbackRows].slice(0, MAX_SELECTION_CANDIDATE_FALLBACKS)
    const reservationLimit = Number.isFinite(maxSelected) && maxSelected > 0
      ? Math.min(orderedRows.length, Math.max(1, Math.ceil(maxSelected * 3)))
      : Math.min(orderedRows.length, selectedRows.length)
    const categoryModeRequiresId = params.settings.selectionMode === 'category'
    const candidatesToReserve: Array<{ candidate: Record<string, unknown>; item: RegistryCandidate }> = []
    const rejected: Array<{ productId: string; reason: string }> = []
    const seenProductIds = new Set<string>()
    const selectedProductIds = new Set(selectedRows.map((candidate) => String(candidate.productId ?? '').trim()))
    for (const candidate of orderedRows) {
      const productId = String(candidate.productId ?? '').trim()
      if (!productId) {
        rejected.push({ productId: '', reason: '候选缺少商品 ID，已跳过并尝试其他候选' })
        continue
      }
      if (seenProductIds.has(productId)) {
        rejected.push({ productId, reason: '本轮候选池中商品 ID 重复，已跳过重复项' })
        continue
      }
      seenProductIds.add(productId)
      const path = Array.isArray(candidate.categoryIds) ? candidate.categoryIds.map(String).filter(Boolean) : []
      const leafCategoryId = categoryModeRequiresId
        ? String(candidate.leafCategoryId ?? path.at(-1) ?? '').trim()
        : ''
      if (categoryModeRequiresId && !leafCategoryId) {
        rejected.push({ productId, reason: '候选缺少百应末级类目 ID，已跳过并尝试其他候选' })
        continue
      }
      candidatesToReserve.push({ candidate, item: {
        productId,
        leafCategoryId,
        title: String(candidate.title ?? '').slice(0, 512)
      } })
    }
    const reservationRequestId = randomUUID()
    const registryResult = candidatesToReserve.length
      ? await reserveRegistryCandidates(
        accountKey,
        reservationRequestId,
        candidatesToReserve.map(({ item }) => item),
        reservationLimit
      )
      : { reserved: [], rejected: [], mode: 'local' as const }
    rejected.push(...registryResult.rejected)
    const tokenByProduct = new Map(registryResult.reserved.map((item) => [item.productId, item.token]))
    const reservedRows = candidatesToReserve
      .map(({ candidate }) => candidate)
      .filter((candidate) => tokenByProduct.has(String(candidate.productId ?? '').trim()))
    const reservationResult = { ...registryResult, rejected, mode: 'local' as const }
    const skippedRegistryRows = (completed: Array<Record<string, unknown>> = []): Array<Record<string, unknown>> => {
      const completedIds = new Set(completed.map((item) => String(item.productId ?? '').trim()).filter(Boolean))
      return reservationResult.rejected.flatMap((item) => {
        const productId = String(item.productId ?? '').trim()
        if (productId && completedIds.has(productId)) return []
        const candidate = snapshot.find((row) => String(row.productId ?? '').trim() === productId)
        return [{
          ...(candidate ?? {}),
          productId: productId || String(candidate?.productId ?? ''),
          cartAdded: false,
          cartAttempted: false,
          skipped: true,
          registryExcluded: true,
          status: `本机登记未通过，已跳过并继续后续候选：${item.reason}`
        }]
      })
    }
    if (!reservedRows.length) {
      if (!reservationResult.rejected.length) {
        throw new Error('本机登记没有返回可执行候选，也没有提供跳过原因；本次未向百应发送加车命令。')
      }
      // 全部候选都因重复登记、类目互斥或缺少 ID 被拒绝时，这是可解释的跳过结果，
      // 不应把账号留在待恢复状态；renderer 会记录这些跳过项并继续批次中的后续账号。
      const completed = skippedRegistryRows()
      return {
        ok: true,
        completed,
        analysisAttemptedCount: 0,
        completionCount: completed.length,
        registryRejected: reservationResult.rejected.map((item) => ({ ...item })),
        registryReplacementCount: 0,
        registrySettlementWarnings: [],
        registryReleaseEvents: []
      }
    }
    const reservedIndexes = reservedRows.map((candidate) => candidate.cardIndex as number)
    try {
      for (const candidate of reservedRows) {
        const token = tokenByProduct.get(String(candidate.productId ?? ''))
        if (token) await beginRegistryReservation(token)
      }
    } catch (error) {
      const cleanup: string[] = []
      for (const candidate of reservedRows) {
        const token = tokenByProduct.get(String(candidate.productId ?? ''))
        if (!token) continue
        try {
          await finishRegistryReservation(token, 'NOT_ADDED')
        } catch (releaseError) {
          cleanup.push(String(releaseError))
        }
      }
      throw new Error(`本机登记预留状态无法确认，未向百应发送加车命令。${cleanup.length ? `部分预留仍需核查：${cleanup[0]}。` : ''}${error instanceof Error ? error.message : String(error)}`)
    }
    const runParams = { ...params, selectedIndexes: reservedIndexes }
    try {
      const result = await runManagedSelection(runParams)
      const completed = Array.isArray(result.completed) ? result.completed as Array<Record<string, unknown>> : []
      const completedById = new Map(completed.map((item) => [String(item.productId ?? ''), item]))
      const settlementWarnings: string[] = []
      const confirmedDownloads: Array<{ productId: string; commodityId: string }> = []
      const registryReleaseEvents: Array<{
        productId: string
        outcome: 'NOT_ADDED'
        state: 'RELEASED'
        reason: 'control failure'
        diagnosticPath: string
        settled: boolean
      }> = []
      for (const candidate of reservedRows) {
        const productId = String(candidate.productId ?? '')
        const token = tokenByProduct.get(productId)
        if (!token) continue
        const item = completedById.get(productId)
        // 只有明确标记为“点击前控件未命中”才允许释放预留。任何结果不明
        // 都必须保留为 UNCERTAIN，避免实际已加车后被下一账号再次选中。
        const controlFailure = item?.cartFailureClass === 'CONTROL_FAILURE' &&
          item.cartAttempted !== true && item.skipped === true &&
          /加车前控件定位失败|未点击加车/.test(String(item.status ?? ''))
        const outcome = item?.cartAdded === true
          ? 'ADDED'
          : controlFailure || item?.cartAttempted !== true
            ? 'NOT_ADDED'
            : 'UNKNOWN'
        const releaseEvent = controlFailure ? {
          productId,
          outcome: 'NOT_ADDED' as const,
          state: 'RELEASED' as const,
          reason: 'control failure' as const,
          diagnosticPath: String(item?.controlDiagnosticPath ?? ''),
          settled: false
        } : null
        if (releaseEvent) registryReleaseEvents.push(releaseEvent)
        try {
          await finishRegistryReservation(token, outcome)
          if (releaseEvent) releaseEvent.settled = true
          if (outcome === 'ADDED' && item?.cartAdded === true && /^\d{8,20}$/.test(productId)) {
            confirmedDownloads.push({ productId, commodityId: String(item.commodityId ?? candidate.commodityId ?? '') })
          }
        } catch (error) {
          settlementWarnings.push(`${productId}本机登记结算暂未确认：${error instanceof Error ? error.message : String(error)}`)
        }
      }
      for (const item of confirmedDownloads) {
        fireCartVideoDownload(params.accountId, item.productId, item.commodityId)
      }
      const commissionCacheWarnings = await cacheVerifiedCartCommissions(params.accountId, completed)
      const rejected = reservationResult.rejected.map((item) => ({ ...item }))
      return {
        ...result,
        completed: [...completed, ...skippedRegistryRows(completed)],
        registryRejected: rejected,
        registryReplacementCount: reservedRows.filter((candidate) =>
          !selectedProductIds.has(String(candidate.productId ?? '').trim())
        ).length,
        registrySettlementWarnings: settlementWarnings,
        commissionCacheWarnings,
        registryReleaseEvents
      }
    } catch (error) {
      const warnings: string[] = []
      for (const candidate of reservedRows) {
        const token = tokenByProduct.get(String(candidate.productId ?? ''))
        if (!token) continue
        try {
          await finishRegistryReservation(token, 'UNKNOWN')
        } catch (settlementError) {
          warnings.push(String(settlementError))
        }
      }
      const reason = error instanceof Error ? error.message : String(error)
      throw new Error(`百应执行结果不确定；本机登记已将本批预留保留为待核查，禁止重复选品。${warnings.length ? ` 本机登记结算也未确认：${warnings[0]}` : ''} ${reason}`)
    }
  })

  ipcMain.handle('browser:prepareDouyinPublish', async (event, params: {
    accountId: number
    queueId?: number
    profilePath?: string
    productId: string
    productName: string
    productUrl: string
    videoPath: string
    description: string
    autoPublish?: boolean
  }) => {
    if (!params || !Number.isInteger(params.accountId) || params.accountId < 1) {
      throw new Error('创作者中心上传账号 ID 无效')
    }
    if (
      typeof params.productId !== 'string' || !params.productId.trim() ||
      typeof params.productName !== 'string' || !params.productName.trim() ||
      typeof params.productUrl !== 'string' || !params.productUrl.trim() ||
      typeof params.videoPath !== 'string' || !params.videoPath.trim() ||
      typeof params.description !== 'string' || !params.description.trim() ||
      params.description.length > 500 ||
      (params.queueId !== undefined && (!Number.isInteger(params.queueId) || params.queueId < 1)) ||
      (params.autoPublish !== undefined && typeof params.autoPublish !== 'boolean')
    ) {
      throw new Error('创作者中心上传参数无效，需提供商品、MP4 文件和简介')
    }
    return commandManagedBrowser('prepare-douyin-publish', {
      accountId: params.accountId,
      ...(params.queueId ? { queueId: params.queueId } : {}),
      productId: params.productId,
      productName: params.productName,
      productUrl: params.productUrl,
      videoPath: params.videoPath,
      description: params.description,
      selectionSettingsDbPath: getClientPaths().databasePath,
      autoPublish: params.autoPublish === true
    }, {
      profileKey: `account-${params.accountId}`,
      profilePath: params.profilePath
    }, (progress) => {
      if (!event.sender.isDestroyed()) {
        event.sender.send('browser:publishProgress', {
          accountId: params.accountId,
          queueId: params.queueId,
          state: 'running',
          updatedAt: new Date().toISOString(),
          ...progress
        })
      }
    })
  })

  ipcMain.handle('browser:submitDouyinPublish', async (event, params: {
    accountId: number
    queueId: number
    profilePath?: string
    productId: string
    productName: string
    productUrl: string
    videoPath: string
    description: string
  }) => {
    if (!params || !Number.isInteger(params.accountId) || params.accountId < 1) {
      throw new Error('创作者中心账号 ID 无效')
    }
    if (
      !Number.isInteger(params.queueId) || params.queueId < 1 ||
      typeof params.productId !== 'string' || !params.productId.trim() ||
      typeof params.productName !== 'string' || !params.productName.trim() ||
      typeof params.productUrl !== 'string' || !params.productUrl.trim() ||
      typeof params.videoPath !== 'string' || !params.videoPath.trim() ||
      typeof params.description !== 'string' || !params.description.trim() ||
      params.description.length > 500
    ) {
      throw new Error('抖音最终发布参数无效')
    }
    return commandManagedBrowser('submit-douyin-publish', {
      accountId: params.accountId,
      queueId: params.queueId,
      productId: params.productId,
      productName: params.productName,
      productUrl: params.productUrl,
      videoPath: params.videoPath,
      description: params.description,
      selectionSettingsDbPath: getClientPaths().databasePath
    }, {
      profileKey: `account-${params.accountId}`,
      profilePath: params.profilePath
    }, (progress) => {
      if (!event.sender.isDestroyed()) {
        event.sender.send('browser:publishProgress', {
          accountId: params.accountId,
          queueId: params.queueId,
          state: 'running',
          updatedAt: new Date().toISOString(),
          ...progress
        })
      }
    })
  })

  ipcMain.handle('browser:openDouyinCreator', async (_e, params: { accountId: number; profilePath?: string }) => {
    if (!params || !Number.isInteger(params.accountId) || params.accountId < 1) {
      throw new Error('创作者中心账号 ID 无效')
    }
    return commandManagedBrowser('open-douyin-creator', {
      accountId: params.accountId
    }, {
      profileKey: `account-${params.accountId}`,
      profilePath: params.profilePath
    })
  })

  // 关闭视图（cookie 保留）
  ipcMain.handle('browser:close', () => {
    detachView()
    emitBrowserState()
    return { ok: true }
  })

  // 同步位置（渲染进程在挂载、缩放、窗口 resize 时上报）
  ipcMain.on('browser:setBounds', (_e, rect: { x: number; y: number; width: number; height: number }) => {
    viewBounds = {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.max(0, Math.round(rect.width)),
      height: Math.max(0, Math.round(rect.height))
    }
    if (loginView && viewAttached) {
      loginView.setBounds(viewBounds)
    }
  })

  // 导航
  ipcMain.handle('browser:goBack', () => {
    const wc = loginView?.webContents
    if (wc?.navigationHistory.canGoBack()) wc.navigationHistory.goBack()
    return { ok: true }
  })

  ipcMain.handle('browser:goForward', () => {
    const wc = loginView?.webContents
    if (wc?.navigationHistory.canGoForward()) wc.navigationHistory.goForward()
    return { ok: true }
  })

  ipcMain.handle('browser:reload', () => {
    loginView?.webContents.reload()
    return { ok: true }
  })

  // 检查登录状态
  ipcMain.handle('login:check', async () => checkLoginState())

  // 登出
  ipcMain.handle('login:clear', async () => {
    await clearLoginState()
    return { ok: true }
  })

  // 导出登录态给 Python 引擎（返回值含明文，不落盘）
  ipcMain.handle('login:exportForEngine', async (_e, targetId: string) =>
    exportForEngine(targetId)
  )

  // 渲染进程主动要求刷新一次浏览器状态
  ipcMain.handle('browser:getState', () => {
    const wc = loginView?.webContents
    if (!wc || wc.isDestroyed()) return { open: false }
    return {
      open: viewAttached,
      targetId: currentTargetId,
      url: wc.getURL(),
      title: wc.getTitle(),
      loading: wc.isLoading(),
      canGoBack: wc.navigationHistory.canGoBack(),
      canGoForward: wc.navigationHistory.canGoForward()
    }
  })
}

/** 应用退出前清理 */
export function disposeLogin(): void {
  detachView()
  if (loginView && !loginView.webContents.isDestroyed()) {
    loginView.webContents.close()
  }
  loginView = null
  viewPartition = LOGIN_TARGETS[0].partition
}
