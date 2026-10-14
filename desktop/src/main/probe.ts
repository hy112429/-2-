import { appendFileSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import type { WebContents } from 'electron'
import { runScript } from './engine'

/**
 * 百应商品数据探针（T-15）：用 CDP（webContents.debugger）监听登录视图的网络请求，
 * 命中商品详情接口 pack_detail 时取完整响应体，解析出
 * `product_id → {标题, 封面, 佣金率, video_url}` 并落库（baiying_cache.py → baiying_products 表）。
 *
 * 为什么必须走探针：pack_detail 接口带 a_bogus / msToken 签名，引擎无法直接构造请求，
 * 且签名属于平台风控逻辑、**不可逆向**（合规边界）。探针复用的是用户真实浏览行为，
 * 签名由浏览器天然生成，我们只做响应解析。
 *
 * 调试日志保留到 %TEMP%\baiying_probe.jsonl（紧凑行，不再存全文）。
 */

const PROBE_LOG = join(tmpdir(), 'baiying_probe.jsonl')

/** 同一商品在防抖窗口内只落库一次，避免详情页重复请求造成进程开销 */
const lastUpsert = new Map<string, number>()
const DEBOUNCE_MS = 5000

/**
 * 选品广场列表/搜索接口关键词：这些接口的响应含 product_id → promotion_id(commodity_id) 映射，
 * 用于在用户浏览选品时自动补全 commodity_id（pack_detail 触发所必需）。
 */
const LIST_API_RE = /(selection_square|search\/query\/recommend|material_list|channel_activity|selection\/common)/

/** 捕获通知（批量浏览驱动靠它感知某个商品已抓到视频直链） */
export interface ProductCaptured {
  productId: string
  videoUrl: string
}
const capturedListeners = new Set<(p: ProductCaptured) => void>()

/** 订阅商品捕获事件；返回取消订阅函数 */
export function onProductCaptured(cb: (p: ProductCaptured) => void): () => void {
  capturedListeners.add(cb)
  return () => {
    capturedListeners.delete(cb)
  }
}

function notifyCaptured(productId: string, videoUrl: string): void {
  for (const cb of capturedListeners) {
    try {
      cb({ productId, videoUrl })
    } catch {
      /* 单个订阅者出错不影响其余 */
    }
  }
}

/** 头条/西瓜 vod 直链捕获通知（头条批量驱动靠它感知直链已拿到） */
export interface VodCaptured {
  url: string
}
const vodListeners = new Set<(v: VodCaptured) => void>()

/** 订阅头条 vod 直链捕获事件；返回取消订阅函数 */
export function onVodCaptured(cb: (v: VodCaptured) => void): () => void {
  vodListeners.add(cb)
  return () => {
    vodListeners.delete(cb)
  }
}

function notifyVodCaptured(url: string): void {
  for (const cb of vodListeners) {
    try {
      cb({ url })
    } catch {
      /* 单个订阅者出错不影响其余 */
    }
  }
}

/** pack_detail 响应体解析结果 */
interface PackDetail {
  productId: string
  title: string
  cover: string
  cosRatio: number | null
  videoUrl: string
}

function logCompact(entry: Record<string, unknown>): void {
  try {
    appendFileSync(PROBE_LOG, JSON.stringify({ t: new Date().toISOString(), ...entry }) + '\n')
  } catch {
    /* 日志写失败不致命 */
  }
}

/** 解析 pack_detail 响应体；结构对不上返回 null（不是 pack_detail 就静默忽略） */
function parsePackDetail(body: string): PackDetail | null {
  try {
    const data = JSON.parse(body)?.data
    const product = data?.model?.product
    const base = product?.product_base
    const media = base?.media
    const cos = product?.product_cos?.cos_label?.cos?.cos_ratio
    const productId = data?.product_id
    const videoUrl = media?.video_url
    if (typeof productId !== 'string' || typeof videoUrl !== 'string' || !videoUrl) {
      return null
    }
    return {
      productId,
      title: typeof base?.title === 'string' ? base.title : '',
      cover: typeof media?.cover_url === 'string' ? media.cover_url : '',
      cosRatio: typeof cos === 'number' ? cos : null,
      videoUrl
    }
  } catch {
    return null
  }
}

/** 经引擎白名单脚本落库（fire-and-forget，失败只记日志） */
function upsertProduct(p: PackDetail): void {
  const now = Date.now()
  if (now - (lastUpsert.get(p.productId) ?? 0) < DEBOUNCE_MS) return
  lastUpsert.set(p.productId, now)

  const args = [
    'upsert',
    '--product-id', p.productId,
    '--title', p.title,
    '--cover', p.cover,
    '--video-url', p.videoUrl
  ]
  if (p.cosRatio !== null) args.push('--cos-ratio', String(p.cosRatio))

  const handle = runScript('baiying_cache.py', { args, onEvent: () => undefined })
  void handle.promise.then((outcome) => {
    logCompact({
      action: 'upsert',
      productId: p.productId,
      title: p.title.slice(0, 40),
      ok: outcome.ok,
      error: outcome.error ?? undefined
    })
  })
}

/** 递归收集 JSON 里的 product_id → promotion_id/commodity_id 映射（选品列表/搜索接口） */
function collectCommodityPairs(
  obj: unknown,
  out: Array<{ productId: string; commodityId: string }>
): void {
  if (!obj || typeof obj !== 'object') return
  if (Array.isArray(obj)) {
    for (const it of obj) collectCommodityPairs(it, out)
    return
  }
  const record = obj as Record<string, unknown>
  const pid = record['product_id']
  const cid = record['promotion_id'] ?? record['commodity_id']
  if (typeof pid === 'string' && typeof cid === 'string') {
    if (/^\d{8,20}$/.test(pid) && /^\d{8,20}$/.test(cid)) {
      out.push({ productId: pid, commodityId: cid })
    }
  }
  for (const value of Object.values(record)) collectCommodityPairs(value, out)
}

/** 把选品列表响应里的 (product_id, commodity_id) 补全落库 */
function linkCommodities(pairs: Array<{ productId: string; commodityId: string }>): void {
  for (const { productId, commodityId } of pairs) {
    const now = Date.now()
    if (now - (lastUpsert.get(productId) ?? 0) < DEBOUNCE_MS) continue
    lastUpsert.set(productId, now)

    const handle = runScript('baiying_cache.py', {
      args: ['set-commodity', '--product-id', productId, '--commodity-id', commodityId],
      onEvent: () => undefined
    })
    void handle.promise.then((outcome) => {
      logCompact({
        action: 'link',
        productId,
        commodityId,
        ok: outcome.ok,
        error: outcome.error ?? undefined
      })
    })
  }
}

export function attachProbe(wc: WebContents): void {
  try {
    wc.debugger.attach('1.3')
  } catch {
    return // 已附着则跳过
  }

  void wc.debugger.sendCommand('Network.enable')

  wc.debugger.on('message', (_event, method, params) => {
    if (method !== 'Network.responseReceived') return
    const url = params?.response?.url ?? ''
    if (!url.includes('buyin')) {
      // 头条/西瓜视频：探针捕获 vod 直链（页面被 JS 防护，直链是最可行来源）
      if (url.includes('toutiaovod.com')) {
        logCompact({ action: 'vod', url: url.slice(0, 400) })
        notifyVodCaptured(url)
      }
      return
    }
    const requestId = params?.requestId as string | undefined
    if (!requestId) return

    // 详情接口 pack_detail：解析并落库商品完整信息
    if (url.includes('pack_detail')) {
      try {
        void wc.debugger
          .sendCommand('Network.getResponseBody', { requestId })
          .then(({ body }) => {
            const parsed = body ? parsePackDetail(body) : null
            if (parsed) {
              logCompact({ action: 'capture', productId: parsed.productId, url: url.slice(0, 120) })
              notifyCaptured(parsed.productId, parsed.videoUrl)
              upsertProduct(parsed)
            }
          })
          .catch(() => undefined)
      } catch {
        /* 忽略 */
      }
      return
    }

    // 选品列表/搜索接口：补全 product_id → commodity_id 映射
    if (LIST_API_RE.test(url)) {
      try {
        void wc.debugger
          .sendCommand('Network.getResponseBody', { requestId })
          .then(({ body }) => {
            if (!body) return
            let parsedBody: unknown
            try {
              parsedBody = JSON.parse(body)
            } catch {
              return
            }
            const pairs: Array<{ productId: string; commodityId: string }> = []
            collectCommodityPairs(parsedBody, pairs)
            if (pairs.length > 0) {
              logCompact({ action: 'list', url: url.slice(0, 120), pairs: pairs.length })
              linkCommodities(pairs)
            }
          })
          .catch(() => undefined)
      } catch {
        /* 忽略 */
      }
    }
  })
}
