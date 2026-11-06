import { app, ipcMain, safeStorage } from 'electron'
import { createServer, type Server } from 'node:http'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, statSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { execFile } from 'node:child_process'
import { getDownloadDir } from './app-dirs'
import { getEngineDir } from './engine'
import { openQianchuanOAuthInPersistentProfile } from './external_browser'

const CALLBACK = 'http://127.0.0.1:8899/callback'
const API = 'https://api.oceanengine.com'
const ID = /^\d{1,20}$/
const timeoutMs = 15 * 60_000
type Credentials = { appId: string; secret: string; accessToken?: string; refreshToken?: string; accessExpiresAt?: number; refreshExpiresAt?: number; advertiserIds?: string[] }
export type QianchuanVideo = { videoId: string; awemeItemId: string; title: string; url: string; duration: number | null }
type Video = QianchuanVideo
export type AuthorizedAccount = { id: string; name: string; source: string }
type AuthorizedIdentity = { id: string; type: string; name: string }
export type AuthorizedAweme = { id: string; name: string }
let listener: Server | null = null
let pendingState: string | null = null
let pendingUntil = 0
let authTimer: NodeJS.Timeout | null = null
let authorizationRevision = 0

function vaultPath(): string { return join(app.getPath('userData'), 'qianchuan-openapi.bin') }
function requireEncryption(): void {
  if (!safeStorage.isEncryptionAvailable()) throw new Error('系统凭证加密不可用，未保存 App Secret 或 Token。')
}
function load(): Credentials | null {
  requireEncryption()
  try { return JSON.parse(safeStorage.decryptString(readFileSync(vaultPath()))) as Credentials }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw new Error('千川凭证无法解密，请检查当前 Windows 用户或重设应用凭证。')
  }
}
function save(value: Credentials): void {
  requireEncryption()
  const path = vaultPath()
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.tmp`
  writeFileSync(tmp, safeStorage.encryptString(JSON.stringify(value)))
  renameSync(tmp, path)
}
function decimal(value: unknown, label: string): string {
  const text = String(value ?? '').trim()
  if (!ID.test(text)) throw new Error(`${label}必须是 1–20 位数字。`)
  return text
}
function safeMessage(value: unknown): string { return String(value ?? '').slice(0, 240) }

// 官方 JSON 响应里的 16 位以上整数要先转为字符串，否则 JSON.parse 会丢失 ID 精度。
export function parseBigIds(raw: string): any {
  let quoted = false; let escaped = false; let result = ''
  for (let i = 0; i < raw.length;) {
    const ch = raw[i]
    if (quoted) {
      result += ch; i++
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') quoted = false
    } else if (ch === '"') { quoted = true; result += ch; i++ }
    else if (ch === '-' || /[0-9]/.test(ch)) {
      const match = raw.slice(i).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/)
      if (!match) { result += ch; i++; continue }
      const token = match[0]
      result += /^-?\d{16,}$/.test(token) ? `"${token}"` : token
      i += token.length
    } else { result += ch; i++ }
  }
  return JSON.parse(result)
}

async function apiPost(path: string, body: Record<string, string>): Promise<any> {
  const response = await fetch(`${API}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, app_id: Number(body.app_id) }), signal: AbortSignal.timeout(20_000) })
  const payload = parseBigIds(await response.text())
  if (!response.ok || payload?.code !== 0) throw new Error(`千川接口失败（${payload?.code ?? response.status}）：${safeMessage(payload?.message)}`)
  return payload.data
}
function storeToken(current: Credentials, data: any): Credentials {
  if (!data?.access_token || !data?.refresh_token) throw new Error('授权接口未返回完整 Token。')
  const now = Date.now()
  const next = { ...current, accessToken: String(data.access_token), refreshToken: String(data.refresh_token), accessExpiresAt: now + Number(data.expires_in ?? 0) * 1000, refreshExpiresAt: now + Number(data.refresh_token_expires_in ?? 0) * 1000, advertiserIds: Array.isArray(data.advertiser_ids) ? data.advertiser_ids.map(String) : current.advertiserIds ?? [] }
  save(next)
  return next
}
async function token(): Promise<Credentials> {
  let current = load()
  if (!current?.accessToken || !current.refreshToken) throw new Error('尚未完成千川账户授权。')
  if ((current.accessExpiresAt ?? 0) > Date.now() + 60_000) return current
  if ((current.refreshExpiresAt ?? 0) <= Date.now()) throw new Error('千川授权已过期，请重新授权。')
  const data = await apiPost('/open_api/oauth2/refresh_token/', { app_id: current.appId, secret: current.secret, refresh_token: current.refreshToken })
  current = storeToken(current, data)
  return current
}
async function apiGet(url: URL, accessToken: string, tokenInQuery = false): Promise<any> {
  if (tokenInQuery) url.searchParams.set('access_token', accessToken)
  const response = await fetch(url, {
    headers: tokenInQuery ? {} : { 'Access-Token': accessToken },
    redirect: 'error',
    cache: 'no-store',
    signal: AbortSignal.timeout(20_000)
  })
  const payload = parseBigIds(await response.text())
  if (!response.ok || payload?.code !== 0) throw new Error(`接口错误 ${payload?.code ?? response.status}：${safeMessage(payload?.message)}`)
  return payload.data
}
async function discoverAccounts(): Promise<{ accounts: AuthorizedAccount[]; identities: AuthorizedIdentity[]; warnings: string[]; requiresQianchuanAuthorization: boolean }> {
  const current = await token()
  const found = new Map<string, AuthorizedAccount>()
  const warnings: string[] = []
  let rows: any[] = []
  try {
    const data = await apiGet(new URL('/open_api/oauth2/advertiser/get/', API), current.accessToken!, true)
    rows = Array.isArray(data?.list) ? data.list : []
  } catch (error) {
    warnings.push(`获取已授权账户失败：${safeMessage((error as Error).message)}`)
    for (const id of current.advertiserIds ?? []) found.set(id, { id, name: '', source: '授权响应（未能核实类型）' })
  }
  const identities: AuthorizedIdentity[] = rows.filter((row) => row?.is_valid !== false).map((row) => ({
    id: String(row?.account_id ?? row?.advertiser_id ?? ''),
    type: String(row?.account_type ?? ''),
    name: String(row?.account_name ?? row?.advertiser_name ?? '')
  })).filter((row) => ID.test(row.id))
  for (const row of rows) {
    if (row?.is_valid === false) continue
    const id = String(row?.account_id ?? row?.advertiser_id ?? '')
    if (!ID.test(id)) continue
    const type = String(row?.account_type ?? '')
    if (type === 'QIANCHUAN') {
      found.set(id, { id, name: String(row?.account_name ?? row?.advertiser_name ?? ''), source: '已授权广告主' })
      continue
    }
    if (type === 'CUSTOMER_ADMIN' || type === 'CUSTOMER_OPERATOR') {
      let page = 1
      while (page <= 100) {
        const url = new URL('/open_api/2/customer_center/advertiser/list/', API)
        url.searchParams.set('cc_account_id', id)
        url.searchParams.set('account_source', 'QIANCHUAN')
        url.searchParams.set('page', String(page))
        url.searchParams.set('page_size', '100')
        try {
          const data = await apiGet(url, current.accessToken!)
          const managedAccounts = Array.isArray(data?.list) ? data.list : []
          for (const account of managedAccounts) {
            const advertiserId = String(account?.advertiser_id ?? account?.account_id ?? '')
            if (!ID.test(advertiserId)) continue
            const name = String(account?.advertiser_name ?? account?.account_name ?? '')
            found.set(advertiserId, { id: advertiserId, name, source: `工作台 ${id}` })
            identities.push({ id: advertiserId, name, type: 'QIANCHUAN' })
          }
          const totalPages = Number(data?.page_info?.total_page ?? 1)
          if (!managedAccounts.length || page >= totalPages) break
          page += 1
        } catch (error) {
          warnings.push(`已授权工作台 ${id}，但无法读取其下千川广告主列表：${safeMessage((error as Error).message)}。请为应用申请工作台账户管理下账户列表接口权限，或在授权页直接勾选目标千川广告主。`)
          break
        }
      }
      continue
    }
    if (type !== 'PLATFORM_ROLE_SHOP_ACCOUNT') continue
    let page = 1
    while (page <= 10) {
      const url = new URL('/open_api/v1.0/qianchuan/shop/advertiser/list/', API)
      url.searchParams.set('shop_id', id)
      url.searchParams.set('page', String(page))
      url.searchParams.set('page_size', '100')
      try {
        const data = await apiGet(url, current.accessToken!)
        for (const adv of Array.isArray(data?.adv_id_list) ? data.adv_id_list : []) {
          const advId = String(adv?.adv_id ?? '')
          if (ID.test(advId)) {
            const name = String(adv?.adv_name ?? '')
            found.set(advId, { id: advId, name, source: `店铺 ${id}` })
            identities.push({ id: advId, name, type: 'QIANCHUAN' })
          }
        }
        for (const advIdValue of Array.isArray(data?.list) ? data.list : []) {
          const advId = String(advIdValue)
          if (ID.test(advId) && !found.has(advId)) {
            found.set(advId, { id: advId, name: '', source: `店铺 ${id}` })
            identities.push({ id: advId, name: '', type: 'QIANCHUAN' })
          }
        }
        if (page >= Number(data?.page_info?.total_page ?? 1)) break
        page += 1
      } catch (error) {
        warnings.push(`已授权店铺 ${id}，但无法读取其下千川广告主：${safeMessage((error as Error).message)}。请在授权页直接勾选“千川号”，或为应用申请店铺广告主列表接口权限后重新授权。`)
        break
      }
    }
  }
  const accounts = Array.from(found.values())
  const hasManagedIdentity = identities.some((item) => item.type === 'CUSTOMER_ADMIN' || item.type === 'CUSTOMER_OPERATOR')
  const requiresQianchuanAuthorization = rows.length > 0 && accounts.length === 0
  if (requiresQianchuanAuthorization && hasManagedIdentity && !warnings.length) warnings.push('已授权工作台管理账户，但没有发现其下千川广告主。请确认该工作台管理目标账户，并申请工作台下千川账户列表权限。')
  else if (requiresQianchuanAuthorization && !warnings.length) warnings.push('本次授权清单没有千川广告主账户。请在授权页勾选目标千川业务账户；如果只授权了抖店，请先补足店铺广告主列表接口权限。')
  if (accounts.map((a) => a.id).join(',') !== (current.advertiserIds ?? []).join(',') && (rows.length > 0 || accounts.length > 0)) {
    save({ ...current, advertiserIds: accounts.map((a) => a.id) })
  }
  return { accounts, identities, warnings, requiresQianchuanAuthorization }
}
async function discoverAwemes(advertiserIdValue: string): Promise<AuthorizedAweme[]> {
  const current = await token()
  const advertiserId = decimal(advertiserIdValue, '广告主 ID')
  const found = new Map<string, AuthorizedAweme>()
  for (let page = 1; page <= 10; page++) {
    const url = new URL('/open_api/v1.0/qianchuan/aweme/authorized/get/', API)
    url.searchParams.set('advertiser_id', advertiserId)
    url.searchParams.set('page', String(page))
    url.searchParams.set('page_size', '50')
    const data = await apiGet(url, current.accessToken!)
    const rows = Array.isArray(data?.aweme_id_list) ? data.aweme_id_list : []
    for (const row of rows) {
      const id = String(row?.aweme_id ?? '')
      if (ID.test(id)) found.set(id, { id, name: String(row?.aweme_name ?? '') })
    }
    if (!rows.length || page >= Number(data?.page_info?.total_page ?? 1)) break
  }
  return Array.from(found.values())
}

/** 发布后自动编排的严格授权预检：目标广告主必须在 OAuth 可见范围内，抖音号必须唯一可匹配。 */
export async function resolveAuthorizedAweme(
  advertiserIdValue: string, preferredAwemeId = ''
): Promise<{ advertiserId: string; awemeId: string; awemeName: string }> {
  const advertiserId = decimal(advertiserIdValue, '千川广告主 ID')
  await token()
  const authorization = await discoverAccounts()
  const target = authorization.accounts.find((account) => account.id === advertiserId)
  if (!target) throw new Error(`广告主 ${advertiserId} 不在当前千川 OAuth 授权范围内`)
  const awemes = await discoverAwemes(advertiserId)
  if (!awemes.length) throw new Error(`广告主 ${advertiserId} 没有可读取的已授权抖音号`)
  const preferred = preferredAwemeId.trim()
  const match = preferred ? awemes.find((aweme) => aweme.id === preferred) : undefined
  if (match) return { advertiserId, awemeId: match.id, awemeName: match.name }
  if (!preferred && awemes.length === 1) {
    return { advertiserId, awemeId: awemes[0].id, awemeName: awemes[0].name }
  }
  throw new Error(preferred
    ? `本地抖音号 ID ${preferred} 不在该广告主的授权抖音号列表中`
    : `广告主下有 ${awemes.length} 个已授权抖音号，无法唯一匹配本地账号`)
}

/** 只收集 HTTPS 直链，并在达到本轮额度后停止分页。 */
export async function queryPlayableVideos(
  params: { advertiserId: string; productId: string; awemeId: string; maxVideos: number; maxPages?: number }
): Promise<Video[]> {
  const maxVideos = Math.max(0, Math.min(Math.trunc(params.maxVideos), 3))
  if (!maxVideos) return []
  const maxPages = Math.max(1, Math.min(Math.trunc(params.maxPages ?? 100), 100))
  const found = new Map<string, Video>()
  let cursor = ''
  for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
    const page = await queryVideos({
      advertiserId: params.advertiserId,
      productId: params.productId,
      awemeId: params.awemeId,
      ...(cursor ? { cursor } : {})
    })
    for (const video of page.videos) {
      let parsed: URL
      try { parsed = new URL(video.url) } catch { continue }
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password) continue
      const key = video.videoId || video.awemeItemId || parsed.toString()
      if (!found.has(key)) found.set(key, video)
      if (found.size >= maxVideos) break
    }
    if (found.size >= maxVideos || !page.hasMore || !page.cursor || page.cursor === cursor) break
    cursor = page.cursor
  }
  return Array.from(found.values()).slice(0, maxVideos)
}
function status(): Record<string, unknown> {
  const current = load()
  return { configured: Boolean(current), authorized: Boolean(current?.accessToken), appId: current?.appId ?? '', advertiserIds: current?.advertiserIds ?? [], callback: CALLBACK, expiresAt: current?.accessExpiresAt ?? null, authorizationRevision }
}
function sameState(received: string): boolean {
  if (!pendingState || Date.now() > pendingUntil) return false
  const a = Buffer.from(received); const b = Buffer.from(pendingState)
  return a.length === b.length && timingSafeEqual(a, b)
}
async function exchange(authCode: string): Promise<void> {
  const current = load()
  if (!current) throw new Error('请先配置 App ID 与 App Secret。')
  const data = await apiPost('/open_api/oauth2/access_token/', { app_id: current.appId, secret: current.secret, auth_code: authCode })
  storeToken(current, data)
  authorizationRevision += 1
}
async function beginAuth(authUrl: string): Promise<{ ok: boolean; callback: string; profilePath: string }> {
  const current = load()
  if (!current) throw new Error('请先保存 App ID 与 App Secret。')
  const url = new URL(authUrl.trim())
  if (url.protocol !== 'https:' || !['open.oceanengine.com', 'ad.oceanengine.com', 'qianchuan.jinritemai.com'].includes(url.hostname)) throw new Error('请粘贴巨量引擎或千川开放平台生成的官方 HTTPS 授权 URL。')
  if (url.searchParams.has('app_id') && url.searchParams.get('app_id') !== current.appId) throw new Error('授权 URL 的 App ID 与本机配置不同。')
  const redirect = url.searchParams.get('redirect_uri')
  if (redirect && redirect !== CALLBACK) throw new Error(`授权 URL 的回调地址必须是 ${CALLBACK}。`)
  if (listener) { listener.close(); listener = null }
  if (authTimer) clearTimeout(authTimer)
  pendingState = randomBytes(24).toString('hex')
  pendingUntil = Date.now() + timeoutMs
  url.searchParams.set('state', pendingState)
  listener = createServer(async (req, res) => {
    if (req.method !== 'GET' || !req.url) { res.writeHead(404).end(); return }
    const incoming = new URL(req.url, CALLBACK)
    if (incoming.pathname !== '/callback') { res.writeHead(404).end(); return }
    const code = incoming.searchParams.get('auth_code') ?? incoming.searchParams.get('code')
    if (!sameState(incoming.searchParams.get('state') ?? '') || !code) {
      res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' }).end('授权验证失败：state 不匹配、已超时或缺少授权码。')
      return
    }
    pendingState = null
    try {
      await exchange(code)
      res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' }).end('千川授权成功，Token 已在本机加密保存。可以关闭此页。')
    } catch (error) {
      res.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' }).end(`千川授权失败：${safeMessage((error as Error).message)}`)
    } finally { listener?.close(); listener = null; if (authTimer) clearTimeout(authTimer); authTimer = null }
  })
  try {
    await new Promise<void>((resolve, reject) => { listener!.once('error', reject); listener!.listen(8899, '127.0.0.1', resolve) })
    authTimer = setTimeout(() => { listener?.close(); listener = null; pendingState = null; authTimer = null }, timeoutMs)
    const opened = await openQianchuanOAuthInPersistentProfile(url.toString())
    if (!opened.ok) throw new Error('Google Chrome 授权窗口未能打开')
    return { ok: true, callback: CALLBACK, profilePath: opened.profilePath }
  } catch (error) {
    listener?.close(); listener = null; pendingState = null; if (authTimer) clearTimeout(authTimer); authTimer = null
    throw new Error(`发起专属浏览器授权失败：${safeMessage((error as Error).message)}`)
  }
}
async function queryVideos(params: { advertiserId: string; productId: string; awemeId: string; cursor?: string }): Promise<{ videos: Video[]; hasMore: boolean; cursor: string; totalReturned: number }> {
  const current = await token()
  const advertiserId = decimal(params.advertiserId, '广告主 ID')
  const productId = decimal(params.productId, '商品 ID')
  const url = new URL('/open_api/v1.0/qianchuan/file/video/aweme/get/', API)
  url.searchParams.set('advertiser_id', advertiserId)
  url.searchParams.set('aweme_id', decimal(params.awemeId, '抖音号 ID'))
  url.searchParams.set('filtering', `{"product_id":${productId}}`)
  url.searchParams.set('count', '50')
  if (params.cursor?.trim()) url.searchParams.set('cursor', decimal(params.cursor, '分页游标'))
  const response = await fetch(url, { headers: { 'Access-Token': current.accessToken! }, signal: AbortSignal.timeout(20_000) })
  const payload = parseBigIds(await response.text())
  if (!response.ok || payload?.code !== 0) throw new Error(`千川视频查询失败（${payload?.code ?? response.status}）：${safeMessage(payload?.message)}`)
  const rows = Array.isArray(payload?.data?.video_list) ? payload.data.video_list : []
  const videos: Video[] = rows.map((row: any) => ({ videoId: String(row.video_id ?? ''), awemeItemId: String(row.aweme_item_id ?? ''), title: String(row.title ?? ''), url: String(row.url ?? ''), duration: Number.isFinite(Number(row.duration)) ? Number(row.duration) : null }))
  return { videos, hasMore: payload?.data?.page_info?.has_more === true || payload?.data?.page_info?.has_more === 1, cursor: String(payload?.data?.page_info?.cursor ?? ''), totalReturned: videos.length }
}
async function verifyVideo(path: string): Promise<{ ok: boolean; path: string; bytes?: number; reason?: string }> {
  const file = resolve(path)
  const base = resolve(getDownloadDir())
  const child = relative(base, file)
  if (!child || child === '..' || child.startsWith(`..${sep}`) || isAbsolute(child) || !file.toLowerCase().endsWith('.mp4')) throw new Error('只能验证当前下载目录下的 MP4。')
  if (!existsSync(file) || !statSync(file).isFile()) return { ok: false, path: file, reason: '文件不存在。' }
  const realBase = realpathSync(base)
  const realChild = relative(realBase, realpathSync(file))
  if (!realChild || realChild === '..' || realChild.startsWith(`..${sep}`) || isAbsolute(realChild)) throw new Error('下载文件实际路径不在当前下载目录内。')
  const bytes = statSync(file).size
  const bundled = join(getEngineDir(), 'bin', process.platform === 'win32' ? 'ffprobe.exe' : 'ffprobe')
  const tool = existsSync(bundled) ? bundled : 'ffprobe'
  return new Promise((done) => {
    execFile(tool, ['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=codec_name', '-of', 'default=noprint_wrappers=1:nokey=1', file], { windowsHide: true, timeout: 20_000 }, (error, stdout) => {
      const codec = stdout.trim()
      done(error || !codec ? { ok: false, path: file, reason: '下载文件不是可识别的视频，或本机 ffprobe 不可用。' } : { ok: true, path: file, bytes })
    })
  })
}
export function registerQianchuanApiIpc(): void {
  ipcMain.handle('qianchuan:api:status', () => status())
  ipcMain.handle('qianchuan:api:configure', (_event, input: { appId: string; secret: string }) => {
    const appId = decimal(input.appId, 'App ID')
    if (!Number.isSafeInteger(Number(appId))) throw new Error('App ID 超出 JavaScript 安全整数范围，无法按接口要求准确提交。')
    if (!input.secret?.trim()) throw new Error('请输入 App Secret。')
    save({ appId, secret: input.secret.trim() })
    return status()
  })
  ipcMain.handle('qianchuan:api:authorize', (_event, url: string) => beginAuth(url))
  ipcMain.handle('qianchuan:api:videos', (_event, params: { advertiserId: string; productId: string; awemeId: string; cursor?: string }) => queryVideos(params))
  ipcMain.handle('qianchuan:api:accounts', () => discoverAccounts())
  ipcMain.handle('qianchuan:api:awemes', (_event, advertiserId: string) => discoverAwemes(advertiserId))
  ipcMain.handle('qianchuan:api:verifyVideo', (_event, path: string) => verifyVideo(path))
}
export function disposeQianchuanApi(): void { listener?.close(); listener = null; pendingState = null; if (authTimer) clearTimeout(authTimer); authTimer = null }
