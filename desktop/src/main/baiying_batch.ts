import { ipcMain, type WebContents } from 'electron'
import { onProductCaptured } from './probe'
import { runScript } from './engine'
import { commandManagedBrowser } from './external_browser'

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

/** 输入行：product_id 或 product_id:commodity_id（冒号分隔） */
interface BatchItemInput {
  productId: string
  commodityId?: string
}

interface BaiyingCaptureAccount {
  id: number
  name: string
  profilePath?: string
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
      settings?: { account?: { browserProfilePath?: unknown } }
    }).settings?.account
    const configuredPath = typeof settings?.browserProfilePath === 'string' ? settings.browserProfilePath.trim() : ''
    accounts.push({
      id,
      name: String(row.name ?? `账号-${id}`),
      status: String(row.status ?? ''),
      ...(configuredPath ? { profilePath: configuredPath } : {})
    })
  }
  return accounts
}

// ---------------------------------------------------------------- IPC 注册

export function registerBaiyingBatchIpc(): void {
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
  for (const run of RUNNING.values()) {
    run.flag.canceled = true
  }
  RUNNING.clear()
}
