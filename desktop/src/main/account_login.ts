import { BrowserWindow, WebContentsView, ipcMain, safeStorage, session, shell } from 'electron'
import { openManagedBrowser } from './external_browser'
import type { Cookie, Session } from 'electron'
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'fs'
import { join, dirname } from 'path'
import { LOGIN_TARGETS } from './login'
import { getClientPaths } from './app-dirs'

/**
 * 账号级客户端缓存隔离（Step 11，SRS FR-G5-01 / FR-C1-01 / A-12）。
 * 登录页由客户端启动的外部 Chrome profile 打开，并由 Playwright 通过本机 CDP 连接；
 * 该 profile 与这里的 Electron 分区仍相互独立。
 * ─────────────────────────────────────────────────────────────
 * 设计要点
 *  1. 每抖音账号一个独立 Chromium 分区 `persist:acct-{id}`，每千川账户一个独立分区
 *     `persist:qc-{id}` —— cookie / localStorage 物理隔离，切换主体即重建视图换分区，
 *     天然不串号。
 *  2. 一账号一指纹（FR-G7-01）：每个分区注入**确定性** canvas 噪声（种子由主体 id 派生，
 *     同一主体每次一致、不同主体不同），配合独立分区，构成可用的指纹隔离。
 *  3. 加密落盘：登录后把目标域 cookie 用 safeStorage（Windows DPAPI）加密写独立备份文件
 *     （每账号/千川一份），磁盘无明文；导出给引擎走内存 → stdin，不落盘。
 *  4. 本模块只服务「账号管理」页；原有 login.ts 的全局登录视图（百应批量/头条批量依赖）
 *     不受影响。
 */

/** 抖音登录态判定用的候选 cookie 名（与 login.ts 同口径，命中任一视为已登录） */
const LOGIN_COOKIE_CANDIDATES = ['sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'web_session']

/** 千川后台入口（域名覆盖 jinritemai.com + oceanengine.com） */
const QC_LOGIN_URL = 'https://qianchuan.jinritemai.com/dashboard'
const QC_DOMAINS = ['jinritemai.com', 'oceanengine.com']

/**
 * 外站链接外开（2026-10-14，用户要求）：账号分区视图内点击跳外站的链接
 * 一律在外部托管 Chrome 打开，避免外站页面混入登录视图造成串号/混淆。
 * 平台域白名单内的正常跳转（登录/扫码/回跳）仍留在视图内。
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

function isExternalNav(url: string): boolean {
  try {
    const host = new URL(url).hostname.toLowerCase()
    return !INTERNAL_NAV_DOMAINS.some((d) => host === d || host.endsWith(`.${d}`))
  } catch {
    return true
  }
}

/** 会话主体：账号（抖音）或千川账户，二者分区互不共用（多开隔离） */
export type SessionSubject =
  | { kind: 'acct'; id: number; name: string }
  | { kind: 'qc'; id: number; name: string }

function partitionOf(subject: SessionSubject): string {
  return subject.kind === 'acct' ? `persist:acct-${subject.id}` : `persist:qc-${subject.id}`
}

/** 指纹备份文件路径 */
function credentialPath(subject: SessionSubject): string {
  const tag = subject.kind === 'acct' ? `acct_${subject.id}` : `qc_${subject.id}`
  return join(getClientPaths().dataDir, `login_credential_${tag}.enc`)
}

/** 指纹种子：由主体 id 派生，稳定且互异（用于 canvas 噪声注入） */
function fingerprintSeed(subject: SessionSubject): number {
  const base = subject.kind === 'acct' ? 0x9e3779b9 : 0x85ebca6b
  return ((subject.id * 2654435761 + base) >>> 0) % 0xffffff
}

// ---------------------------------------------------------------- 视图状态

let currentView: WebContentsView | null = null
let currentSubject: SessionSubject | null = null
let viewAttached = false
let hostWindow: BrowserWindow | null = null
let viewBounds = { x: 0, y: 0, width: 0, height: 0 }

function getHostWindow(): BrowserWindow | null {
  if (hostWindow && !hostWindow.isDestroyed()) return hostWindow
  const all = BrowserWindow.getAllWindows()
  return all.length > 0 ? all[0] : null
}

function sendToRenderer(channel: string, payload: unknown): void {
  const win = getHostWindow()
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload)
}

// ---------------------------------------------------------------- 指纹注入

/**
 * 一账号一指纹（FR-G7-01）：canvas 噪声。
 * 通过 executeJavaScript 在主世界覆盖 toDataURL/getImageData，按种子做确定性偏移，
 * 使不同主体的 canvas 指纹不同、同一主体稳定。dom-ready 后注入一次即可
 * （prototype 修改对同渲染进程后续页面持续生效）。
 */
function attachFingerprint(wc: Electron.WebContents, seed: number): void {
  const script = `
    (() => {
      if (window.__acctFpApplied__) return;
      window.__acctFpApplied__ = true;
      const seed = ${seed};
      // 确定性伪随机：同一种子输出序列恒定
      let state = seed | 1;
      const next = () => { state ^= state << 13; state ^= state >>> 17; state ^= state << 5; return state >>> 0; };
      const offset = (next() % 254) + 1;
      const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
      const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
      HTMLCanvasElement.prototype.toDataURL = function (...args) {
        const out = origToDataURL.apply(this, args);
        // 在 dataURL 末尾追加一段可见的注释块，改变 hash 值（不影响渲染）
        const pad = ';fp=' + (seed.toString(16));
        return out.length > 50 ? out.slice(0, -1) + pad + out.slice(-1) : out;
      };
      CanvasRenderingContext2D.prototype.getImageData = function (...args) {
        const img = origGetImageData.apply(this, args);
        const data = img.data;
        for (let i = 0; i < data.length; i += 4 * 89) {
          data[i] = (data[i] + offset) & 0xff;
        }
        return img;
      };
    })()
  `
  wc.executeJavaScript(script, true).catch(() => undefined)
}

// ---------------------------------------------------------------- 视图管理

function destroyView(): void {
  if (currentView && !currentView.webContents.isDestroyed()) {
    currentView.webContents.close()
  }
  currentView = null
  viewAttached = false
}

/** 确保视图绑定到指定主体（换主体会销毁旧视图重建 —— 分区在创建时不可改） */
function ensureView(subject: SessionSubject): WebContentsView {
  if (currentView && !currentView.webContents.isDestroyed()) {
    if (currentSubject && sameSubject(currentSubject, subject)) return currentView
    detachView()
    destroyView()
  }

  const view = new WebContentsView({
    webPreferences: {
      partition: partitionOf(subject),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false
    }
  })
  view.setBackgroundColor('#ffffff')
  const wc = view.webContents

  // 页面里的 target="_blank" 一律交给系统浏览器打开（外站外开），不在视图内加载
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

  wc.on('did-navigate', () => emitViewState())
  wc.on('did-navigate-in-page', () => emitViewState())
  wc.on('did-start-loading', () => emitViewState())
  wc.on('did-stop-loading', () => emitViewState())

  wc.on('dom-ready', () => attachFingerprint(wc, fingerprintSeed(subject)))

  // 登录成功自动上报，界面免手动刷新
  wc.on('did-finish-load', () => {
    emitViewState()
    void emitLoginState(subject)
  })

  wc.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return
    sendToRenderer('account:loadFailed', { code, desc, url })
  })

  currentView = view
  currentSubject = subject
  return view
}

function sameSubject(a: SessionSubject, b: SessionSubject): boolean {
  return a.kind === b.kind && a.id === b.id
}

function attachView(): boolean {
  const win = getHostWindow()
  if (!win || !currentView) return false
  if (!viewAttached) {
    win.contentView.addChildView(currentView)
    viewAttached = true
    win.on('resize', () => sendToRenderer('account:needBounds', {}))
  }
  currentView.setBounds(viewBounds)
  currentView.setVisible(true)
  return true
}

function detachView(): void {
  const win = getHostWindow()
  if (!win || win.isDestroyed() || !currentView || currentView.webContents.isDestroyed()) return
  // 幂等：无论 viewAttached 标记如何，先强制隐藏再移除（与 login.ts 同口径修复，
  // 避免守卫短路导致残留可见浏览器视图挡在界面上方）
  try {
    currentView.setVisible(false)
    win.contentView.removeChildView(currentView)
  } catch {
    /* 视图可能已被移除，忽略 */
  }
  viewAttached = false
}

function emitViewState(): void {
  if (!currentView || currentView.webContents.isDestroyed()) {
    sendToRenderer('account:stateChanged', { open: false })
    return
  }
  const wc = currentView.webContents
  sendToRenderer('account:stateChanged', {
    open: viewAttached,
    subject: currentSubject,
    url: wc.getURL(),
    title: wc.getTitle(),
    loading: wc.isLoading(),
    canGoBack: wc.navigationHistory.canGoBack(),
    canGoForward: wc.navigationHistory.canGoForward()
  })
}

// ---------------------------------------------------------------- 登录态查询 / 加密备份

function filterCookies(all: Cookie[], domains: readonly string[]): Cookie[] {
  return all.filter((c) => {
    const domain = c.domain ?? ''
    return domains.some((d) => domain === d || domain.endsWith(d))
  })
}

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

function buildCookieHeader(list: Cookie[]): string {
  return list.map((c) => `${c.name}=${c.value}`).join('; ')
}

/** 主体对应的目标域 */
function targetDomains(subject: SessionSubject): readonly string[] {
  if (subject.kind === 'acct') {
    // 账号级登录态 = 抖音主站 + 巨量百应（同属账号矩阵的投放域）
    const douyin = LOGIN_TARGETS.find((x) => x.id === 'douyin')
    const baiying = LOGIN_TARGETS.find((x) => x.id === 'baiying')
    return Array.from(
      new Set([
        ...(douyin ? douyin.domains : ['douyin.com']),
        ...(baiying ? baiying.domains : [])
      ])
    )
  }
  return QC_DOMAINS
}

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

async function checkLogin(subject: SessionSubject): Promise<AccountLoginState> {
  const ses = session.fromPartition(partitionOf(subject))
  const all = await ses.cookies.get({})
  const mine = dedupeCookies(filterCookies(all, targetDomains(subject)))
  const names = Array.from(new Set(mine.map((c) => c.name))).sort()
  const matched = LOGIN_COOKIE_CANDIDATES.filter((n) => names.includes(n))

  // 已登录 → 顺带做一次加密备份（磁盘无明文，灾备 + 导出源）
  let backupExists = existsSync(credentialPath(subject))
  let backupError: string | undefined
  if (matched.length > 0 && safeStorage.isEncryptionAvailable()) {
    try {
      const enc = safeStorage.encryptString(
        JSON.stringify(mine.map((c) => ({ name: c.name, value: c.value, domain: c.domain ?? '', path: c.path ?? '/' })))
      )
      mkdirSync(getClientPaths().dataDir, { recursive: true })
      writeFileSync(credentialPath(subject), enc)
      backupExists = true
    } catch (error) {
      backupError = String(error)
    }
  }

  return {
    kind: subject.kind,
    id: subject.id,
    loggedIn: matched.length > 0,
    matchedCookies: matched,
    cookieCount: mine.length,
    cookieNames: names,
    backupExists,
    backupError,
    checkedAt: new Date().toISOString()
  }
}

async function emitLoginState(subject: SessionSubject): Promise<void> {
  try {
    sendToRenderer('account:loginStateChanged', await checkLogin(subject))
  } catch {
    // 查询失败不打断界面
  }
}

/** 导出给 Python 引擎（含明文，仅内存流转，不落盘） */
async function exportForEngine(subject: SessionSubject) {
  const ses = session.fromPartition(partitionOf(subject))
  const all = await ses.cookies.get({})
  const mine = dedupeCookies(filterCookies(all, targetDomains(subject)))
  const view = currentView
  const userAgent = view && !view.webContents.isDestroyed() ? view.webContents.getUserAgent() : 'Mozilla/5.0'
  return {
    cookieHeader: buildCookieHeader(mine),
    userAgent,
    cookieCount: mine.length,
    cookieNames: mine.map((c) => c.name).sort(),
    subject,
    exportedAt: new Date().toISOString()
  }
}

// ---------------------------------------------------------------- 拉取信息探针

/**
 * 从抖音用户主页 SSR 数据里提取公开信息（昵称/粉丝数/作品数）。
 * 递归扫描 _ROUTER_DATA，找第一个含 nickname + 粉丝计数的 user 节点。
 */
const EXTRACT_SCRIPT = `
  (() => {
    try {
      const rd = window._ROUTER_DATA;
      if (!rd) return null;
      const walk = (o, depth) => {
        if (!o || typeof o !== 'object' || depth > 9) return null;
        if (typeof o.nickname === 'string' &&
            (o.follower_count !== undefined || o.followerCount !== undefined)) {
          return {
            nickname: o.nickname,
            followerCount: Number(o.follower_count ?? o.followerCount ?? 0),
            awemeCount: Number(o.aweme_count ?? o.awemeCount ?? o.totalAwemeCount ?? 0)
          };
        }
        for (const k of Object.keys(o)) {
          if (k === '__proto__' || k === 'trackData') continue;
          const r = walk(o[k], depth + 1);
          if (r) return r;
        }
        return null;
      };
      return walk(rd, 0);
    } catch (e) { return null; }
  })()
`

/**
 * 拉取账号公开信息：绑定账号分区 → 打开抖音主页 → 解析 _ROUTER_DATA。
 * 最多重试 3 次（页面可能先跳登录/加载慢）。需要该账号已登录。
 */
async function pullUserInfo(subject: SessionSubject, douyinId: string): Promise<unknown> {
  const view = ensureView(subject)
  if (!attachView()) throw new Error('无法挂载浏览器视图（没有主窗口）')
  const wc = view.webContents

  const url = `https://www.douyin.com/user/${encodeURIComponent(douyinId)}`
  await wc.loadURL(url)
  // 等 SPA 渲染出 _ROUTER_DATA
  await new Promise((r) => setTimeout(r, 1200))

  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const info = await wc.executeJavaScript(EXTRACT_SCRIPT, true)
      if (info && typeof info.nickname === 'string') return info
    } catch {
      /* 解析失败继续重试 */
    }
    if (attempt < 2) {
      await new Promise((r) => setTimeout(r, 900))
      wc.reload()
      await new Promise((r) => setTimeout(r, 1200))
    }
  }
  return null
}

// ---------------------------------------------------------------- 操作

async function openExternalLogin(
  url: string,
  profileKey: string,
  profilePath?: string,
  accountId?: number
): Promise<{ ok: boolean; url: string; external: true; profilePath?: string; port?: number; accountId?: number; error?: string }> {
  closeBrowser()
  const result = await openManagedBrowser(url, { profileKey, profilePath })
  return { ...result, ...(accountId === undefined ? {} : { accountId }) }
}

function closeBrowser(): { ok: boolean } {
  detachView()
  emitViewState()
  return { ok: true }
}

async function openMaterialFolder(accountName: string): Promise<{ ok: boolean; error?: string }> {
  const folder = join(getClientPaths().downloadsDir, accountName)
  if (!existsSync(folder)) {
    return { ok: false, error: `素材目录不存在：${folder}（该账号尚未下载过素材）` }
  }
  const err = await shell.openPath(folder)
  if (err) return { ok: false, error: err }
  return { ok: true }
}

/** 登出：清空该主体的登录态（含加密备份） */
async function clearLogin(subject: SessionSubject): Promise<{ ok: boolean }> {
  const ses = session.fromPartition(partitionOf(subject))
  const all = await ses.cookies.get({})
  const domains = targetDomains(subject)
  for (const c of filterCookies(all, domains)) {
    const scheme = c.secure ? 'https' : 'http'
    const host = (c.domain ?? '').replace(/^\./, '')
    if (!host) continue
    try {
      await ses.cookies.remove(`${scheme}://${host}${c.path ?? '/'}`, c.name)
    } catch {
      /* 单条删除失败不致命 */
    }
  }
  try {
    rmSync(credentialPath(subject), { force: true })
  } catch {
    /* 删除失败不致命 */
  }
  return { ok: true }
}

// ---------------------------------------------------------------- IPC 注册

export function registerAccountLoginIpc(): void {
  // 抖音登录页在账号专属托管 Chrome profile 打开；与 Electron 缓存分区相互独立。
  ipcMain.handle('account:openBrowser', async (_e, params: { accountId: number; profilePath?: string }) => {
    const douyin = LOGIN_TARGETS.find((t) => t.id === 'douyin')
    return openExternalLogin(
      douyin ? douyin.url : 'https://www.douyin.com/',
      `account-${params.accountId}`,
      params.profilePath,
      params.accountId
    )
  })

  // 打开精选页（百应选品广场入口）
  ipcMain.handle('account:openJingxuan', async (_e, params: { accountId: number; profilePath?: string }) => {
    const baiying = LOGIN_TARGETS.find((t) => t.id === 'baiying')
    const url = baiying ? baiying.url : 'https://buyin.jinritemai.com/'
    return openExternalLogin(url, `account-${params.accountId}`, params.profilePath, params.accountId)
  })

  // 千川登录页在独立的托管 Chrome profile 打开。
  ipcMain.handle('qianchuan:openLogin', async (_e, qcId: number) => {
    if (!Number.isSafeInteger(qcId) || qcId < 1) {
      return { ok: false, external: true, url: QC_LOGIN_URL, error: '请选择有效的千川账户档案后再打开登录页' }
    }
    return openExternalLogin(QC_LOGIN_URL, `qianchuan-${qcId}`, undefined, qcId)
  })

  ipcMain.handle('account:closeBrowser', () => closeBrowser())

  // 拉取信息：返回 { nickname, followerCount, awemeCount } 或 null
  ipcMain.handle(
    'account:pullInfo',
    async (_e, params: { accountId: number; douyinId: string }): Promise<unknown> => {
      const subject: SessionSubject = { kind: 'acct', id: params.accountId, name: `账号${params.accountId}` }
      try {
        return await pullUserInfo(subject, params.douyinId)
      } catch (error) {
        return { error: String(error) }
      }
    }
  )

  ipcMain.handle('account:checkLogin', async (_e, accountId: number) => {
    return checkLogin({ kind: 'acct', id: accountId, name: `账号${accountId}` })
  })

  ipcMain.handle('qianchuan:checkLogin', async (_e, qcId: number) => {
    return checkLogin({ kind: 'qc', id: qcId, name: `千川${qcId}` })
  })

  ipcMain.handle('account:exportForEngine', async (_e, accountId: number) => {
    return exportForEngine({ kind: 'acct', id: accountId, name: `账号${accountId}` })
  })

  ipcMain.handle('qianchuan:exportForEngine', async (_e, qcId: number) => {
    return exportForEngine({ kind: 'qc', id: qcId, name: `千川${qcId}` })
  })

  ipcMain.handle('account:clearLogin', async (_e, accountId: number) => {
    return clearLogin({ kind: 'acct', id: accountId, name: `账号${accountId}` })
  })

  ipcMain.handle('qianchuan:clearLogin', async (_e, qcId: number) => {
    return clearLogin({ kind: 'qc', id: qcId, name: `千川${qcId}` })
  })

  // 打开素材文件夹
  ipcMain.handle('account:openMaterialFolder', (_e, accountName: string) => openMaterialFolder(accountName))

  // 视图位置同步（渲染进程测出宿主位置后上报）
  ipcMain.on('account:setBounds', (_e, rect: { x: number; y: number; width: number; height: number }) => {
    viewBounds = {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.max(0, Math.round(rect.width)),
      height: Math.max(0, Math.round(rect.height))
    }
    if (currentView && viewAttached) currentView.setBounds(viewBounds)
  })

  ipcMain.handle('account:getState', () => {
    if (!currentView || currentView.webContents.isDestroyed()) return { open: false }
    return {
      open: viewAttached,
      subject: currentSubject,
      url: currentView.webContents.getURL(),
      title: currentView.webContents.getTitle(),
      loading: currentView.webContents.isLoading(),
      canGoBack: currentView.webContents.navigationHistory.canGoBack(),
      canGoForward: currentView.webContents.navigationHistory.canGoForward()
    }
  })
}

/** 应用退出前清理 */
export function disposeAccountLogin(): void {
  detachView()
  destroyView()
  currentSubject = null
}
