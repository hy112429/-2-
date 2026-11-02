import { spawn, type ChildProcessWithoutNullStreams } from 'child_process'
import { existsSync, mkdirSync, readFileSync } from 'fs'
import { homedir } from 'os'
import { join, resolve } from 'path'
import { createInterface } from 'readline'
import { getEngineDir, getPythonPath, getPythonSpawnEnvironment } from './engine'
import { getClientPaths } from './app-dirs'
import { captureExternalBaiyingResponse } from './probe'

const CHROME_CANDIDATES = [
  process.env['TOUGUANG_CHROME_PATH'],
  join(process.env['ProgramFiles'] ?? 'C:\\Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  join(process.env['ProgramFiles(x86)'] ?? 'C:\\Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
  join(process.env['LOCALAPPDATA'] ?? join(homedir(), 'AppData', 'Local'), 'Google', 'Chrome', 'Application', 'chrome.exe')
].filter((value): value is string => Boolean(value))

const CDP_START_TIMEOUT_MS = 30_000
const bridgeSessions = new Map<string, BridgeSession>()
const creatingSessions = new Map<string, Promise<BridgeSession>>()
let bridgeSequence = 0

interface BridgeSession {
  profilePath: string
  port: number
  child: ChildProcessWithoutNullStreams
  pending: Map<string, {
    resolve: (value: Record<string, unknown>) => void
    reject: (error: Error) => void
    onProgress?: (value: ManagedBrowserProgress) => void
  }>
  ready: boolean
  sequence: number
  stderrTail: string
}

interface OpenManagedBrowserOptions {
  profileKey: string
  profilePath?: string
}

export interface ManagedBrowserProgress {
  stage: string
  message?: string
  index?: number
  total?: number
  percent?: number
  unit?: string
  indeterminate?: boolean
  productId?: string
  productTitle?: string
}

export interface ManagedBrowserPageStatus {
  ok: true
  inspectable: boolean
  baiyingOpen: boolean
  creatorCenterOpen: boolean
}

function findChrome(): string {
  const executable = CHROME_CANDIDATES.find((candidate) => existsSync(candidate))
  if (!executable) {
    throw new Error('找不到 Google Chrome。请安装 Chrome，或设置 TOUGUANG_CHROME_PATH 指向 chrome.exe。')
  }
  return executable
}

function safeProfileKey(value: string): string {
  const key = value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-')
  return key || 'general'
}

function canonicalProfilePath(value: string): string {
  return resolve(value).replace(/[\\/]+$/, '').toLowerCase()
}

function rejectDefaultChromeProfile(profilePath: string): void {
  const localAppData = process.env['LOCALAPPDATA'] ?? join(homedir(), 'AppData', 'Local')
  const defaultPath = join(localAppData, 'Google', 'Chrome', 'User Data')
  if (canonicalProfilePath(profilePath) === canonicalProfilePath(defaultPath)) {
    throw new Error('Chrome 136 及以上版本不允许对默认用户资料目录开放远程调试。请留空使用客户端专用 profile，或选择其他非默认 Chrome 资料目录。')
  }
}

function resolveProfilePath(options: OpenManagedBrowserOptions, createDefaultDirectory = true): string {
  const profilePath = options.profilePath?.trim()
    ? resolve(options.profilePath)
    : join(getClientPaths().browserProfilesDir, safeProfileKey(options.profileKey))
  rejectDefaultChromeProfile(profilePath)

  if (options.profilePath?.trim()) {
    const defaultDirectory = join(profilePath, 'Default')
    if (!existsSync(defaultDirectory)) {
      throw new Error(`所选浏览器目录内没有 Default 文件夹：${profilePath}`)
    }
  } else if (createDefaultDirectory) {
    mkdirSync(profilePath, { recursive: true })
  }
  return profilePath
}

/** 只解析并校验账号 profile，不启动 Chrome，也不创建默认 profile 目录。 */
export function validateManagedProfilePath(profileKey: string, profilePath?: string): string {
  return resolveProfilePath({ profileKey, profilePath }, false)
}

/** 连接千川等独立账号的现有托管浏览器；此处不启动浏览器或触发登录。 */
export async function existingManagedBrowserConnection(profileKey: string): Promise<{ profilePath: string; port: number } | null> {
  const profilePath = validateManagedProfilePath(profileKey)
  const port = await readActivePort(profilePath)
  return port === null ? null : { profilePath, port }
}

async function readActivePort(profilePath: string): Promise<number | null> {
  const activePortPath = join(profilePath, 'DevToolsActivePort')
  if (!existsSync(activePortPath)) return null
  try {
    const raw = readFileSync(activePortPath, 'utf-8').trim().split(/\r?\n/)[0]
    const port = Number(raw)
    if (!Number.isInteger(port) || port < 1 || port > 65535) return null
    const response = await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(1200) })
    if (!response.ok) return null
    const payload = await response.json() as { webSocketDebuggerUrl?: unknown }
    return typeof payload.webSocketDebuggerUrl === 'string' ? port : null
  } catch {
    return null
  }
}

/** 只读检查现有 CDP profile 的标签页，不启动 Chrome，也不读取登录态。 */
export async function inspectManagedBrowserPages(options: OpenManagedBrowserOptions): Promise<ManagedBrowserPageStatus> {
  const profilePath = resolveProfilePath(options, false)
  const port = await readActivePort(profilePath)
  if (port === null) {
    return { ok: true, inspectable: false, baiyingOpen: false, creatorCenterOpen: false }
  }

  try {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(2_000) })
    if (!response.ok) {
      return { ok: true, inspectable: false, baiyingOpen: false, creatorCenterOpen: false }
    }
    const targets: unknown = await response.json()
    if (!Array.isArray(targets)) {
      return { ok: true, inspectable: false, baiyingOpen: false, creatorCenterOpen: false }
    }

    let baiyingOpen = false
    let creatorCenterOpen = false
    for (const target of targets) {
      if (!target || typeof target !== 'object') continue
      const item = target as { type?: unknown; url?: unknown }
      if (item.type !== 'page' || typeof item.url !== 'string') continue
      let hostname = ''
      try {
        hostname = new URL(item.url).hostname.toLowerCase()
      } catch {
        continue
      }
      if (hostname === 'buyin.jinritemai.com' || hostname === 'buyin.douyinec.com') baiyingOpen = true
      if (hostname === 'creator.douyin.com') creatorCenterOpen = true
      if (baiyingOpen && creatorCenterOpen) break
    }
    return { ok: true, inspectable: true, baiyingOpen, creatorCenterOpen }
  } catch {
    return { ok: true, inspectable: false, baiyingOpen: false, creatorCenterOpen: false }
  }
}

async function waitForActivePort(profilePath: string): Promise<number> {
  const startedAt = Date.now()
  while (Date.now() - startedAt < CDP_START_TIMEOUT_MS) {
    const port = await readActivePort(profilePath)
    if (port !== null) return port
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250))
  }
  throw new Error(
    `Chrome 未开放 CDP 端口。若所选 profile 正被普通 Chrome 窗口使用，请先关闭使用该 profile 的 Chrome，再重试；也可留空创建客户端专用 profile。目录：${profilePath}`
  )
}

async function launchChrome(profilePath: string): Promise<number> {
  const activePort = await readActivePort(profilePath)
  if (activePort !== null) return activePort

  const chrome = findChrome()
  const child = spawn(chrome, [
    `--user-data-dir=${profilePath}`,
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=0',
    '--remote-allow-origins=*',
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank'
  ], { detached: true, stdio: 'ignore', windowsHide: false })
  child.on('error', () => undefined)
  child.unref()
  return waitForActivePort(profilePath)
}

function parseBridgeLine(line: string, session: BridgeSession, readyResolve: (value: BridgeSession) => void, readyReject: (error: Error) => void): void {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(line) as Record<string, unknown>
  } catch {
    return
  }

  if (payload['type'] === 'ready') {
    session.ready = true
    readyResolve(session)
    return
  }
  if (payload['type'] === 'fatal') {
    const error = new Error(String(payload['error'] ?? 'Playwright CDP 桥接启动失败'))
    session.ready = false
    if (bridgeSessions.get(canonicalProfilePath(session.profilePath)) === session) {
      bridgeSessions.delete(canonicalProfilePath(session.profilePath))
    }
    readyReject(error)
    for (const pending of session.pending.values()) pending.reject(error)
    session.pending.clear()
    return
  }
  if (payload['type'] === 'baiying-response') {
    const url = payload['url']
    const body = payload['body']
    if (typeof url === 'string' && typeof body === 'string') {
      captureExternalBaiyingResponse(url, body)
    }
    return
  }
  if (payload['type'] === 'progress') {
    const id = String(payload['id'] ?? '')
    const pending = session.pending.get(id)
    if (!pending?.onProgress) return
    const percent = Number(payload['percent'])
    const index = Number(payload['index'])
    const total = Number(payload['total'])
    pending.onProgress({
      stage: String(payload['stage'] ?? '处理中'),
      ...(typeof payload['message'] === 'string' ? { message: payload['message'] } : {}),
      ...(Number.isInteger(index) ? { index } : {}),
      ...(Number.isInteger(total) ? { total } : {}),
      ...(Number.isFinite(percent) ? { percent: Math.max(0, Math.min(100, percent)) } : {}),
      ...(typeof payload['unit'] === 'string' ? { unit: payload['unit'] } : {}),
      indeterminate: payload['indeterminate'] === true || !Number.isFinite(percent),
      ...(typeof payload['productId'] === 'string' ? { productId: payload['productId'] } : {}),
      ...(typeof payload['productTitle'] === 'string' ? { productTitle: payload['productTitle'] } : {})
    })
    return
  }
  if (payload['type'] === 'result') {
    const id = String(payload['id'] ?? '')
    const pending = session.pending.get(id)
    if (!pending) return
    session.pending.delete(id)
    if (
      payload['ok'] === true ||
      (payload['isolated'] === true && [
        'PUBLISH_VERIFY_OTHER_ROW_ISOLATED',
        'CREATOR_COVER_REQUIRED_ISOLATED'
      ].includes(String(payload['classification'] ?? '')))
    ) pending.resolve(payload)
    else pending.reject(new Error(String(payload['error'] ?? '外部 Chrome 操作失败')))
  }
}

async function startBridge(profilePath: string, port: number): Promise<BridgeSession> {
  const scriptPath = join(getEngineDir(), 'browser_cdp.py')
  if (!existsSync(scriptPath)) throw new Error(`CDP 桥接脚本不存在：${scriptPath}`)
  const child = spawn(getPythonPath(), [scriptPath, 'watch', '--profile', profilePath], {
    cwd: getEngineDir(),
    windowsHide: true,
    stdio: 'pipe',
    env: getPythonSpawnEnvironment({
      TOUGUANG_DATA_DIR: getClientPaths().dataDir,
      TOUGUANG_DOWNLOADS_DIR: getClientPaths().downloadsDir
    })
  })
  const session: BridgeSession = {
    profilePath,
    port,
    child,
    pending: new Map(),
    ready: false,
    sequence: 0,
    stderrTail: ''
  }
  let readySettled = false
  let readyResolve!: (value: BridgeSession) => void
  let readyReject!: (error: Error) => void
  const readyPromise = new Promise<BridgeSession>((resolvePromise, rejectPromise) => {
    readyResolve = (value) => { readySettled = true; resolvePromise(value) }
    readyReject = (error) => { readySettled = true; rejectPromise(error) }
  })
  const readyTimer = setTimeout(() => readyReject(new Error('连接 Chrome CDP 超时，请确认 profile 目录可用。')), 20_000)
  createInterface({ input: child.stdout }).on('line', (line) => {
    parseBridgeLine(line, session, readyResolve, readyReject)
  })
  child.stderr.on('data', (chunk: Buffer) => {
    session.stderrTail = `${session.stderrTail}${chunk.toString('utf-8')}`.slice(-3000)
  })
  child.on('error', (error) => {
    if (!readySettled) readyReject(new Error(`无法启动 Playwright 桥接进程：${error.message}`))
  })
  child.on('exit', (code) => {
    const error = new Error(`Playwright 桥接已退出（${code ?? '未知退出码'}）${session.stderrTail ? `：${session.stderrTail.trim().slice(-500)}` : ''}`)
    if (!readySettled) readyReject(error)
    for (const pending of session.pending.values()) pending.reject(error)
    session.pending.clear()
    if (bridgeSessions.get(canonicalProfilePath(profilePath)) === session) {
      bridgeSessions.delete(canonicalProfilePath(profilePath))
    }
  })
  try {
    return await readyPromise
  } finally {
    clearTimeout(readyTimer)
  }
}

async function createSession(profilePath: string): Promise<BridgeSession> {
  const port = await launchChrome(profilePath)
  return startBridge(profilePath, port)
}

function retireSession(profilePath: string, session: BridgeSession): void {
  const key = canonicalProfilePath(profilePath)
  session.ready = false
  if (bridgeSessions.get(key) === session) bridgeSessions.delete(key)
  try {
    session.child.stdin.end()
  } catch {
    try {
      session.child.kill()
    } catch {
      /* 桥接进程已经退出。 */
    }
  }
}

function isBrowserDisconnectedError(error: unknown): boolean {
  const message = (error instanceof Error ? error.message : String(error)).toLowerCase()
  return [
    'target page, context or browser has been closed',
    'target closed',
    'browser has been closed',
    'browser closed',
    'browser disconnected',
    'connection closed',
    'cdp session closed',
    'playwright 桥接已退出'
  ].some((marker) => message.includes(marker))
}

/** 只在候选快照能按商品 ID 恢复时，才允许断线后重跑有副作用的选品命令。 */
export function prepareManagedBrowserRetry(
  action: string,
  data: Record<string, unknown>
): Record<string, unknown> | null {
  if (action === 'prepare-douyin-publish' || action === 'submit-douyin-publish') return null
  if (action !== 'run-selection' || data['previewOnly'] === true) return data
  const candidates = data['candidateSnapshot']
  const indexes = data['selectedIndexes']
  if (!Array.isArray(candidates) || !Array.isArray(indexes) || indexes.length === 0) return null
  const selectedIndexes = indexes.filter((value): value is number => Number.isInteger(value) && value >= 0)
  if (selectedIndexes.length !== indexes.length) return null
  const candidateIds = new Set(candidates.flatMap((candidate) =>
    candidate && typeof candidate === 'object' && Number.isInteger(candidate['cardIndex']) &&
      typeof candidate['productId'] === 'string' && candidate['productId']
      ? [candidate['cardIndex'] as number]
      : []
  ))
  if (!selectedIndexes.every((index) => candidateIds.has(index))) return null
  // browser_cdp.py 收到此标记后会重应用筛选、按商品 ID 重新取候选并映射卡片，
  // 再检查百应当前加车状态，避免使用过期 cardIndex 或重复点击加车。
  return { ...data, refreshCandidatesById: true }
}

/** Windows 上结束桥接 Python 及其子进程；托管 Chrome 是独立进程，不会被连带关闭。 */
export function bridgeProcessTreeTermination(
  platform: string,
  pid: number | undefined
): { command: string; args: string[] } | null {
  if (platform !== 'win32' || !pid || !Number.isInteger(pid) || pid < 1) return null
  return { command: 'taskkill', args: ['/pid', String(pid), '/T', '/F'] }
}

function terminateBridgeProcessTree(child: ChildProcessWithoutNullStreams): void {
  if (child.exitCode !== null) return
  const termination = bridgeProcessTreeTermination(process.platform, child.pid)
  if (!termination) {
    child.kill('SIGTERM')
    return
  }
  const killer = spawn(termination.command, termination.args, { windowsHide: true, stdio: 'ignore' })
  killer.on('error', () => {
    try {
      child.kill('SIGTERM')
    } catch {
      /* 进程已退出。 */
    }
  })
  killer.unref()
}

async function getSession(profilePath: string): Promise<BridgeSession> {
  const key = canonicalProfilePath(profilePath)
  const existing = bridgeSessions.get(key)
  if (existing && existing.ready && existing.child.exitCode === null) {
    const activePort = await readActivePort(profilePath)
    if (activePort === existing.port) return existing
    // Chrome 被人工关闭后，桥接进程可能仍存活，但其中的 BrowserContext 已失效。
    retireSession(profilePath, existing)
  }
  let creation = creatingSessions.get(key)
  if (!creation) {
    creation = createSession(profilePath)
    creatingSessions.set(key, creation)
  }
  try {
    const session = await creation
    bridgeSessions.set(key, session)
    return session
  } finally {
    if (creatingSessions.get(key) === creation) creatingSessions.delete(key)
  }
}

async function sendManagedCommand(
  profilePath: string,
  action: string,
  data: Record<string, unknown> = {},
  onProgress?: (progress: ManagedBrowserProgress) => void
): Promise<{ session: BridgeSession; result: Record<string, unknown> }> {
  let lastError: unknown
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const session = await getSession(profilePath)
    try {
      const result = await sendBridgeCommand(session, action, data, onProgress)
      return { session, result }
    } catch (error) {
      lastError = error
      const disconnected = isBrowserDisconnectedError(error)
      if (!disconnected) throw error
      // 断线后桥接上下文已不可信；先释放旧连接，避免后续命令继续复用失效会话。
      retireSession(profilePath, session)
      const retryData = prepareManagedBrowserRetry(action, data)
      if (!retryData) {
        if (action === 'prepare-douyin-publish') {
          throw new Error(
            `抖音创作者中心上传准备期间 Chrome/CDP 连接中断；为避免重复上传或重复创建草稿，本次未自动重放。` +
            `请检查创作者中心当前页面后再处理。原始错误：${error instanceof Error ? error.message : String(error)}`
          )
        }
        if (action === 'submit-douyin-publish') {
          throw new Error(
            `抖音最终发布期间 Chrome/CDP 连接中断，发布结果不确定；为避免重复发布，本次未自动重放。` +
            `请检查创作者中心后再处理。原始错误：${error instanceof Error ? error.message : String(error)}`
          )
        }
        throw new Error(
          `百应执行期间 Chrome/CDP 连接中断，且没有完整候选商品 ID 检查点，无法安全自动恢复；` +
          `为避免重复加车，本次未重放。当前批次检查点已保留。原始错误：${error instanceof Error ? error.message : String(error)}`
        )
      }
      if (attempt > 0) {
        if (action === 'run-selection' && data['previewOnly'] !== true) {
          throw new Error(
            `百应已按商品 ID 自动恢复一次，但 Chrome/CDP 再次中断；为避免重复加车，已停止继续重放并保留当前批次。` +
            `原始错误：${error instanceof Error ? error.message : String(error)}`
          )
        }
        throw error
      }
      data = retryData
      // 只读命令与带商品 ID 检查点的选品任务可安全重连；后者强制刷新并按 ID 恢复。
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError))
}

function sendBridgeCommand(
  session: BridgeSession,
  action: string,
  data: Record<string, unknown> = {},
  onProgress?: (progress: ManagedBrowserProgress) => void
): Promise<Record<string, unknown>> {
  const id = `cdp-${++bridgeSequence}`
  return new Promise((resolvePromise, rejectPromise) => {
    const timeoutMs = action === 'run-selection' || action === 'prepare-douyin-publish' || action === 'submit-douyin-publish' || (action === 'inspect-selection' && data['includeCategoryTree'] === true)
      ? 10 * 60_000
      : 60_000
    const timer = setTimeout(() => {
      session.pending.delete(id)
      rejectPromise(new Error(`Chrome 操作超时（${action}）`))
    }, timeoutMs)
    session.pending.set(id, {
      resolve: (value) => { clearTimeout(timer); resolvePromise(value) },
      reject: (error) => { clearTimeout(timer); rejectPromise(error) },
      onProgress
    })
    try {
      session.child.stdin.write(`${JSON.stringify({ id, action, ...data })}\n`)
    } catch (error) {
      clearTimeout(timer)
      session.pending.delete(id)
      rejectPromise(new Error(`无法向 Chrome 桥接发送命令：${String(error)}`))
    }
  })
}

/** 向指定托管 Chrome profile 发送白名单百应页面命令。 */
export async function commandManagedBrowser(
  action: 'inspect-selection' | 'run-selection' | 'check-account-logins' | 'prepare-douyin-publish' | 'submit-douyin-publish' | 'open-douyin-creator',
  data: Record<string, unknown>,
  options: OpenManagedBrowserOptions,
  onProgress?: (progress: ManagedBrowserProgress) => void
): Promise<Record<string, unknown>> {
  const profilePath = resolveProfilePath(options)
  const { result } = await sendManagedCommand(profilePath, action, data, onProgress)
  return result
}

export async function openManagedBrowser(
  url: string,
  options: OpenManagedBrowserOptions
): Promise<{ ok: boolean; url: string; external: true; profilePath?: string; port?: number; title?: string; error?: string }> {
  try {
    const profilePath = resolveProfilePath(options)
    const { session, result } = await sendManagedCommand(profilePath, 'open', { url })
    return {
      ok: true,
      external: true,
      url: typeof result['url'] === 'string' ? result['url'] : url,
      title: typeof result['title'] === 'string' ? result['title'] : undefined,
      profilePath,
      port: session.port
    }
  } catch (error) {
    return { ok: false, external: true, url, error: error instanceof Error ? error.message : String(error) }
  }
}

/** 应用退出时仅关闭 Playwright 连接；外部 Chrome 与其登录 profile 保持打开。 */
export function disposeExternalBrowserBridges(): void {
  for (const session of bridgeSessions.values()) {
    try {
      session.child.stdin.write(`${JSON.stringify({ action: 'stop' })}\n`)
      session.child.stdin.end()
    } catch {
      /* 即使管道已关闭，也继续终止桥接进程树。 */
    }
    terminateBridgeProcessTree(session.child)
  }
  bridgeSessions.clear()
  creatingSessions.clear()
}
