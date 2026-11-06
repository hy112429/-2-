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
const CATEGORY_API_RE = /(category|cate_info|cate_list|cate_tree|cat_list)/i
const CATEGORY_CONTAINER_RE = /(categories|category_list|category_tree|category_nodes|cate_info|cate_list|cate_tree|cat_list|first_level_categories|first_category_list)/i
let lastCategorySignature = ''

/** 捕获通知（包含零视频详情，便于查询立即区分“无视频”和“未响应”） */
export interface ProductCaptured {
  productId: string
  videoUrls: string[]
  accountId: number | null
}
const capturedListeners = new Set<(p: ProductCaptured) => void>()

/** 订阅商品捕获事件；返回取消订阅函数 */
export function onProductCaptured(cb: (p: ProductCaptured) => void): () => void {
  capturedListeners.add(cb)
  return () => {
    capturedListeners.delete(cb)
  }
}

function notifyCaptured(productId: string, videoUrls: string[], accountId: number | null = null): void {
  for (const cb of capturedListeners) {
    try {
      cb({ productId, videoUrls, accountId })
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
  videoUrl: string | null
  videoUrls: string[]
  images: string[]
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
    if (typeof productId !== 'string') {
      return null
    }
    const collectVideoUrls = (value: unknown, inVideoField = false): string[] => {
      if (typeof value === 'string') return inVideoField && /^https?:\/\//i.test(value) ? [value] : []
      if (Array.isArray(value)) return value.flatMap((item) => collectVideoUrls(item, inVideoField))
      if (!value || typeof value !== 'object') return []
      const urls: string[] = []
      for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
        if (/(cover|poster|thumb|image|audio|subtitle)/i.test(key)) continue
        const videoContext = inVideoField || /(video|play|movie)/i.test(key)
        if (videoContext && /^(url|origin_url|video_url|play_url|download_url|src)$/i.test(key) && typeof child === 'string') {
          if (/^https?:\/\//i.test(child)) urls.push(child)
        } else {
          urls.push(...collectVideoUrls(child, videoContext))
        }
      }
      return urls
    }
    const collectUrls = (value: unknown): string[] => {
      if (!Array.isArray(value)) return []
      return value.flatMap((item) => {
        const url = typeof item === 'string' ? item
          : item && typeof item === 'object'
            ? String((item as Record<string, unknown>).url ?? (item as Record<string, unknown>).origin_url ?? '')
            : ''
        return /^https?:\/\//i.test(url) ? [url] : []
      })
    }
    const images = [...new Set([...collectUrls(base?.big_imgs), ...collectUrls(base?.images), ...collectUrls(base?.white_background)])].slice(0, 25)
    const videoUrls = [...new Set(collectVideoUrls(media))]
    const videoUrl = videoUrls[0] ?? null
    const cover = typeof media?.cover_url === 'string' ? media.cover_url
      : typeof base?.cover === 'string' ? base.cover : ''
    if (!videoUrl && !images.length) return null
    return {
      productId,
      title: typeof base?.title === 'string' ? base.title : '',
      cover,
      cosRatio: typeof cos === 'number' ? cos : null,
      videoUrl,
      videoUrls,
      images
    }
  } catch {
    return null
  }
}

/** 经引擎白名单脚本落库（fire-and-forget，失败只记日志） */
function upsertProduct(p: PackDetail, accountId: number | null = null): void {
  const now = Date.now()
  const captureKey = `${accountId ?? 'unknown'}:${p.productId}`
  if (now - (lastUpsert.get(captureKey) ?? 0) < DEBOUNCE_MS) return
  lastUpsert.set(captureKey, now)

  const args = ['upsert', '--product-id', p.productId, '--detail-stdin']
  if (p.title) args.push('--title', p.title)
  if (p.cover) args.push('--cover', p.cover)
  if (p.cosRatio !== null) args.push('--cos-ratio', String(p.cosRatio))

  const handle = runScript('baiying_cache.py', {
    args,
    stdinJson: { gallery: p.images, videoUrls: p.videoUrls, accountId },
    onEvent: () => undefined
  })
  void handle.promise.then((outcome) => {
      logCompact({
      action: 'upsert',
      productId: p.productId,
        accountId,
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

/** 从百应真实响应中提取带父子关系的类目节点；只读取平台返回内容，不构造请求参数。 */
function extractCategoryTree(body: unknown): Array<{ id: string; name: string; children: unknown[] }> {
  const idKeys = ['category_id', 'cate_id', 'cat_id', 'first_category_id', 'second_category_id', 'third_category_id', 'id', 'value']
  const nameKeys = ['category_name', 'cate_name', 'cat_name', 'first_category_name', 'second_category_name', 'third_category_name', 'name', 'label']
  const childKeys = ['children', 'childs', 'child_list', 'sub_categories', 'sub_category_list', 'child_category', 'category_list', 'cate_list', 'cate_info']

  const normalize = (value: unknown): Array<{ id: string; name: string; children: unknown[] }> => {
    if (!Array.isArray(value)) return []
    const nodes: Array<{ id: string; name: string; children: unknown[] }> = []
    for (const item of value) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue
      const record = item as Record<string, unknown>
      const idValue = idKeys.map((key) => record[key]).find((entry) => typeof entry === 'string' || typeof entry === 'number')
      const nameValue = nameKeys.map((key) => record[key]).find((entry) => typeof entry === 'string')
      if (idValue === undefined || typeof nameValue !== 'string' || !nameValue.trim()) continue
      const rawChildren = childKeys.map((key) => record[key]).find(Array.isArray)
      nodes.push({ id: String(idValue), name: nameValue.trim(), children: rawChildren ? normalize(rawChildren) : [] })
    }
    return nodes
  }

  const visit = (value: unknown, underCategoryKey = false): Array<{ id: string; name: string; children: unknown[] }> => {
    if (!value || typeof value !== 'object') return []
    if (Array.isArray(value)) return underCategoryKey ? normalize(value) : value.flatMap((item) => visit(item, false))
    const record = value as Record<string, unknown>
    for (const [key, child] of Object.entries(record)) {
      if (CATEGORY_CONTAINER_RE.test(key)) {
        const found = normalize(child)
        if (found.length) return found
        const nested = visit(child, true)
        if (nested.length) return nested
      }
    }
    for (const child of Object.values(record)) {
      const nested = visit(child, false)
      if (nested.length) return nested
    }
    return []
  }

  const catalog = visit(body)
  return catalog
}

function captureCategoryCatalog(body: string, url: string): void {
  let parsed: unknown
  try {
    parsed = JSON.parse(body)
  } catch {
    return
  }
  const categories = extractCategoryTree(parsed)
  const categoryPaths: string[] = []
  const scanShape = (value: unknown, path: string, depth: number): void => {
    if (depth > 7 || !value || typeof value !== 'object') return
    if (Array.isArray(value)) {
      if (/categor|cate|cat/i.test(path)) categoryPaths.push(`${path}[${value.length}]`)
      value.slice(0, 2).forEach((item, index) => scanShape(item, `${path}[${index}]`, depth + 1))
      return
    }
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      const childPath = path ? `${path}.${key}` : key
      if (/categor|cate|cat/i.test(key)) {
        const size = Array.isArray(child) ? `[${child.length}]` : ''
        categoryPaths.push(`${childPath}${size}`)
      }
      scanShape(child, childPath, depth + 1)
    }
  }
  scanShape(parsed, '', 0)
  if (categoryPaths.length) {
    let path = ''
    try { path = new URL(url).pathname } catch { path = '百应接口' }
    logCompact({ action: 'category-scan', path, extracted: categories.length, fields: Array.from(new Set(categoryPaths)).slice(0, 30) })
  }
  if (!categories.length) return
  let mode = 'merge'
  let parentId = ''
  try {
    const params = new URL(url).searchParams
    const categoryType = params.get('cate_type') ?? params.get('category_type') ?? ''
    parentId = params.get('parent_id') ?? params.get('parent_category_id') ?? params.get('category_id') ?? params.get('cate_id') ?? ''
    if (categoryType === '1') mode = 'root-snapshot'
    else if (categoryType && categoryType !== '1' && parentId) mode = 'children'
  } catch {
    /* 没有可解析的请求参数时按增量目录合并 */
  }
  const signature = JSON.stringify(categories)
  if (`${mode}:${parentId}:${signature}` === lastCategorySignature) return
  lastCategorySignature = `${mode}:${parentId}:${signature}`
  const args = ['catalog', 'save', '--mode', mode]
  if (parentId) args.push('--parent-id', parentId)
  const handle = runScript('selection_settings.py', {
    args,
    stdinJson: { categories },
    onEvent: () => undefined
  })
  void handle.promise.then((outcome) => {
    let source = '百应类目接口'
    try { source = new URL(url).pathname } catch { /* 保留脱敏来源名 */ }
    logCompact({ action: 'category-catalog', count: categories.length, source, ok: outcome.ok })
    if (!outcome.ok && lastCategorySignature === `${mode}:${parentId}:${signature}`) lastCategorySignature = ''
  })
}

/** 处理来自 Electron 探针或外部托管 Chrome CDP 桥接的百应响应。 */
export function captureExternalBaiyingResponse(url: string, body: string, accountId: number | null = null): void {
  if (!url.includes('buyin')) return

  if (url.includes('pack_detail')) {
    const parsed = parsePackDetail(body)
    if (parsed) {
      logCompact({ action: 'capture', productId: parsed.productId, accountId, path: new URL(url).pathname, videoCount: parsed.videoUrls.length, imageCount: parsed.images.length, source: 'cdp' })
      notifyCaptured(parsed.productId, parsed.videoUrls, accountId)
      upsertProduct(parsed, accountId)
    }
    return
  }

  if (!LIST_API_RE.test(url) && !CATEGORY_API_RE.test(url)) return
  let parsedBody: unknown
  try {
    parsedBody = JSON.parse(body)
  } catch {
    return
  }
  captureCategoryCatalog(body, url)
  const pairs: Array<{ productId: string; commodityId: string }> = []
  collectCommodityPairs(parsedBody, pairs)
  if (pairs.length > 0) {
    logCompact({ action: 'list', path: new URL(url).pathname, pairs: pairs.length, source: 'cdp' })
    linkCommodities(pairs)
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
        // 过滤：只收完整视频流直链（media-video），排除音频流（media-audio）
        // 与字节范围分片（br=/bt=，如 br=49&bt=49 是 Range 请求仅 1 字节，非可下载文件）。
        // 实测教训（2026-10-15）：抓到 audio-und-mp4a 分片当视频直链 → 下载 404。
        if (url.includes('media-audio') || /[?&]br=\d+&bt=\d+/.test(url)) return
        logCompact({ action: 'vod', url: url.slice(0, 400) })
        notifyVodCaptured(url)
      }
      return
    }
    const requestId = params?.requestId as string | undefined
    if (!requestId) return

    // 商品详情、选品列表/搜索和类目接口统一走同一响应处理器。
    if (url.includes('pack_detail') || LIST_API_RE.test(url) || CATEGORY_API_RE.test(url)) {
      try {
        void wc.debugger
          .sendCommand('Network.getResponseBody', { requestId })
          .then(({ body }) => {
            if (body) captureExternalBaiyingResponse(url, body)
          })
          .catch(() => undefined)
      } catch {
        /* 忽略 */
      }
    }
  })
}
