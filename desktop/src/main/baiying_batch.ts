import { ipcMain, BrowserWindow, type WebContents } from 'electron'
import { onProductCaptured } from './probe'
import { runScript } from './engine'
import { ensureLoginView, attachLoginViewAt, detachLoginView, checkLoginState } from './login'

/**
 * 百应「半自动批量浏览」驱动（T-15）
 * ─────────────────────────────────────────────────────────────
 * 为什么需要它：pack_detail 接口带 a_bogus / msToken 签名，引擎无法直接构造请求
 * （合规不逆向）。替代方案是让**已登录的真实浏览器**逐个打开商品详情页——
 * 签名由浏览器天然生成，客户端只做响应解析（探针 probe.ts）并落库。
 *
 * 流程：商品ID列表 → 逐个导航登录视图到该商品的「商品决策页」路由 → 探针捕获
 * pack_detail → 写入 baiying_products 缓存 → 固定间隔 + 随机抖动限速 → 下一个。
 *
 * 关键实测（2026-10-12，7 轮二分）：详情路由必须携带 commodity_id 等上下文参数，
 * 否则前端只渲染应用壳、不发起 pack_detail。构造规则见 buildDetailUrl。
 */

const DETAIL_ROUTE = 'https://buyin.jinritemai.com/dashboard/merch-picking-library/merch-promoting'

/** 单商品最大等待时间：页面加载 20s + 捕获 25s */
const LOAD_TIMEOUT_MS = 20_000
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

/** 最近一次捕获的时间戳（productId → ts），由探针通知更新 */
const lastCaptured = new Map<string, number>()

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

/** 导航到详情页，等 did-finish-load / did-fail-load（超时兜底） */
function loadDetail(wc: WebContents, url: string): Promise<{ ok: boolean; code?: number; desc?: string }> {
  return new Promise((resolve) => {
    let settled = false
    const finish = (r: { ok: boolean; code?: number; desc?: string }): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      wc.removeListener('did-finish-load', onFinish)
      wc.removeListener('did-fail-load', onFail)
      resolve(r)
    }
    const onFinish = (): void => finish({ ok: true })
    const onFail = (_e: unknown, code: number, desc: string): void => finish({ ok: false, code, desc })
    const timer = setTimeout(() => finish({ ok: false, code: -2, desc: '页面加载超时' }), LOAD_TIMEOUT_MS)

    wc.on('did-finish-load', onFinish)
    wc.on('did-fail-load', onFail)

    // 同一 URL 再次导航不会触发加载，强制 reload
    const target = new URL(url)
    const current = wc.getURL()
    const currentUrl = current ? new URL(current) : null
    if (currentUrl && currentUrl.origin === target.origin && currentUrl.pathname === target.pathname) {
      wc.reload()
    } else {
      void wc.loadURL(url).catch(() => finish({ ok: false, code: -1, desc: 'loadURL 抛出异常' }))
    }
  })
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

/** 轮询等待探针捕获到指定商品（before 之后的新捕获），超时返回 false */
function waitCapture(
  productId: string,
  before: number,
  timeoutMs: number,
  flag: { canceled: boolean }
): Promise<boolean> {
  return new Promise((resolve) => {
    const started = Date.now()
    const timer = setInterval(() => {
      if (flag.canceled) {
        clearInterval(timer)
        resolve(false)
        return
      }
      if ((lastCaptured.get(productId) ?? 0) > before) {
        clearInterval(timer)
        resolve(true)
        return
      }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer)
        resolve(false)
      }
    }, 400)
  })
}

async function runBatch(
  taskId: string,
  sender: Electron.WebContents,
  items: BatchItemInput[],
  intervalMs: number,
  flag: { canceled: boolean }
): Promise<void> {
  const emit = (payload: Record<string, unknown>): void => {
    if (!sender.isDestroyed()) sender.send('baiying:event', { taskId, ...payload })
  }

  // 前置条件：百应已登录（驱动的是登录视图，未登录打开详情页必然拿不到数据）
  const login = await checkLoginState()
  const baiying = login.targets.find((t) => t.id === 'baiying')
  if (!baiying?.loggedIn) {
    emit({ type: 'error', message: '百应未登录：请先在「巨量百应」工作台扫码登录后重试' })
    RUNNING.delete(taskId)
    return
  }

  const view = ensureLoginView()
  const wc = view.webContents

  // 挂到窗口给真实视口（0×0 视图会导致详情懒加载不触发、pack_detail 不发请求）
  const host = BrowserWindow.getAllWindows().find((w) => !w.isDestroyed())
  if (host) {
    const [width, height] = host.getContentSize()
    attachLoginViewAt({ x: 0, y: 0, width, height })
  }

  const offCaptured = onProductCaptured((p) => {
    lastCaptured.set(p.productId, Date.now())
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

      const before = lastCaptured.get(pid) ?? 0

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

      const loaded = await loadDetail(wc, buildDetailUrl({ productId: pid, commodityId }))
      if (flag.canceled) break

      // -3 是主动中断（reload 等）不算失败，仍进入捕获等待
      if (loaded.ok || loaded.code === -3) {
        const captured = await waitCapture(pid, before, CAPTURE_TIMEOUT_MS, flag)
        if (flag.canceled) break
        if (captured) {
          success++
          emit({ type: 'product', productId: pid, status: 'success', message: '已捕获视频直链' })
        } else {
          failed++
          emit({
            type: 'product',
            productId: pid,
            status: 'failed',
            message: '未捕获到视频直链（详情页可能未正常渲染）'
          })
        }
      } else {
        failed++
        emit({
          type: 'product',
          productId: pid,
          status: 'failed',
          message: `页面加载失败：${loaded.desc ?? loaded.code}`
        })
      }

      if (i < total - 1 && !flag.canceled) {
        await sleep(intervalMs + Math.round(Math.random() * 2000))
      }
    }
  } finally {
    offCaptured()
    detachLoginView()
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

// ---------------------------------------------------------------- IPC 注册

export function registerBaiyingBatchIpc(): void {
  ipcMain.handle(
    'baiying:batchStart',
    async (
      event,
      params: { productIds?: string[]; intervalMs?: number }
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

      const intervalMs = Math.max(MIN_INTERVAL_MS, Math.min(60_000, params?.intervalMs ?? 8_000))
      const taskId = `baiying-batch-${++seq}`
      const flag = { canceled: false }
      RUNNING.set(taskId, { flag })

      void runBatch(taskId, event.sender, items, intervalMs, flag)
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
