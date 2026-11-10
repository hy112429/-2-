import { clipboard, ipcMain, type WebContents } from 'electron'
import { createHash } from 'node:crypto'
import { createReadStream, existsSync } from 'node:fs'
import { join } from 'node:path'
import { onProductCaptured } from './probe'
import { runEngineCommand, runScript } from './engine'
import { commandManagedBrowser, validateManagedProfilePath } from './external_browser'
import { getClientPaths, getDownloadDir } from './app-dirs'
import { withCartDownloadSlot } from './cart_download_slots'

/**
 * 百应「半自动批量浏览」驱动（T-15）
 * ─────────────────────────────────────────────────────────────
 * 为什么需要它：pack_detail 接口带 a_bogus / msToken 签名，引擎无法直接构造请求
 * （合规不逆向）。替代方案是让**已登录的真实浏览器**逐个打开商品详情页——
 * 签名由浏览器天然生成，客户端只做响应解析（探针 probe.ts）并落库。
 *
 * 流程：选择本地账号 → 使用其专属托管 Chrome profile → 逐个打开商品详情页 → 探针捕获
 * pack_detail → 写入 baiying_products 缓存 → 固定间隔 + 随机抖动限速 → 下一个。
 *
 * 关键实测（2026-10-12，7 轮二分）：详情路由必须携带 commodity_id 等上下文参数，
 * 否则前端只渲染应用壳、不发起 pack_detail。构造规则见 buildDetailUrl。
 */

const DETAIL_ROUTE = 'https://buyin.jinritemai.com/dashboard/merch-picking-library/merch-promoting'

/** 单商品最大等待时间：等待详情接口响应 */
const CAPTURE_TIMEOUT_MS = 25_000
/** 单批上限（避免一次性驱动过多页面触发风控） */
const MAX_BATCH = 500
/** 间隔下限：太快会被平台风控，固定下限 2s */
const MIN_INTERVAL_MS = 2_000

function safePathSegment(value: unknown, fallback: string): string {
  const segment = String(value ?? '').trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/[. ]+$/g, '')
    .slice(0, 80)
  return segment && segment !== '.' && segment !== '..' ? segment : fallback
}

/** 输入行：product_id 或 product_id:commodity_id（冒号分隔） */
interface BatchItemInput {
  productId: string
  commodityId?: string
}

interface BaiyingCaptureAccount {
  id: number
  name: string
  profilePath?: string
  downloadPath?: string
  status: string
}

/** 解析输入行 */
function parseInputLine(line: string): BatchItemInput | null {
  const [a, b] = line.split(':')
  const productId = (a ?? '').trim()
  const commodityId = b ? b.trim() : undefined
  if (!/^\d{8,20}$/.test(productId)) return null
  if (commodityId !== undefined && !/^\d{8,20}$/.test(commodityId)) return null
  return { productId, commodityId }
}

interface BatchRun {
  flag: { canceled: boolean }
}

const RUNNING = new Map<string, BatchRun>()
let seq = 0

/** 最近一次详情响应（账号与商品 → 时间及视频条数），由探针通知更新。 */
const lastCaptured = new Map<string, { time: number; videoCount: number }>()

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

/**
 * 构造商品决策页 URL。
 *
 * ⚠️ 实测结论（2026-10-12，7 轮二分）：直接打开详情路由时，pack_detail 只在
 * URL 携带足够上下文参数时才会被前端触发。最小必需集：
 *   · commodity_id（推广ID，每商品不同）+ product_id（动态）
 *   · commodity_location / id / decision_enter_from / only_query_params /
 *     universal_append / pre_universal_page_params_id（固定值，已验证可复用）
 * 缺少 commodity_id 时页面只渲染壳、不发起 pack_detail（带货数据全 0）。
 */
const FIXED_DETAIL_PARAMS =
  'commodity_location=1&decision_enter_from=pc.selection_square.recommend_main&only_query_params=1&universal_append=1&pre_universal_page_params_id=87e0475e-4976-40dd-83c8-658b76d382de'

function buildDetailUrl(item: BatchItemInput): string {
  const cid = encodeURIComponent(item.commodityId ?? '')
  const pid = encodeURIComponent(item.productId)
  return (
    `${DETAIL_ROUTE}?commodity_id=${cid}&id=${cid}&product_id=${pid}&` + FIXED_DETAIL_PARAMS
  )
}

/** 从百应缓存查询 commodity_id（用户只提供 product_id 时自动补全） */
function resolveCommodity(productId: string): Promise<string | null> {
  return new Promise((resolve) => {
    const handle = runScript('baiying_cache.py', {
      args: ['get', '--product-id', productId],
      onEvent: () => undefined
    })
    void handle.promise.then((outcome) => {
      if (!outcome.ok || !outcome.result) {
        resolve(null)
        return
      }
      const r = outcome.result as { product?: { commodity_id?: string | null } }
      resolve(r?.product?.commodity_id ?? null)
    })
  })
}

/** 轮询等待探针捕获到指定商品（before 之后的新捕获）；null 表示超时。 */
function waitCapture(
  productId: string,
  accountId: number,
  before: number,
  timeoutMs: number,
  flag: { canceled: boolean }
): Promise<number | null> {
  return new Promise((resolve) => {
    const started = Date.now()
    const timer = setInterval(() => {
      if (flag.canceled) {
        clearInterval(timer)
        resolve(null)
        return
      }
      const captured = lastCaptured.get(`${accountId}:${productId}`)
      if (captured && captured.time > before) {
        clearInterval(timer)
        resolve(captured.videoCount)
        return
      }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer)
        resolve(null)
      }
    }, 400)
  })
}

async function runBatch(
  taskId: string,
  sender: Electron.WebContents,
  items: BatchItemInput[],
  account: BaiyingCaptureAccount,
  intervalMs: number,
  flag: { canceled: boolean }
): Promise<void> {
  const emit = (payload: Record<string, unknown>): void => {
    if (!sender.isDestroyed()) sender.send('baiying:event', { taskId, ...payload })
  }

  const browserOptions = { profileKey: `account-${account.id}`, profilePath: account.profilePath }
  let login: Record<string, unknown>
  try {
    login = await commandManagedBrowser('check-account-logins', { accountId: account.id }, browserOptions)
  } catch (error) {
    emit({
      type: 'error',
      message: `检查账号“${account.name}”的百应登录状态失败：${error instanceof Error ? error.message : String(error)}`
    })
    RUNNING.delete(taskId)
    return
  }
  const baiying = login.baiying as { state?: string; message?: string } | undefined
  if (login.ok !== true || baiying?.state !== 'logged_in') {
    emit({ type: 'error', message: `账号“${account.name}”的专属百应未确认登录：${baiying?.message ?? '请先打开该账号百应登录页并完成登录'}` })
    RUNNING.delete(taskId)
    return
  }

  const offCaptured = onProductCaptured((p) => {
    if (p.accountId === account.id) lastCaptured.set(`${account.id}:${p.productId}`, {
      time: Date.now(), videoCount: p.videoUrls.length
    })
  })

  const total = items.length
  let success = 0
  let failed = 0

  try {
    for (let i = 0; i < total; i++) {
      if (flag.canceled) break
      const item = items[i]
      const pid = item.productId
      emit({
        type: 'stage',
        index: i,
        total,
        percent: Math.round((i / total) * 100),
        productId: pid,
        message: `第 ${i + 1}/${total} 个：${pid}`
      })

      const captureKey = `${account.id}:${pid}`
      const before = lastCaptured.get(captureKey)?.time ?? 0

      // commodity_id 是触发 pack_detail 的必要参数：优先用户提供，否则从缓存自动补全
      let commodityId: string | null | undefined = item.commodityId
      if (!commodityId) {
        emit({ type: 'stage', index: i, total, percent: Math.round((i / total) * 100), productId: pid, message: `第 ${i + 1}/${total} 个：${pid}（查缓存补全 commodity_id…）` })
        commodityId = await resolveCommodity(pid)
      }
      if (!commodityId) {
        failed++
        emit({
          type: 'product',
          productId: pid,
          status: 'failed',
          message: '缺少 commodity_id：请先用 商品ID:commodityID 格式，或在百应选品广场浏览/搜索该商品后重试'
        })
        continue
      }

      try {
        const opened = await commandManagedBrowser('open', {
          url: buildDetailUrl({ productId: pid, commodityId })
        }, browserOptions)
        if (opened.ok !== true) throw new Error(String(opened.error ?? '百应商品详情打开失败'))
        if (flag.canceled) break
        const captured = await waitCapture(pid, account.id, before, CAPTURE_TIMEOUT_MS, flag)
        if (flag.canceled) break
        if (captured !== null && captured > 0) {
          success++
          emit({ type: 'product', productId: pid, status: 'success', message: `已从账号“${account.name}”捕获 ${captured} 条视频直链` })
        } else {
          failed++
          emit({
            type: 'product',
            productId: pid,
            status: 'failed',
            message: captured === 0 ? '百应详情已返回，但该商品没有视频直链' : '未捕获到商品详情响应（详情页可能未正常渲染）'
          })
        }
      } catch (error) {
        failed++
        emit({
          type: 'product',
          productId: pid,
          status: 'failed',
          message: `账号“${account.name}”打开商品详情失败：${error instanceof Error ? error.message : String(error)}`
        })
      }

      if (i < total - 1 && !flag.canceled) {
        await sleep(intervalMs + Math.round(Math.random() * 2000))
      }
    }
  } finally {
    offCaptured()
    RUNNING.delete(taskId)
  }

  emit({
    type: 'result',
    total,
    success,
    failed,
    canceled: flag.canceled
  })
}

async function loadBaiyingCaptureAccounts(): Promise<BaiyingCaptureAccount[]> {
  const listed = await runScript('account_manager.py', { args: ['account-list'], onEvent: () => undefined }).promise
  if (!listed.ok || !listed.result) throw new Error(listed.error ?? '读取账号列表失败')
  const rows = (listed.result as { accounts?: Array<Record<string, unknown>> }).accounts ?? []
  const activeRows = rows.filter((row) => row.status === 'active' && Number.isSafeInteger(Number(row.id)) && Number(row.id) > 0)
  const accounts: BaiyingCaptureAccount[] = []
  for (const row of activeRows) {
    const id = Number(row.id)
    const settingsReply = await runScript('selection_settings.py', {
      args: ['get', '--account-id', String(id)],
      onEvent: () => undefined
    }).promise
    if (!settingsReply.ok || !settingsReply.result) {
      throw new Error(`读取账号“${String(row.name ?? id)}”的百应浏览器路径失败：${settingsReply.error ?? '未知错误'}`)
    }
    const settings = (settingsReply.result as {
      settings?: { account?: { browserProfilePath?: unknown; downloadPath?: unknown } }
    }).settings?.account
    const configuredPath = typeof settings?.browserProfilePath === 'string' ? settings.browserProfilePath.trim() : ''
    const downloadPath = typeof settings?.downloadPath === 'string' ? settings.downloadPath.trim() : ''
    accounts.push({
      id,
      name: String(row.name ?? `账号-${id}`),
      status: String(row.status ?? ''),
      ...(configuredPath ? { profilePath: configuredPath } : {}),
      ...(downloadPath ? { downloadPath } : {})
    })
  }
  return accounts
}

async function resolvePromotionOwner(productId: string, accounts: BaiyingCaptureAccount[]): Promise<{
  account: BaiyingCaptureAccount
  commodityId: string
  source: string
}> {
  const stored = await runScript('baiying_promotion_videos.py', {
    args: ['--resolve-account', '--product-id', productId],
    onEvent: () => undefined
  }).promise
  if (!stored.ok || !stored.result) throw new Error(stored.error ?? '无法读取选品账号记录')
  const resolved = stored.result as { found?: boolean; accountId?: number; commodityId?: string; accountSource?: string }
  if (resolved.found) {
    const account = accounts.find((row) => row.id === resolved.accountId)
    if (account) {
      return { account, commodityId: String(resolved.commodityId ?? ''), source: String(resolved.accountSource ?? '选品记录') }
    }
    // 浏览缓存只表明过去由哪个账号看过商品，不代表已经用该账号加车。
    // 明确的加车、选品登记或发布记录仍须严格归属原账号。
    if (resolved.accountSource !== '百应浏览缓存') {
      throw new Error(`该商品的${resolved.accountSource ?? '选品'}账号 ${resolved.accountId ?? '未知'} 已停用或不存在；请启用原账号后重试`)
    }
  }

  // 未有有效归属，或仅有停用账号的旧浏览缓存时，优先匹配现有账号浏览器中的商品页。
  const matches: Array<{ account: BaiyingCaptureAccount; commodityId: string }> = []
  for (const account of accounts) {
    let profilePath: string
    try {
      profilePath = validateManagedProfilePath(`account-${account.id}`, account.profilePath)
    } catch {
      continue
    }
    const probe = await runScript('baiying_promotion_videos.py', {
      args: ['--find-open-product', '--product-id', productId, '--profile-path', profilePath],
      onEvent: () => undefined
    }).promise
    const found = probe.result as { found?: boolean; commodityId?: string } | undefined
    if (probe.ok && found?.found && /^\d{8,20}$/.test(String(found.commodityId ?? ''))) {
      matches.push({ account, commodityId: String(found.commodityId) })
    }
  }
  if (matches.length > 1) throw new Error('多个账号浏览器都打开了该商品，无法确定选品账号；请先通过软件选品记录归属')
  if (matches.length === 1) return { ...matches[0], source: '已打开的百应商品页' }

  // 仅有一个启用账号时，旧缓存的推广 ID 可用作打开商品页的线索；
  // 是否有权限读取带货内容仍由该账号自己的百应登录态和页面响应验证。
  const cachedCommodityId = String(resolved.commodityId ?? '')
  if (resolved.accountSource === '百应浏览缓存' && /^\d{8,20}$/.test(cachedCommodityId) && accounts.length === 1) {
    return { account: accounts[0], commodityId: cachedCommodityId, source: '唯一启用账号（旧浏览缓存仅供定位商品）' }
  }
  if (resolved.accountSource === '百应浏览缓存' && accounts.length > 1) {
    throw new Error('旧浏览缓存对应账号已停用，当前有多个启用账号；无法自动确定下载账号。请先在目标账号专属百应浏览器打开该商品页')
  }
  throw new Error('未找到可用的选品账号记录，也没有启用账号的专属百应浏览器打开该商品页')
}

const CART_DOWNLOAD_ACTIVE = new Set<string>()
const CART_DOWNLOAD_TAILS = new Map<number, Promise<void>>()
const CART_DOWNLOAD_JOBS = new Map<string, Promise<void>>()
let cartDownloadTimer: ReturnType<typeof setInterval> | null = null
let cartDownloadSchedulerRunning = false

function cartDownloadDate(date = new Date()): string {
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function cartDownloadTimestamp(seconds?: number): string {
  const parsed = Number.isFinite(seconds) && seconds && seconds > 0 ? new Date(seconds * 1000) : new Date()
  const date = Number.isFinite(parsed.getTime()) ? parsed : new Date()
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
}

function cartDownloadDestination(folder: string, productId: string, videoId: string, serial: number, publishTime?: number): string {
  const prefix = `${productId}_${videoId}_${String(serial).padStart(3, '0')}_`
  const original = join(folder, `${prefix}${cartDownloadTimestamp(publishTime)}.mp4`)
  if (!existsSync(original)) return original
  // 文件存在却未进入素材库时，保留原文件供核查，使用新时间戳重新下载并登记来源。
  for (let offset = 0; offset < 60; offset++) {
    const retry = join(folder, `${prefix}${cartDownloadTimestamp(Math.floor(Date.now() / 1000) + offset)}.mp4`)
    if (!existsSync(retry)) return retry
  }
  throw new Error(`视频 ${videoId} 的目标文件名已占满，未覆盖原文件`)
}

async function cartState(command: string, accountId?: number, productId?: string, extra: string[] = []): Promise<Record<string, unknown>> {
  const args = ['--db', getClientPaths().databasePath, command]
  if (accountId !== undefined && productId !== undefined) {
    args.push('--account-id', String(accountId), '--product-id', productId)
  }
  const outcome = await runEngineCommand('baiying_cart_download_state.py', [...args, ...extra])
  if (!outcome.ok || !outcome.result || (outcome.result as { ok?: boolean }).ok === false) {
    throw new Error(outcome.error ?? `加车下载状态 ${command} 记录失败`)
  }
  return outcome.result as Record<string, unknown>
}

async function fileContentHash(filePath: string): Promise<string> {
  return new Promise((resolveHash, rejectHash) => {
    const hash = createHash('sha256')
    const stream = createReadStream(filePath)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.on('error', rejectHash)
    stream.on('end', () => resolveHash(hash.digest('hex')))
  })
}

async function existingCartVideos(dbPath: string, accountId: number, productId: string): Promise<{
  videoIds: string[]; files: Array<{ videoId: string; filePath: string }>
}> {
  const outcome = await runScript('baiying_promotion_videos.py', {
    internal: true,
    args: ['--db', dbPath, '--account-id', String(accountId), '--product-id', productId, '--existing-videos'],
    onEvent: () => undefined
  }).promise
  if (!outcome.ok || !outcome.result) throw new Error(outcome.error ?? '无法检查已入库视频')
  const result = outcome.result as { videoIds?: string[]; files?: Array<{ videoId: string; filePath: string }> }
  return { videoIds: result.videoIds ?? [], files: result.files ?? [] }
}

/** 加车成功后下载同款作品；状态落库，重启后可续做，失败不回滚加车。 */
async function runCartVideoDownload(accountId: number, productId: string, commodityId: string, minimumUniqueCount = 0): Promise<void> {
  if (!Number.isSafeInteger(accountId) || accountId < 1 || !/^\d{8,20}$/.test(productId)) {
    throw new Error('加车下载的账号或商品 ID 无效')
  }
  const dbPath = getClientPaths().databasePath
  const quota = await getPromotionVideoQuota(accountId)
  const target = Math.min(500, Math.max(quota, minimumUniqueCount))
  await cartState('schedule', accountId, productId,
    ['--commodity-id', commodityId, '--target-count', String(target)])
  const claim = await cartState('start', accountId, productId)
  if (claim['started'] !== true) return
  let uniqueCount = 0
  let availableCount = 0
  let failure = ''
  let sourceExhausted = false
  try {
    const account = (await loadBaiyingCaptureAccounts()).find((row) => row.id === accountId)
    if (!account) throw new Error(`选品账号 ${accountId} 已停用或不存在`)
    const profilePath = validateManagedProfilePath(`account-${accountId}`, account.profilePath)
    const existing = await existingCartVideos(dbPath, accountId, productId)
    const downloadedIds = new Set(existing.videoIds)
    const uniqueHashes = new Set<string>()
    for (const file of existing.files) uniqueHashes.add(await fileContentHash(file.filePath))
    uniqueCount = uniqueHashes.size
    if (uniqueCount < target) {
      const attempt = Number((claim['job'] as { attemptCount?: number } | undefined)?.attemptCount ?? 1)
      const candidateLimit = Math.min(500, target + 40 * Math.max(1, attempt))
      const args = ['--db', dbPath, '--account-id', String(accountId), '--product-id', productId,
        '--profile-path', profilePath, '--candidate-limit', String(candidateLimit)]
      if (/^\d{8,20}$/.test(commodityId)) args.push('--commodity-id', commodityId)
      const discovered = await runScript('baiying_promotion_videos.py', {
        internal: true, args, onEvent: () => undefined
      }).promise
      if (!discovered.ok || !discovered.result) throw new Error(discovered.error ?? '百应没有返回带货视频')
      const found = discovered.result as { available?: number; videos?: Array<{ videoId: string; url: string; publishTime?: number }> }
      const videos = Array.isArray(found.videos) ? found.videos : []
      availableCount = Math.max(videos.length, Number(found.available ?? videos.length))
      sourceExhausted = availableCount <= candidateLimit || videos.length < candidateLimit
      const accountRoot = account.downloadPath || join(getDownloadDir(), safePathSegment(account.name, `账号-${accountId}`))
      const folder = join(accountRoot, cartDownloadDate(), productId)
      for (const [index, video] of videos.entries()) {
        if (uniqueCount >= target) break
        if (!/^\d{8,25}$/.test(video.videoId) || !/^https:\/\//.test(video.url) || downloadedIds.has(video.videoId)) continue
        const task = {
          id: `cart-${accountId}-${productId}-${video.videoId}`,
          product_id: productId, account_id: accountId, verify_video: true, url: video.url,
          dest: cartDownloadDestination(folder, productId, video.videoId, index + 1, video.publishTime)
        }
        const outcome = await runScript('download_queue.py', {
          internal: true, args: ['--db', dbPath, '--workers', '1', '--max-retries', '2'],
          stdinJson: [task], onEvent: () => undefined
        }).promise
        if (!outcome.ok) {
          failure ||= outcome.error ?? `视频 ${video.videoId} 下载失败`
          continue
        }
        const registered = await existingCartVideos(dbPath, accountId, productId)
        if (!registered.videoIds.includes(video.videoId)) {
          failure ||= `视频 ${video.videoId} 文件未登记到同账号素材库`
          continue
        }
        downloadedIds.add(video.videoId)
        const hash = await fileContentHash(task.dest)
        if (!uniqueHashes.has(hash)) {
          uniqueHashes.add(hash)
          uniqueCount = uniqueHashes.size
        }
      }
    }
    console.info(`[加车后视频] 账号 ${accountId} 商品 ${productId}：不同内容 ${uniqueCount}/${target}，候选 ${availableCount}`)
  } catch (error) {
    failure ||= error instanceof Error ? error.message : String(error)
  }
  await cartState('finish', accountId, productId, [
    '--unique-count', String(uniqueCount), '--available-count', String(availableCount), '--error', failure,
    ...(sourceExhausted ? ['--source-exhausted'] : [])
  ])
  if (failure) throw new Error(failure)
}

/** 选品回调只负责触发，视频下载与后续选品、图转视频和发布并行。 */
export function fireCartVideoDownload(accountId: number, productId: string, commodityId = '', minimumUniqueCount = 0): void {
  const key = `${accountId}:${productId}`
  if (CART_DOWNLOAD_ACTIVE.has(key)) return
  CART_DOWNLOAD_ACTIVE.add(key)
  const previous = CART_DOWNLOAD_TAILS.get(accountId) ?? Promise.resolve()
  const current = previous.then(() => withCartDownloadSlot(
    () => runCartVideoDownload(accountId, productId, commodityId, minimumUniqueCount)))
    .catch((error) => console.error(`[加车后视频] 账号 ${accountId} 商品 ${productId}：${error instanceof Error ? error.message : String(error)}`))
    .finally(() => {
      CART_DOWNLOAD_ACTIVE.delete(key)
      CART_DOWNLOAD_JOBS.delete(key)
      if (CART_DOWNLOAD_TAILS.get(accountId) === current) CART_DOWNLOAD_TAILS.delete(accountId)
    })
  CART_DOWNLOAD_TAILS.set(accountId, current)
  CART_DOWNLOAD_JOBS.set(key, current)
}

async function cartDownloadSchedulerTick(): Promise<void> {
  if (cartDownloadSchedulerRunning) return
  cartDownloadSchedulerRunning = true
  try {
    const result = await cartState('due', undefined, undefined, ['--limit', '20'])
    const due = Array.isArray(result['rows']) ? result['rows'] as Array<{
      accountId: number; productId: string; commodityId: string; targetCount: number
    }> : []
    for (const row of due) fireCartVideoDownload(row.accountId, row.productId, row.commodityId, row.targetCount)
  } catch (error) {
    console.warn(`[加车后视频] 恢复任务检查失败：${error instanceof Error ? error.message : String(error)}`)
  } finally {
    cartDownloadSchedulerRunning = false
  }
}

export function startCartVideoDownloadScheduler(): void {
  if (cartDownloadTimer) return
  cartDownloadTimer = setInterval(() => { void cartDownloadSchedulerTick() }, 60_000)
  setTimeout(() => { void cartDownloadSchedulerTick() }, 15_000)
}

/** 发布后先等同商品的加车下载结束，再决定是否需要调用千川接口重新下载。 */
export async function waitForCartVideoDownload(accountId: number, productId: string): Promise<void> {
  await CART_DOWNLOAD_JOBS.get(`${accountId}:${productId}`)
}

/** 发布后缺素材时按原账号补下载；远端合并重复素材时可追加一个新内容。 */
export async function retryCartVideoDownload(accountId: number, productId: string, additionalUnique = 0): Promise<void> {
  await waitForCartVideoDownload(accountId, productId)
  const owner = await resolvePromotionOwner(productId, await loadBaiyingCaptureAccounts())
  if (owner.account.id !== accountId) throw new Error('商品选品账号与发布账号不一致，已停止补下载')
  const current = await cartState('get', accountId, productId)
  const job = current['job'] as { uniqueCount?: number } | null
  const requested = additionalUnique > 0 ? Number(job?.uniqueCount ?? 0) + additionalUnique : 0
  await runCartVideoDownload(accountId, productId, owner.commodityId, requested)
}

export async function getCartVideoDownloadStatus(accountId: number, productId: string): Promise<Record<string, unknown> | null> {
  const result = await cartState('get', accountId, productId)
  return (result['job'] as Record<string, unknown> | null) ?? null
}

/** 发布后入千川沿用该选品账号的同款作品下载额度。 */
export async function getPromotionVideoQuota(accountId: number): Promise<number> {
  const outcome = await runScript('baiying_promotion_videos.py', {
    internal: true,
    args: ['--db', getClientPaths().databasePath, '--account-id', String(accountId), '--get-quota'],
    onEvent: () => undefined
  }).promise
  const limit = Number((outcome.result as { quota?: unknown } | undefined)?.quota)
  if (!outcome.ok || !Number.isSafeInteger(limit) || limit < 10 || limit > 500) {
    throw new Error(outcome.error ?? '无法读取该账号的带货作品下载额度（须为 10–500 条）')
  }
  return limit
}

// ---------------------------------------------------------------- IPC 注册

export function registerBaiyingBatchIpc(): void {
  ipcMain.handle('baiying:cartDownloadStatus', async (_event, accountId: number, productId: string) => {
    try {
      return await cartState('get', accountId, productId)
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })
  ipcMain.handle('baiying:clipboardProductId', async () => {
    const copied = (await clipboard.readText()).trim()
    const match = /^(?:商品\s*ID\s*[:：]?\s*)?(\d{8,20})$/i.exec(copied)
    if (!match) {
      return { ok: false, error: '剪贴板里不是 8–20 位商品 ID；请先在百应商品页点击「ID」复制' }
    }
    return { ok: true, productId: match[1] }
  })

  ipcMain.handle('baiying:promotionQuota', async (_event, params: { accountId?: number; limit?: number }) => {
    try {
      const accountId = Number(params?.accountId)
      if (!Number.isSafeInteger(accountId) || accountId < 1) throw new Error('账号 ID 无效')
      if (!(await loadBaiyingCaptureAccounts()).some((account) => account.id === accountId)) {
        throw new Error('账号不存在或已停用')
      }
      const args = ['--db', getClientPaths().databasePath, '--account-id', String(accountId)]
      if (params.limit === undefined) args.push('--get-quota')
      else {
        if (!Number.isSafeInteger(params.limit) || params.limit < 10 || params.limit > 500) {
          throw new Error('每账号下载额度须为 10–500 条')
        }
        args.push('--set-quota', String(params.limit))
      }
      const outcome = await runScript('baiying_promotion_videos.py', { args, onEvent: () => undefined }).promise
      if (!outcome.ok || !outcome.result) throw new Error(outcome.error ?? '读取下载额度失败')
      return outcome.result
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })

  ipcMain.handle('baiying:promotionVideos', async (_event, params: { productId?: string }) => {
    try {
      const productId = String(params?.productId ?? '').trim()
      if (!/^\d{8,20}$/.test(productId)) throw new Error('商品 ID 格式无效')
      const { account, commodityId, source } = await resolvePromotionOwner(productId, await loadBaiyingCaptureAccounts())
      const accountId = account.id
      const options = { profileKey: `account-${accountId}`, profilePath: account.profilePath }
      const login = await commandManagedBrowser('check-account-logins', { accountId }, options)
      const baiying = login.baiying as { state?: string; message?: string } | undefined
      if (login.ok !== true || baiying?.state !== 'logged_in') {
        throw new Error(`账号“${account.name}”的百应尚未登录：${baiying?.message ?? '请先登录专属浏览器'}`)
      }
      const profilePath = validateManagedProfilePath(options.profileKey, options.profilePath)
      const args = ['--account-id', String(accountId), '--product-id', productId, '--profile-path', profilePath]
      if (commodityId) args.push('--commodity-id', commodityId)
      const outcome = await runScript('baiying_promotion_videos.py', {
        args,
        onEvent: () => undefined
      }).promise
      if (!outcome.ok || !outcome.result) throw new Error(outcome.error ?? '读取百应同款带货作品失败')
      const result = outcome.result as { commodityId?: string }
      if (result.commodityId && /^\d{8,20}$/.test(result.commodityId)) {
        const saved = await runScript('baiying_cache.py', {
          args: [
            'set-commodity', '--product-id', productId,
            '--commodity-id', result.commodityId, '--account-id', String(accountId)
          ],
          onEvent: () => undefined
        }).promise
        if (!saved.ok) return {
          ...(outcome.result as Record<string, unknown>), accountId, accountName: account.name,
          downloadPath: account.downloadPath ?? '', accountSource: source,
          cacheWarning: saved.error ?? '推广 ID 缓存失败'
        }
      }
      return {
        ...(outcome.result as Record<string, unknown>), accountId, accountName: account.name,
        downloadPath: account.downloadPath ?? '', accountSource: source
      }
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })

  ipcMain.handle('baiying:batchAccounts', async () => {
    try {
      return { ok: true, accounts: await loadBaiyingCaptureAccounts() }
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error) }
    }
  })

  ipcMain.handle(
    'baiying:batchStart',
    async (
      event,
      params: { productIds?: string[]; accountId?: number; intervalMs?: number }
    ): Promise<{ ok: boolean; taskId?: string; error?: string }> => {
      const items: BatchItemInput[] = (Array.isArray(params?.productIds) ? params.productIds : [])
        .map((s) => String(s).trim())
        .filter(Boolean)
        .map(parseInputLine)
        .filter((x): x is BatchItemInput => x !== null)
      if (items.length === 0) {
        return {
          ok: false,
          error: '商品 ID 列表为空或格式不正确（应为 8~20 位数字，可用 商品ID:commodityID 形式携带推广ID）'
        }
      }
      if (items.length > MAX_BATCH) return { ok: false, error: `单批最多 ${MAX_BATCH} 个商品 ID，请分批` }
      const accountId = Number(params?.accountId)
      if (!Number.isSafeInteger(accountId) || accountId < 1) return { ok: false, error: '请先选择百应采集账号' }
      let account: BaiyingCaptureAccount
      try {
        account = (await loadBaiyingCaptureAccounts()).find((item) => item.id === accountId) as BaiyingCaptureAccount
        if (!account) return { ok: false, error: '所选账号不存在或已停用，请刷新账号列表后重试' }
      } catch (error) {
        return { ok: false, error: error instanceof Error ? error.message : String(error) }
      }

      const intervalMs = Math.max(MIN_INTERVAL_MS, Math.min(60_000, params?.intervalMs ?? 8_000))
      const taskId = `baiying-batch-${++seq}`
      const flag = { canceled: false }
      RUNNING.set(taskId, { flag })

      void runBatch(taskId, event.sender, items, account, intervalMs, flag)
      return { ok: true, taskId }
    }
  )

  ipcMain.handle('baiying:batchCancel', (_e, taskId: string): { ok: boolean; alreadyEnded?: boolean } => {
    const run = RUNNING.get(taskId)
    if (!run) return { ok: true, alreadyEnded: true }
    run.flag.canceled = true
    return { ok: true }
  })
}

/** 应用退出前收尾：停掉所有进行中的批量抓取 */
export function disposeBaiyingBatch(): void {
  if (cartDownloadTimer) clearInterval(cartDownloadTimer)
  cartDownloadTimer = null
  for (const run of RUNNING.values()) {
    run.flag.canceled = true
  }
  RUNNING.clear()
}
