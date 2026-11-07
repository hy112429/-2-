<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'
import QianchuanVideoQueryPanel from './QianchuanVideoQueryPanel.vue'

/**
 * 商品查询主界面（真实数据链路）
 *
 * 数据链路：商品 ID → 选品记录自动确定账号 → 该账号百应「带货内容」；
 * 只下载同款带货作品的完整视频，账号额度单独保存。
 */

interface CachedProduct {
  product_id: string
  account_id?: number | null
  commodity_id?: string | null
  title?: string | null
  cover_url?: string | null
  cos_ratio?: number | null
  video_url?: string | null
  video_urls?: string[] | null
  captured_at: string
}

// ---------------------------------------------------------------- 搜索状态

const productId = ref('')
const searchBusy = ref(false)
const error = ref('')
const searchStatus = ref('')
const foundProduct = ref<CachedProduct | null>(null)
interface PromotionVideo {
  videoId: string
  url: string
  title: string
  author: string
  duration?: number
  publishTime?: number
  shareUrl?: string
}
const promotionVideos = ref<PromotionVideo[]>([])
const promotionAvailable = ref(0)
const queryAccountId = ref<number | null>(null)
const queryAccountName = ref('')
const queryAccountSource = ref('')
const queryAccountDownloadPath = ref('')
const queryQuota = ref(10)
const quotaNotice = ref('')
const foundVideoUrls = computed(() => promotionVideos.value.map((video) => video.url))

watch(queryAccountId, async (accountId) => {
  if (accountId === null) return
  const result = await window.api.baiying.promotionQuota({ accountId })
  if (result.ok && result.quota && queryAccountId.value === accountId) queryQuota.value = result.quota
})

async function saveQueryQuota(): Promise<void> {
  const accountId = queryAccountId.value
  if (accountId === null) {
    quotaNotice.value = '请先查询商品，让软件自动确定选品账号'
    return
  }
  const limit = Number(queryQuota.value)
  if (!Number.isSafeInteger(limit) || limit < 10) {
    quotaNotice.value = '每账号下载额度必须是至少 10 条的整数'
    return
  }
  const result = await window.api.baiying.promotionQuota({ accountId, limit })
  quotaNotice.value = result.ok ? `已为该账号保存每商品 ${result.quota} 条的下载额度` : result.error ?? '保存失败'
}

function videoUrlsOf(product: CachedProduct): string[] {
  const stored = Array.isArray(product.video_urls) ? product.video_urls : []
  const urls = [...stored, ...(product.video_url ? [product.video_url] : [])]
  return [...new Set(urls.filter((url): url is string => typeof url === 'string' && /^https?:\/\//i.test(url)))]
}

function localDate(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

function fileTimestamp(): string {
  const now = new Date()
  return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}`
}

function videoIdFromUrl(url: string, index: number): string {
  try {
    const segments = new URL(url).pathname.split('/').filter(Boolean)
    const candidate = [...segments].reverse().find((part) => part.length >= 10 && /^[\w-]+$/.test(part))
    return candidate ?? `v${index + 1}`
  } catch {
    return `v${index + 1}`
  }
}

function baiyingAccountFolder(product: CachedProduct): string {
  return product.account_id && Number.isInteger(product.account_id) && product.account_id > 0
    ? `账号-${product.account_id}`
    : '账号-未知'
}

// 下载目录
const downloadDir = ref('')
const dirNotice = ref('')

// ---------------------------------------------------------------- 本地视频自动剪辑

const editSourcePath = ref('')
const editProductId = ref('')
const editProductTitle = ref('')
const editPrice = ref('')
const editSellingPoint = ref('')
const editTrimStart = ref(0)
const editTrimEnd = ref<number | string>('')
const editBusy = ref(false)
const editProgress = ref(0)
const editStatus = ref('')
const editError = ref('')
const editOutputPath = ref('')
const editSubtitlePath = ref('')
const editEngineId = ref('')
let offVideoEdit: (() => void) | null = null

// 预览
const previewText = ref('')

// ---------------------------------------------------------------- 引擎调用

/** 查百应缓存商品（baiying_cache.py get --product-id） */
function queryCachedProduct(id: string): Promise<{ found: boolean; product?: CachedProduct }> {
  return new Promise((resolve, reject) => {
    let settled = false
    let taskId: string | null = null
    let off: () => void = () => undefined
    const earlyEvents: EngineEventPayload[] = []
    let timeout: number | undefined
    const settle = (fn: () => void): void => {
      if (settled) return
      settled = true
      if (timeout) clearTimeout(timeout)
      off()
      fn()
    }

    // 先订阅再启动：引擎脚本秒回，先 run 再订阅会丢 result 事件（竞态）
    const handle = (e: EngineEventPayload): void => {
      if (e.taskId !== taskId) return
      if (e.type === 'result') {
        const d = e.data as { found?: boolean; product?: CachedProduct | null }
        settle(() => resolve({ found: Boolean(d?.found), product: d?.product ?? undefined }))
      } else if (e.type === 'error') {
        settle(() => reject(new Error(e.message ?? '引擎返回错误')))
      }
    }
    off = window.api.engine.onEvent((e) => {
      if (!taskId) earlyEvents.push(e)
      else handle(e)
    })

    timeout = window.setTimeout(() => settle(() => reject(new Error('引擎查询超时（30 秒）'))), 30_000)

    void window.api.engine
      .run({ script: 'baiying_cache.py', args: ['get', '--product-id', id] })
      .then((reply) => {
        if (!reply.taskId) {
          settle(() => reject(new Error(reply.error ?? '引擎任务启动失败')))
          return
        }
        taskId = reply.taskId
        earlyEvents.forEach(handle)
      })
      .catch((cause: unknown) => settle(() => reject(cause instanceof Error ? cause : new Error(String(cause)))))
  })
}

async function search(): Promise<void> {
  if (searchBusy.value) return
  const id = productId.value.trim()
  if (!/^\d{8,20}$/.test(id)) {
    error.value = '请输入 8–20 位数字商品 ID'
    return
  }
  searchBusy.value = true
  error.value = ''
  searchStatus.value = '正在查找同款带货作品…'
  foundProduct.value = null
  promotionVideos.value = []
  promotionAvailable.value = 0
  queryAccountId.value = null
  queryAccountName.value = ''
  queryAccountSource.value = ''
  queryAccountDownloadPath.value = ''
  quotaNotice.value = ''
  previewText.value = ''
  try {
    const r = await queryCachedProduct(id)
    const product: CachedProduct = r.product ?? { product_id: id, captured_at: new Date().toISOString() }
    foundProduct.value = product
    const result = await window.api.baiying.promotionVideos({ productId: id })
    if (!result.ok) throw new Error(result.error ?? '读取同款带货作品失败')
    if (!Number.isSafeInteger(result.accountId) || !result.accountId || result.accountId < 1) {
      throw new Error('未能确认该商品的选品账号，已停止下载')
    }
    queryAccountId.value = result.accountId
    queryAccountName.value = result.accountName || `账号-${result.accountId}`
    queryAccountSource.value = result.accountSource || '选品记录'
    queryAccountDownloadPath.value = result.downloadPath || ''
    if (Number.isSafeInteger(result.limit) && result.limit && result.limit >= 10) queryQuota.value = result.limit
    foundProduct.value = { ...product, account_id: result.accountId, commodity_id: result.commodityId ?? product.commodity_id }
    promotionVideos.value = result.videos ?? []
    promotionAvailable.value = result.available ?? promotionVideos.value.length
    if (!promotionVideos.value.length) throw new Error('百应带货内容没有可下载的同款视频')
    const started = await downloadFound()
    if (!started) throw new Error(queueSummary.value || '下载队列未启动')
    searchStatus.value = `已按${queryAccountSource.value}匹配「${queryAccountName.value}」。百应找到 ${promotionAvailable.value} 条同款带货作品，按该账号额度选取 ${promotionVideos.value.length} 条，已启动下载队列。${result.cacheWarning ? `推广 ID 缓存待检查：${result.cacheWarning}` : ''}`
  } catch (err) {
    error.value = `商品视频搜索失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    searchBusy.value = false
  }
}

async function pasteIdAndDownload(): Promise<void> {
  if (searchBusy.value || queueRunning.value) return
  const copied = await window.api.baiying.clipboardProductId()
  if (!copied.ok || !copied.productId) {
    error.value = copied.error ?? '未读取到商品 ID'
    return
  }
  productId.value = copied.productId
  await search()
}

// ---------------------------------------------------------------- 预览 / 下载目录

function videoPreview(): void {
  const p = foundProduct.value
  if (!p) {
    previewText.value = '（未选择商品）请先搜索'
    return
  }
  if (!promotionVideos.value.length) {
    previewText.value = '百应未返回该商品的同款带货作品视频。'
    return
  }
  previewText.value = `【${p.title ?? p.product_id}】\n商品ID：${p.product_id}\n百应同款带货作品：${promotionVideos.value.length} 条\n${promotionVideos.value.map((video, index) => `${index + 1}. ${video.author}｜${video.title}｜作品ID ${video.videoId}`).join('\n')}`
}

function clearPreview(): void {
  previewText.value = ''
}

async function chooseDir(): Promise<void> {
  const picked = await window.api.dir.chooseDownloadDir()
  if (picked) {
    downloadDir.value = picked
    dirNotice.value = `下载目录已设置为：${picked}`
  }
}

async function chooseVideoToEdit(): Promise<void> {
  const picked = await window.api.dir.choosePublishVideoFile()
  if (picked) {
    editSourcePath.value = picked
    editError.value = ''
    editOutputPath.value = ''
    editSubtitlePath.value = ''
    editStatus.value = '已选择视频，可设置首尾裁剪时间并生成成片。'
  }
}

function fillEditProductFromSearch(): void {
  const product = foundProduct.value
  if (!product) return
  editProductId.value = product.product_id
  editProductTitle.value = product.title ?? ''
  editStatus.value = '已从当前查询的百应商品填入 ID 和名称；价格、卖点请按实际商品补充。'
}

function handleVideoEditEvent(event: { taskId?: string; type?: string; stage?: string; percent?: number; message?: string; data?: unknown; output?: string; subtitle?: string; warning?: string }): void {
  if (event.type === 'stage') {
    editStatus.value = event.stage ?? '正在处理…'
    editProgress.value = event.percent ?? editProgress.value
    return
  }
  if (event.type === 'log') {
    editStatus.value = event.message ?? editStatus.value
    return
  }
  if (event.type === 'error') {
    editError.value = event.message ?? '视频剪辑失败'
    editStatus.value = '处理失败'
    editBusy.value = false
    editEngineId.value = ''
    offVideoEdit?.()
    offVideoEdit = null
    return
  }
  if (event.type === 'result') {
    const data = (event.data && typeof event.data === 'object' ? event.data : event) as {
      output?: string
      subtitle?: string
      warning?: string
      subtitleCount?: number
    }
    editOutputPath.value = data.output ?? ''
    editSubtitlePath.value = data.subtitle ?? ''
    editProgress.value = 100
    editStatus.value = data.warning || (data.subtitleCount === 0)
      ? (data.warning ?? '没有识别到语音字幕，已生成商品信息版视频。')
      : `完成：生成 ${data.subtitleCount ?? 0} 段同步字幕。`
    editBusy.value = false
    editEngineId.value = ''
    if (editOutputPath.value) notifyGlobal('视频自动剪辑完成，可打开成片位置。', 'success')
    offVideoEdit?.()
    offVideoEdit = null
  }
}

async function startVideoEdit(): Promise<void> {
  if (editBusy.value) return
  if (!editSourcePath.value) {
    editError.value = '请先选择一个本地 MP4 视频。'
    return
  }
  if (!editProductTitle.value.trim()) {
    editError.value = '请填写商品名称。'
    return
  }
  if (!downloadDir.value) downloadDir.value = await window.api.dir.getDownloadDir()
  editError.value = ''
  editOutputPath.value = ''
  editSubtitlePath.value = ''
  editProgress.value = 0
  editStatus.value = '正在启动本地语音识别与剪辑…首次使用会下载约 500 MB 的中文语音模型。'
  editBusy.value = true

  let taskId: string | null = null
  const pendingEvents: Parameters<typeof handleVideoEditEvent>[0][] = []
  offVideoEdit?.()
  offVideoEdit = window.api.engine.onEvent((event) => {
    if (!taskId) {
      pendingEvents.push(event)
      return
    }
    if (event.taskId !== taskId) return
    handleVideoEditEvent(event)
  })

  try {
    const reply = await window.api.engine.run({
      script: 'auto_video_editor.py',
      args: ['--input', editSourcePath.value, '--output-dir', downloadDir.value],
      stdinJson: {
        productId: editProductId.value.trim(),
        title: editProductTitle.value.trim(),
        price: editPrice.value.trim(),
        sellingPoint: editSellingPoint.value.trim(),
        trimStart: Number(editTrimStart.value) || 0,
        trimEnd: editTrimEnd.value === '' ? null : Number(editTrimEnd.value)
      }
    })
    if (!reply.taskId) {
      offVideoEdit?.()
      offVideoEdit = null
      editBusy.value = false
      editStatus.value = '启动失败'
      editError.value = reply.error ?? '无法启动本地剪辑引擎'
      return
    }
    taskId = reply.taskId
    editEngineId.value = taskId
    for (const event of pendingEvents) {
      if (event.taskId === taskId) handleVideoEditEvent(event)
    }
  } catch (error) {
    offVideoEdit?.()
    offVideoEdit = null
    editBusy.value = false
    editStatus.value = '启动失败'
    editError.value = String(error)
  }
}

async function cancelVideoEdit(): Promise<void> {
  if (!editEngineId.value) return
  editStatus.value = '正在取消…'
  await window.api.engine.cancel(editEngineId.value)
}

// ---------------------------------------------------------------- 下载队列（批量下载）

const DB_PATH = 'D:\\software\\投广客户端\\data\\ad_client.db'

interface QueueTaskItem {
  id: string
  url: string
  dest: string
  status: 'pending' | 'running' | 'retrying' | 'success' | 'failed' | 'skipped'
  percent: number
  attempts: number
  message: string
  /** 落盘路径（下载成功/跳过时由引擎回传） */
  file_path?: string
  /** 下载来源明确提供平台无水印视频；只有这类素材可自动进入发布队列 */
  sourceNoWatermark?: boolean
  /** 下载后素材处理状态，仅保存在本次页面会话 */
  watermarkStatus?: 'clean' | 'needs_review' | 'failed'
}

/** 去水印半自动标注用的一帧（由 watermark:getFrames 返回） */
interface WmFrame {
  t: number
  frame: number
  w: number
  h: number
  /** 本地 JPEG 绝对路径（渲染进程经 frame:// 协议加载） */
  path: string
}

const workers = ref(2)
const maxRetries = ref(3)
const queueTasks = ref<QueueTaskItem[]>([])
const queueEngineId = ref('')
const queueRunning = ref(false)
const queueSummary = ref('')
const queueHistory = ref<QueueTaskItem[]>([])
const finalizedDownloadIds = new Set<string>()

const STATUS_TEXT: Record<string, string> = {
  pending: '等待',
  running: '进行中',
  retrying: '重试中',
  success: '成功',
  failed: '失败',
  skipped: '跳过'
}

function statusText(s: string): string {
  return STATUS_TEXT[s] ?? s
}

/** 取消全部：杀队列进程树，引擎侧把未完成任务标失败 */
async function cancelAll(): Promise<void> {
  if (!queueRunning.value) return
  await window.api.engine.cancel(queueEngineId.value)
  queueTasks.value = queueTasks.value.map((t) =>
    t.status === 'running' || t.status === 'retrying' || t.status === 'pending'
      ? { ...t, status: 'failed' as const, message: '任务已被取消' }
      : t
  )
  queueRunning.value = false
  queueSummary.value = '已请求取消全部'
}

/** 打开下载任务所在文件夹（需落盘路径） */
function openQueueFolder(filePath: string): void {
  void window.api.assets.openFolder(filePath)
}

// ---------------------------------------------------------------- 去水印（框选模板交互）

const wmShow = ref(false)
const wmTask = ref<QueueTaskItem | null>(null)
const wmImgEl = ref<HTMLImageElement | null>(null)
const wmRunning = ref(false)
const wmAutoRunning = ref(false)
const wmProgress = ref(0)
const wmResult = ref('')
const wmOutputPath = ref('')
const wmError = ref('')
const wmFrames = ref<WmFrame[]>([])
const wmMarks = ref<Array<{ x: number; y: number } | null>>([])
const wmIdx = ref(0)
const wmCur = computed(() => wmFrames.value[wmIdx.value])
const wmMarkedCount = computed(() => wmMarks.value.filter((m) => m !== null).length)
/** 当前帧的 frame:// 加载 URL */
const wmFrameUrl = computed(() => {
  const cur = wmCur.value
  return cur ? `frame:///${encodeURI(cur.path.replace(/\\/g, '/'))}` : ''
})

async function openWatermark(task: QueueTaskItem): Promise<void> {
  if (!task.file_path) return
  wmTask.value = task
  wmShow.value = true
  wmResult.value = ''
  wmOutputPath.value = ''
  wmError.value = ''
  wmProgress.value = 0
  wmRunning.value = false
  wmIdx.value = 0
  const r = await window.api.watermark.getFrames(task.file_path, 5)
  if (!r.ok || !r.frames || r.frames.length === 0) {
    wmError.value = r.error ?? '抽帧失败'
    return
  }
  wmFrames.value = r.frames
  wmMarks.value = r.frames.map(() => null)
}

function wmClose(): void {
  if (wmRunning.value) return
  wmShow.value = false
  wmTask.value = null
  wmFrames.value = []
}

/** 点击帧图标注水印位置（记录相对图片显示区的坐标；再点同一红点可取消） */
function wmClick(e: MouseEvent): void {
  const img = e.currentTarget as HTMLImageElement
  if (!img || wmRunning.value) return
  const rect = img.getBoundingClientRect()
  const px = e.clientX - rect.left
  const py = e.clientY - rect.top
  const prev = wmMarks.value[wmIdx.value]
  if (prev && Math.abs(prev.x - px) < 10 && Math.abs(prev.y - py) < 10) {
    wmMarks.value[wmIdx.value] = null
  } else {
    wmMarks.value[wmIdx.value] = { x: px, y: py }
  }
}

function wmPrev(): void {
  if (wmIdx.value <= 0) return
  wmIdx.value -= 1
}

function wmNext(): void {
  if (wmIdx.value >= wmFrames.value.length - 1) return
  wmIdx.value += 1
}

async function confirmWatermark(): Promise<void> {
  const img = wmImgEl.value
  const points: string[] = []
  wmMarks.value.forEach((m, i) => {
    if (!m) return
    const f = wmFrames.value[i]
    // 图片显示坐标 → 原图像素坐标
    let x = m.x
    let y = m.y
    if (img && img.clientWidth > 0 && f.w > 0) {
      x = Math.round((m.x / img.clientWidth) * f.w)
      y = Math.round((m.y / img.clientHeight) * f.h)
    }
    points.push(`${f.frame},${Math.max(0, x)},${Math.max(0, y)}`)
  })
  if (points.length < 2) {
    wmError.value = '至少标注 2 帧的水印位置'
    return
  }
  await runRemoveWatermark(points.join(';'))
}

/** 引擎自动找水印：检测每帧水印位置并填充红点，用户可微调（点错重标/取消） */
async function autoDetectWatermark(): Promise<void> {
  const frames = wmFrames.value
  if (!frames.length || wmRunning.value || wmAutoRunning.value) return
  wmAutoRunning.value = true
  wmError.value = ''
  const frameList = frames.map((f) => f.frame).join(',')
  const ok = await runDetect(frameList)
  wmAutoRunning.value = false
  if (ok) wmError.value = ''
}

function runDetect(frameList: string): Promise<boolean> {
  return new Promise((resolve) => {
    let taskId: string | null = null
    let off: () => void = () => undefined
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'result') {
        const d = e.data as { points?: Array<[number, number, number] | null> }
        if (d?.points) {
          const img = wmImgEl.value
          wmFrames.value.forEach((f, i) => {
            const p = d.points?.[i]
            if (!p) return
            if (img && f.w > 0 && img.clientWidth > 0) {
              wmMarks.value[i] = {
                x: Math.round((p[1] * img.clientWidth) / f.w),
                y: Math.round((p[2] * img.clientHeight) / f.h)
              }
            } else {
              wmMarks.value[i] = { x: p[1], y: p[2] }
            }
          })
          off()
          resolve(true)
        } else {
          off()
          resolve(false)
        }
      } else if (e.type === 'error') {
        wmError.value = e.message ?? '自动检测水印失败'
        off()
        resolve(false)
      }
    })
    void window.api.engine
      .run({
        script: 'remove_watermark.py',
        args: ['--detect', '--input', wmTask.value!.file_path!, '--frames', frameList]
      })
      .then((reply) => {
        if (!reply.taskId) {
          wmError.value = reply.error ?? '自动检测任务启动失败'
          off()
          resolve(false)
        } else {
          taskId = reply.taskId
        }
      })
  })
}

function runRemoveWatermark(pointsStr: string): Promise<void> {
  return new Promise((resolve, reject) => {
    let taskId: string | null = null
    let off: () => void = () => undefined
    wmRunning.value = true
    wmProgress.value = 0
    wmResult.value = ''
    wmError.value = ''
    const settle = (fn: () => void): void => {
      if (!wmRunning.value) return
      wmRunning.value = false
      off()
      fn()
    }
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'stage') {
        wmProgress.value = e.percent ?? 0
      } else if (e.type === 'result') {
        const d = e.data as { output?: string }
        settle(() => {
          if (d?.output) {
            wmOutputPath.value = d.output
            wmResult.value = '去水印完成，已生成新文件'
          } else {
            wmResult.value = '去水印完成'
          }
          resolve()
        })
      } else if (e.type === 'error') {
        settle(() => {
          wmError.value = e.message ?? '去水印失败'
          reject(new Error(e.message ?? '去水印失败'))
        })
      }
    })
    void window.api.engine
      .run({
        script: 'remove_watermark.py',
        args: ['--input', wmTask.value!.file_path!, '--points', pointsStr]
      })
      .then((reply) => {
        if (!reply.taskId) {
          settle(() => {
            wmError.value = reply.error ?? '去水印任务启动失败'
            reject(new Error(reply.error ?? '启动失败'))
          })
        } else {
          taskId = reply.taskId
        }
      })
  })
}

/** 引擎命令的单次结果包装；订阅先于启动，避免快速结果事件丢失。 */
function runEngineResult(
  script: string,
  args: string[] = [],
  stdinJson?: unknown,
  timeoutMs = 90 * 60_000
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let taskId: string | null = null
    let settled = false
    let timer = 0
    let off: () => void = () => undefined
    const pending: EngineEventPayload[] = []
    const finish = (callback: () => void): void => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      off()
      callback()
    }
    const handle = (event: EngineEventPayload): void => {
      if (event.type === 'result') {
        finish(() => resolve((event.data ?? {}) as Record<string, unknown>))
      } else if (event.type === 'error') {
        finish(() => reject(new Error(event.message ?? '自动处理失败')))
      }
    }
    off = window.api.engine.onEvent((event) => {
      if (!taskId) pending.push(event)
      else if (event.taskId === taskId) handle(event)
    })
    timer = window.setTimeout(() => {
      if (taskId) void window.api.engine.cancel(taskId)
      finish(() => reject(new Error('任务超时，已请求停止')))
    }, timeoutMs)
    void window.api.engine.run({ script, args, stdinJson }).then((reply) => {
      if (!reply.taskId) {
        finish(() => reject(new Error(reply.error ?? '本地任务启动失败')))
        return
      }
      taskId = reply.taskId
      for (const event of pending.splice(0)) {
        if (event.taskId === taskId) handle(event)
      }
    }).catch((error: unknown) => {
      finish(() => reject(error instanceof Error ? error : new Error(String(error))))
    })
  })
}

/** 只有明确来自平台无水印源的文件才自动送入待确认发布队列。 */
async function handleDownloadedAsset(task: QueueTaskItem): Promise<void> {
  if (!task.file_path || finalizedDownloadIds.has(task.id)) return
  finalizedDownloadIds.add(task.id)
  task.percent = 100

  if (task.sourceNoWatermark !== true) {
    task.watermarkStatus = 'needs_review'
    task.message = '来源未确认无平台水印；原视频已保留，未加入发布队列'
    return
  }

  task.watermarkStatus = 'clean'
  task.message = '已确认使用平台无水印源，保留原视频并关联待确认发布队列…'
  try {
    const queued = await runEngineResult('publish_queue.py', [
      '--db', DB_PATH, 'add-downloaded', '--product-id', task.id, '--video-path', task.file_path
    ])
    const entries = Array.isArray(queued.queued) ? queued.queued as Array<Record<string, unknown>> : []
    const accepted = entries.filter((entry) => entry.ok === true)
    const rejected = entries.filter((entry) => entry.ok !== true)
    if (queued.unmatched === true) {
      task.message = '已确认使用平台无水印源并保留原视频；未匹配到已加车商品，未加入发布队列'
    } else if (accepted.length > 0 && rejected.length === 0) {
      task.message = '已确认使用平台无水印源并保留原视频；已加入 ' + accepted.length + ' 个账号的待确认发布队列'
    } else if (accepted.length > 0) {
      task.message = '已保留平台无水印源；' + accepted.length + ' 个账号已入队，' + rejected.length + ' 个账号入队失败'
    } else {
      const reasons = rejected.map((entry) => String(entry.error ?? '')).filter(Boolean).join('；')
      task.message = '已保留平台无水印源；发布队列未接收：' + (reasons || '未返回匹配结果')
    }
  } catch (error) {
    task.message = '已保留平台无水印源；加入发布队列失败：' + String(error)
  }
}

function isVerifiedToutiaoNoWatermarkUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl)
    return url.hostname.toLowerCase().endsWith('toutiaovod.com') &&
      url.searchParams.get('lr')?.toLowerCase() === 'unwatermarked'
  } catch {
    return false
  }
}

/** 订阅队列引擎事件流（组件卸载时退订） */
let offQueue: (() => void) | undefined
function bindQueueEvents(): void {
  offQueue = window.api.engine.onEvent((e) => {
    if (e.taskId !== queueEngineId.value) return
    if (e.type === 'task' && e.tid) {
      const t = queueTasks.value.find((q) => q.id === e.tid)
      if (t) {
        t.status = (e.status as QueueTaskItem['status']) ?? t.status
        t.percent = e.percent ?? t.percent
        t.attempts = e.attempts ?? t.attempts
        t.message = e.message ?? ''
        t.file_path = e.file_path ?? t.file_path
        if ((t.status === 'success' || t.status === 'skipped') && t.file_path) {
          void handleDownloadedAsset(t)
        }
      }
    } else if (e.type === 'result') {
      const d = e.data as
        | { total?: number; success?: number; failed?: number; skipped?: number; canceled?: boolean }
        | undefined
      queueRunning.value = false
      queueSummary.value = d
        ? `队列结束：共 ${d.total ?? 0}，成功 ${d.success ?? 0} / 失败 ${d.failed ?? 0} / 跳过 ${d.skipped ?? 0}${d.canceled ? '（已取消）' : ''}`
        : '队列结束'
      void loadQueueHistory()
    } else if (e.type === 'error') {
      queueRunning.value = false
      const msg = e.message ?? ''
      queueSummary.value = msg.includes('取消') ? '队列已取消' : `队列出错：${msg}`
    }
  })
}

/** 用 db.py 的只读 sql 逃生舱查队列历史 */
function queryDb(query: string): Promise<{ rows?: unknown[] } | null> {
  return new Promise((resolve, reject) => {
    let settled = false
    let taskId: string | null = null
    let off: () => void = () => undefined
    const settle = (fn: () => void): void => {
      if (settled) return
      settled = true
      off()
      fn()
    }
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'result') settle(() => resolve(e.data as { rows?: unknown[] }))
      else if (e.type === 'error') settle(() => reject(new Error(e.message ?? '查询失败')))
    })
    window.setTimeout(() => settle(() => reject(new Error('查询超时'))), 10_000)
    void window.api.engine
      .run({ script: 'db.py', args: ['--db', DB_PATH, 'sql', '--query', query] })
      .then((reply) => {
        if (!reply.taskId) settle(() => reject(new Error(reply.error ?? '查询启动失败')))
        else taskId = reply.taskId
      })
  })
}

/** 加载最近的任务记录（重启后可恢复可见） */
async function loadQueueHistory(): Promise<void> {
  try {
    const data = await queryDb(
      "SELECT id, status, attempts, progress, message FROM tasks WHERE task_type='download' ORDER BY id DESC LIMIT 8"
    )
    if (data?.rows) queueHistory.value = data.rows as QueueTaskItem[]
  } catch (err) {
    queueSummary.value = `历史加载失败：${String(err)}`
  }
}

// ---------------------------------------------------------------- 百应批量抓取（T-15）

interface BatchItem {
  productId: string
  status: 'loading' | 'success' | 'failed'
  message: string
}

interface BatchCaptureAccount {
  id: number
  name: string
  profilePath?: string
}

const batchIdsText = ref('')
const batchInterval = ref(8) // 秒
const batchAccounts = ref<BatchCaptureAccount[]>([])
const batchAccountId = ref<number | null>(null)
const batchAccountsBusy = ref(false)
const batchRunning = ref(false)
const batchTaskId = ref('')
const batchSummary = ref('')
const batchItems = ref<BatchItem[]>([])

const BATCH_STATUS_TEXT: Record<BatchItem['status'], string> = {
  loading: '抓取中',
  success: '成功',
  failed: '失败'
}

function upsertBatchItem(productId: string, patch: Partial<BatchItem>): void {
  const list = [...batchItems.value]
  const idx = list.findIndex((it) => it.productId === productId)
  if (idx >= 0) {
    list[idx] = { ...list[idx], ...patch }
  } else {
    list.push({ productId, status: 'loading', message: '', ...patch })
  }
  batchItems.value = list
}

async function loadBaiyingBatchAccounts(): Promise<void> {
  batchAccountsBusy.value = true
  try {
    const result = await window.api.baiying.batchAccounts()
    if (result.ok !== true) throw new Error(result.error ?? '读取百应采集账号失败')
    batchAccounts.value = result.accounts ?? []
    if (!batchAccounts.value.some((account) => account.id === batchAccountId.value)) batchAccountId.value = null
  } catch (cause) {
    batchAccounts.value = []
    batchAccountId.value = null
    batchSummary.value = `读取百应采集账号失败：${cause instanceof Error ? cause.message : String(cause)}`
  } finally {
    batchAccountsBusy.value = false
  }
}

async function openBatchAccountLogin(): Promise<void> {
  const selected = batchAccounts.value.find((account) => account.id === batchAccountId.value)
  if (!selected) {
    batchSummary.value = '请先选择百应采集账号'
    return
  }
  try {
    const result = await window.api.account.openJingxuan(selected.id, selected.profilePath)
    batchSummary.value = result.ok
      ? `已打开账号“${selected.name}”专属百应浏览器；请在这个窗口登录后再开始抓取。`
      : `打开账号“${selected.name}”百应失败：${result.error ?? '未知错误'}`
  } catch (cause) {
    batchSummary.value = `打开账号“${selected.name}”百应失败：${cause instanceof Error ? cause.message : String(cause)}`
  }
}

/** 订阅批量抓取事件流（组件卸载时退订） */
let offBatch: (() => void) | undefined
function bindBatchEvents(): void {
  offBatch = window.api.baiying.onBatchEvent((e) => {
    if (e.taskId !== batchTaskId.value) return
    if (e.type === 'stage') {
      batchSummary.value = e.message ?? ''
      upsertBatchItem(e.productId ?? '', { status: 'loading', message: '打开详情页…' })
    } else if (e.type === 'product') {
      upsertBatchItem(e.productId ?? '', {
        status: e.status ?? 'failed',
        message: e.message ?? ''
      })
    } else if (e.type === 'result') {
      batchRunning.value = false
      batchSummary.value = `批量抓取结束：成功 ${e.success ?? 0} / 失败 ${e.failed ?? 0}${e.canceled ? '（已取消）' : ''}`
    } else if (e.type === 'error') {
      batchRunning.value = false
      batchSummary.value = e.message ?? '批量抓取出错'
    }
  })
}

async function startBatch(): Promise<void> {
  const ids = batchIdsText.value
    .split(/[\s,，、;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (ids.length === 0) {
    batchSummary.value = '请先粘贴商品 ID 列表'
    return
  }
  if (batchAccountId.value === null) {
    batchSummary.value = '请先选择本次百应采集使用的账号'
    return
  }
  const reply = await window.api.baiying.batchStart({
    productIds: ids,
    accountId: batchAccountId.value,
    intervalMs: Math.max(2, Math.round(batchInterval.value)) * 1000
  })
  if (!reply.taskId) {
    batchSummary.value = `批量抓取启动失败：${reply.error ?? '未知错误'}`
    return
  }
  batchTaskId.value = reply.taskId
  batchRunning.value = true
  batchItems.value = []
  const accountName = batchAccounts.value.find((account) => account.id === batchAccountId.value)?.name ?? `账号-${batchAccountId.value}`
  batchSummary.value = `已启动账号“${accountName}”的采集：${ids.length} 个商品，间隔 ${batchInterval.value}s`
}

async function stopBatch(): Promise<void> {
  if (!batchRunning.value) return
  await window.api.baiying.batchCancel(batchTaskId.value)
  batchSummary.value = '已请求停止（当前商品完成后结束）'
}

/** 导入探针捕获的头条/西瓜视频直链（在客户端内置浏览器打开头条视频页后自动捕获） */
async function importToutiaoVod(): Promise<void> {
  dySummary.value = ''
  try {
    const links = await new Promise<string[]>((resolve, reject) => {
      let settled = false
      let taskId: string | null = null
      let off: () => void = () => undefined
      const settle = (fn: () => void): void => {
        if (settled) return
        settled = true
        off()
        fn()
      }
      off = window.api.engine.onEvent((e) => {
        if (taskId && e.taskId !== taskId) return
        if (e.type === 'result') {
          const d = e.data as { links?: string[] } | undefined
          settle(() => resolve(d?.links ?? []))
        } else if (e.type === 'error') {
          settle(() => reject(new Error(e.message ?? '导入失败')))
        }
      })
      window.setTimeout(() => settle(() => resolve([])), 10_000)
      void window.api.engine
        .run({ script: 'read_vod.py' })
        .then((reply) => {
          if (!reply.taskId) settle(() => resolve([]))
          else taskId = reply.taskId
        })
    })
    if (links.length === 0) {
      dySummary.value = '探针未捕获到头条直链：先在客户端「百应工作台」打开头条视频页播放视频'
      return
    }
    const existing = dyLinksText.value.trim() ? dyLinksText.value.trimEnd() + '\n' : ''
    dyLinksText.value = existing + links.join('\n')
    dySummary.value = `已导入 ${links.length} 条头条直链`
  } catch (err) {
    dySummary.value = String(err)
  }
}

// ---------------------------------------------------------------- 百应缓存商品（T-15：真实数据 + 下载）

const cachedProducts = ref<CachedProduct[]>([])
const cachedSelected = ref<Set<string>>(new Set())
const cachedBusy = ref(false)
const cachedNotice = ref('')

/** 拉取百应缓存商品列表（baiying_cache.py list） */
function loadCachedProducts(): Promise<CachedProduct[]> {
  return new Promise((resolve, reject) => {
    let settled = false
    let taskId: string | null = null
    let off: () => void = () => undefined
    const settle = (fn: () => void): void => {
      if (settled) return
      settled = true
      off()
      fn()
    }
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'result') {
        const d = e.data as { products?: CachedProduct[] } | undefined
        settle(() => resolve(d?.products ?? []))
      } else if (e.type === 'error') {
        settle(() => reject(new Error(e.message ?? '缓存列表查询失败')))
      }
    })
    window.setTimeout(() => settle(() => reject(new Error('缓存列表查询超时'))), 10_000)
    void window.api.engine
      .run({ script: 'baiying_cache.py', args: ['list', '--limit', '100'] })
      .then((reply) => {
        if (!reply.taskId) settle(() => reject(new Error(reply.error ?? '查询启动失败')))
        else taskId = reply.taskId
      })
  })
}

async function refreshCached(): Promise<void> {
  cachedBusy.value = true
  cachedNotice.value = ''
  try {
    cachedProducts.value = await loadCachedProducts()
    cachedNotice.value = cachedProducts.value.length > 0 ? '' : '缓存为空：先在百应打开商品详情或批量抓取'
  } catch (err) {
    cachedNotice.value = String(err)
  } finally {
    cachedBusy.value = false
  }
}

function cachedHasVideo(p: CachedProduct): boolean {
  return videoUrlsOf(p).length > 0
}

function toggleCached(pid: string): void {
  const next = new Set(cachedSelected.value)
  if (next.has(pid)) next.delete(pid)
  else next.add(pid)
  cachedSelected.value = next
}

function selectAllCached(): void {
  cachedSelected.value = new Set(cachedProducts.value.filter(cachedHasVideo).map((p) => p.product_id))
}

const selectedCachedRows = computed<CachedProduct[]>(() =>
  cachedProducts.value.filter((p) => cachedSelected.value.has(p.product_id))
)

/** 用真实 video_url 下载选中的缓存商品（走现有下载队列引擎） */
async function downloadCached(): Promise<void> {
  const rows = selectedCachedRows.value
  if (rows.length === 0 || queueRunning.value) return
  const date = localDate()
  const timestamp = fileTimestamp()
  const tasks = rows.flatMap((p) => videoUrlsOf(p).map((url, index) => ({
    id: `${p.product_id}-${index + 1}`,
    product_id: p.product_id,
    url,
    dest: `${downloadDir.value}\\${baiyingAccountFolder(p)}\\${date}\\${p.product_id}\\${p.product_id}_${videoIdFromUrl(url, index)}_${String(index + 1).padStart(3, '0')}_${timestamp}.mp4`
  })))
  queueTasks.value = tasks.map((t) => ({
    ...t,
    sourceNoWatermark: true,
    status: 'pending' as const,
    percent: 0,
    attempts: 0,
    message: '等待中'
  }))
  queueSummary.value = ''
  const reply = await window.api.engine.run({
    script: 'download_queue.py',
    args: ['--workers', String(workers.value), '--max-retries', String(maxRetries.value)],
    stdinJson: tasks
  })
  if (!reply.taskId) {
    queueSummary.value = `队列启动失败：${reply.error ?? '未知错误'}`
    return
  }
  queueEngineId.value = reply.taskId
  queueRunning.value = true
}

/** 下载当前搜索命中的商品视频（单任务，走同一队列） */
async function downloadFound(): Promise<boolean> {
  const p = foundProduct.value
  if (!p || !foundVideoUrls.value.length || queueRunning.value || queryAccountId.value === null) return false
  if (!downloadDir.value) downloadDir.value = await window.api.dir.getDownloadDir()
  const accountRoot = queryAccountDownloadPath.value || `${downloadDir.value}\\账号-${queryAccountId.value}`
  const date = localDate()
  const tasks = promotionVideos.value.map((video, index) => ({
    id: `${p.product_id}-${video.videoId}`,
    product_id: p.product_id,
    account_id: queryAccountId.value,
    verify_video: true,
    url: video.url,
    dest: `${accountRoot}\\${date}\\${p.product_id}\\${p.product_id}_${video.videoId}_${String(index + 1).padStart(3, '0')}_${video.publishTime ? fileTimestampFromSeconds(video.publishTime) : fileTimestamp()}.mp4`
  }))
  queueTasks.value = tasks.map((t) => ({
    ...t,
    sourceNoWatermark: false,
    status: 'pending' as const,
    percent: 0,
    attempts: 0,
    message: '等待中'
  }))
  queueSummary.value = ''
  const reply = await window.api.engine.run({
    script: 'download_queue.py',
    args: ['--workers', String(workers.value), '--max-retries', String(maxRetries.value)],
    stdinJson: tasks
  })
  if (!reply.taskId) {
    queueSummary.value = `队列启动失败：${reply.error ?? '未知错误'}`
    return false
  }
  queueEngineId.value = reply.taskId
  queueRunning.value = true
  return true
}

function fileTimestampFromSeconds(seconds: number): string {
  const date = new Date(seconds * 1000)
  if (!Number.isFinite(date.getTime())) return fileTimestamp()
  return `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}${String(date.getHours()).padStart(2, '0')}${String(date.getMinutes()).padStart(2, '0')}${String(date.getSeconds()).padStart(2, '0')}`
}

// ---------------------------------------------------------------- 抖音链接批量解析下载（补充路径）

interface DyBatchItem {
  tid: string
  input: string
  status: 'pending' | 'success' | 'failed'
  message: string
  url: string
}

interface DyParsedItem {
  tid: string
  input: string
  ok: boolean
  aweme_id?: string | null
  note_id?: string | null
  video_id?: string | null
  title?: string | null
  no_watermark_url?: string | null
  error?: string | null
}

const dyLinksText = ref('')
const dyItems = ref<DyBatchItem[]>([])
const dyRunning = ref(false)
const dyEngineId = ref('')
const dySummary = ref('')

watch(error, (message) => {
  if (message) notifyGlobal(message)
})
watch(dirNotice, (message) => {
  if (message) notifyGlobal(message)
})
watch(queueSummary, (message) => {
  if (message && !queueRunning.value) notifyGlobal(message)
})
watch(batchSummary, (message) => {
  if (message && !batchRunning.value) notifyGlobal(message)
})
watch(cachedNotice, (message) => {
  if (message && /失败|错误|超时|异常/.test(message)) notifyGlobal(message, 'error')
})
watch(dySummary, (message) => {
  if (message && !dyRunning.value) notifyGlobal(message)
})
watch(wmResult, (message) => {
  if (message) notifyGlobal(message, 'success')
})
watch(wmError, (message) => {
  if (message) notifyGlobal(message, 'error')
})

const DY_STATUS_TEXT: Record<DyBatchItem['status'], string> = {
  pending: '等待',
  success: '成功',
  failed: '失败'
}

let offDy: (() => void) | undefined
let offToutiao: (() => void) | undefined

/** 订阅头条批量解析事件（先订阅再启动，避免竞态） */
function bindToutiaoEvents(): void {
  offToutiao?.()
  offToutiao = window.api.toutiao.onBatchEvent((e) => {
    if (e.taskId !== dyEngineId.value) return
    if (e.type === 'stage') {
      dySummary.value = e.message ?? ''
    } else if (e.type === 'item' && e.index !== undefined) {
      const t = dyItems.value[e.index]
      if (t) {
        t.status = e.status === 'success' ? 'success' : 'failed'
        t.message = e.message ?? ''
        t.url = ''
      }
    } else if (e.type === 'result') {
      dyRunning.value = false
      dySummary.value = `头条解析完成：成功 ${e.success ?? 0} / 失败 ${e.failed ?? 0}${e.canceled ? '（已取消）' : ''}`
      const okItems = (e.items ?? []).filter((x) => x.ok && x.vod_url)
      if (okItems.length > 0) void downloadToutiaoItems(okItems)
    } else if (e.type === 'error') {
      dyRunning.value = false
      dySummary.value = e.message ?? '头条批量解析出错'
    }
  })
}

/** 把头条捕获的 vod 直链送入下载队列 */
async function downloadToutiaoItems(okItems: Array<{ input: string; ok: boolean; vod_url: string | null; error: string | null }>): Promise<void> {
  if (queueRunning.value) return
  const date = new Date().toISOString().slice(0, 10)
  const tasks = okItems.map((it, i) => ({
    id: `tt-${dyEngineId.value}-${i}`,
    url: it.vod_url as string,
    dest: `${downloadDir.value}\\抖音素材\\${date}\\tt_${new Date().getTime()}_${i}.mp4`
  }))
  queueTasks.value = tasks.map((t) => ({
    ...t,
    sourceNoWatermark: isVerifiedToutiaoNoWatermarkUrl(t.url),
    status: 'pending' as const,
    percent: 0,
    attempts: 0,
    message: '等待中'
  }))
  queueSummary.value = ''
  const reply = await window.api.engine.run({
    script: 'download_queue.py',
    args: ['--workers', String(workers.value), '--max-retries', String(maxRetries.value)],
    stdinJson: tasks
  })
  if (!reply.taskId) {
    queueSummary.value = `队列启动失败：${reply.error ?? '未知错误'}`
    return
  }
  queueEngineId.value = reply.taskId
  queueRunning.value = true
}

/** 订阅解析事件流（先订阅再启动，避免竞态） */
function bindDyEvents(): void {
  offDy?.()
  offDy = window.api.engine.onEvent((e) => {
    if (e.taskId !== dyEngineId.value) return
    if (e.type === 'task' && e.tid) {
      const t = dyItems.value.find((x) => x.tid === e.tid)
      if (t) {
        t.status = e.status === 'success' ? 'success' : 'failed'
        t.message = e.message ?? ''
      }
    } else if (e.type === 'result') {
      dyRunning.value = false
      const d = e.data as { total?: number; ok?: number; failed?: number; items?: DyParsedItem[] } | undefined
      const items = d?.items ?? []
      for (const it of items) {
        const t = dyItems.value.find((x) => x.tid === it.tid)
        if (t) {
          t.status = it.ok ? 'success' : 'failed'
          t.message = it.ok ? (it.title || '解析成功') : (it.error || '解析失败')
          t.url = it.no_watermark_url ?? ''
        }
      }
      dySummary.value = `解析完成：成功 ${d?.ok ?? 0} / 失败 ${d?.failed ?? 0}`
      const okItems = items.filter((x) => x.ok && x.no_watermark_url)
      if (okItems.length > 0) void downloadDyItems(okItems)
    } else if (e.type === 'error') {
      dyRunning.value = false
      dySummary.value = e.message ?? '解析出错'
    }
  })
}

async function startDyBatch(): Promise<void> {
  const links = dyLinksText.value
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (links.length === 0) {
    dySummary.value = '请粘贴抖音分享链接（每行一条）'
    return
  }
  // 头条页面链接：走内置浏览器批量解析（真浏览器过 JS 防护，探针捕获 vod 直链）
  if (links.some((l) => /toutiao\.com\/video\/|ixigua\.com/.test(l))) {
    dyItems.value = links.map((l, i) => ({
      tid: `tt-${i + 1}`,
      input: l,
      status: 'pending' as const,
      message: '等待打开页面…',
      url: ''
    }))
    dySummary.value = ''
    bindToutiaoEvents()
    const reply = await window.api.toutiao.batchStart({ links })
    if (!reply.taskId) {
      dySummary.value = `头条批量解析启动失败：${reply.error ?? '未知错误'}`
      return
    }
    dyEngineId.value = reply.taskId
    dyRunning.value = true
    return
  }
  dyItems.value = links.map((l, i) => ({
    tid: `dy-${i + 1}`,
    input: l,
    status: 'pending' as const,
    message: '等待解析',
    url: ''
  }))
  dySummary.value = ''
  bindDyEvents()
  // 小红书解析需登录态：导出小红书 cookie（未登录则为空，小红书条目会提示登录）
  const sess = await window.api.login.exportForEngine('xiaohongshu')
  const reply = await window.api.engine.run({
    script: 'link_batch.py',
    stdinJson: { links, cookie: sess?.cookieHeader ?? '', timeout: 15 }
  })
  if (!reply.taskId) {
    dySummary.value = `解析启动失败：${reply.error ?? '未知错误'}`
    return
  }
  dyEngineId.value = reply.taskId
  dyRunning.value = true
}

/** 把解析成功的无水印地址送入下载队列（落盘 抖音素材 目录） */
async function downloadDyItems(okItems: DyParsedItem[]): Promise<void> {
  if (queueRunning.value) return
  const date = new Date().toISOString().slice(0, 10)
  const tasks = okItems.map((it) => {
    const vid = it.video_id ?? it.note_id ?? it.aweme_id ?? it.tid
    return {
      id: vid,
      url: it.no_watermark_url as string,
      dest: `${downloadDir.value}\\抖音素材\\${date}\\${vid}.mp4`
    }
  })
  queueTasks.value = tasks.map((t) => ({
    ...t,
    sourceNoWatermark: true,
    status: 'pending' as const,
    percent: 0,
    attempts: 0,
    message: '等待中'
  }))
  queueSummary.value = ''
  const reply = await window.api.engine.run({
    script: 'download_queue.py',
    args: ['--workers', String(workers.value), '--max-retries', String(maxRetries.value)],
    stdinJson: tasks
  })
  if (!reply.taskId) {
    queueSummary.value = `队列启动失败：${reply.error ?? '未知错误'}`
    return
  }
  queueEngineId.value = reply.taskId
  queueRunning.value = true
}

// ---------------------------------------------------------------- 生命周期

onMounted(async () => {
  downloadDir.value = await window.api.dir.getDownloadDir()
  bindQueueEvents()
  bindBatchEvents()
  void loadQueueHistory()
  void refreshCached()
  void loadBaiyingBatchAccounts()
})

onBeforeUnmount(() => {
  offQueue?.()
  offBatch?.()
  offDy?.()
  offToutiao?.()
  offVideoEdit?.()
})
</script>

<template>
  <section class="query">
    <header class="query-head"><h1>商品查询与下载</h1><span>输入商品 ID，下载百应同款带货作品视频</span></header>
    <!-- ---------- 搜索区 ---------- -->
    <div class="search-bar">
      <label class="fld">
        <span class="fld-label">商品 ID</span>
        <input
          v-model="productId"
          class="input id-input"
          placeholder="输入商品 ID，如 3829105980558409845"
          @keyup.enter="search"
        />
      </label>

      <button class="btn primary" :disabled="searchBusy || queueRunning" @click="search">
        {{ searchBusy ? '查询中…' : '搜索并下载' }}
      </button>
      <button class="btn" :disabled="searchBusy || queueRunning" @click="pasteIdAndDownload">粘贴 ID 并下载</button>
      <p v-if="searchStatus" class="notice" role="status">{{ searchStatus }}</p>
      <div v-if="error" class="search-feedback">
        <span class="error">{{ error }}</span>
      </div>
    </div>

    <div class="search-bar">
      <label class="fld">
        <span class="fld-label">选品账号（自动匹配）</span>
        <span class="input">{{ queryAccountId === null ? '查询商品后自动识别' : `${queryAccountName}（ID ${queryAccountId}）` }}</span>
      </label>
      <label class="fld">
        <span class="fld-label">该账号每商品下载额度（至少 10 条）</span>
        <input v-model.number="queryQuota" class="input" type="number" min="10" step="1" :disabled="queryAccountId === null" />
      </label>
      <button class="btn" :disabled="queryAccountId === null" @click="saveQueryQuota">保存额度</button>
      <p v-if="quotaNotice" class="notice">{{ quotaNotice }}</p>
    </div>

    <QianchuanVideoQueryPanel />

    <!-- ---------- 真实数据标注 ---------- -->
    <p v-if="foundProduct" class="real-tag">商品信息来自百应缓存；视频来自选品账号百应「带货内容」</p>

    <!-- ---------- 商品信息 ---------- -->
    <div v-if="foundProduct" class="product-card">
      <div class="p-col p-name">
        <span class="p-label">商品名称</span><b>{{ foundProduct.title ?? '（未抓取详情）' }}</b>
      </div>
      <div class="p-col"><span class="p-label">商品 ID</span><span class="mono">{{ foundProduct.product_id }}</span></div>
      <div class="p-col p-rate"><span class="p-label">佣金率</span><b>{{ foundProduct.cos_ratio ?? '-' }}%</b></div>
      <div class="p-col"><span class="p-label">抓取时间</span>{{ foundProduct.captured_at.slice(0, 16) }}</div>
    </div>

    <!-- 常用操作保持在结果上方，普通窗口无需滚到表格底部。 -->
    <div class="action-bar">
      <button class="btn primary" :disabled="!foundVideoUrls.length || queueRunning" @click="downloadFound">
        {{ queueRunning ? '队列运行中…' : `重新下载本次视频（${foundVideoUrls.length}）` }}
      </button>
      <button class="btn danger" :disabled="!queueRunning" @click="cancelAll">取消全部</button>
      <button class="btn" @click="chooseDir">设置下载目录</button>
      <button class="btn" :disabled="!foundProduct" @click="videoPreview">视频预览</button>
      <button class="btn" @click="clearPreview">清空预览</button>
    </div>

    <!-- ---------- 本地视频自动剪辑 ---------- -->
    <section class="edit-panel">
      <div class="edit-heading">
        <div>
          <b>视频自动剪辑</b>
          <span>保留原片，可裁首尾；自动识别语音生成同步字幕并叠加商品信息</span>
        </div>
        <button class="btn" :disabled="!foundProduct" @click="fillEditProductFromSearch">从已查询商品填入</button>
      </div>

      <div class="edit-source-row">
        <button class="btn" :disabled="editBusy" @click="chooseVideoToEdit">选择本地 MP4</button>
        <span class="edit-path mono" :title="editSourcePath">{{ editSourcePath || '尚未选择视频' }}</span>
      </div>

      <div class="edit-fields">
        <label>
          <span>商品名称</span>
          <input v-model="editProductTitle" class="input" maxlength="80" placeholder="填写实际商品名称" :disabled="editBusy" />
        </label>
        <label>
          <span>商品 ID（可选）</span>
          <input v-model="editProductId" class="input" placeholder="商品 ID" :disabled="editBusy" />
        </label>
        <label>
          <span>价格（可选）</span>
          <input v-model="editPrice" class="input" maxlength="30" placeholder="如：19.9 元" :disabled="editBusy" />
        </label>
        <label>
          <span>卖点（可选）</span>
          <input v-model="editSellingPoint" class="input" maxlength="80" placeholder="只填写已核实的商品信息" :disabled="editBusy" />
        </label>
      </div>

      <div class="edit-options">
        <label>从第 <input v-model.number="editTrimStart" class="input edit-time-input" type="number" min="0" step="0.1" :disabled="editBusy" /> 秒开始</label>
        <label>到第 <input v-model="editTrimEnd" class="input edit-time-input" type="number" min="0" step="0.1" placeholder="视频末尾" :disabled="editBusy" /> 秒结束</label>
        <span class="edit-output-hint">输出：{{ downloadDir }}\视频自动剪辑（H.264 / AAC 128 kbps）</span>
      </div>

      <div class="edit-actions">
        <button class="btn primary" :disabled="editBusy || !editSourcePath || !editProductTitle.trim()" @click="startVideoEdit">
          {{ editBusy ? '正在识别并剪辑…' : '生成字幕商品视频' }}
        </button>
        <button class="btn danger" :disabled="!editBusy || !editEngineId" @click="cancelVideoEdit">取消处理</button>
        <button v-if="editOutputPath" class="btn" @click="openQueueFolder(editOutputPath)">打开成片位置</button>
        <span v-if="editStatus" class="edit-status">{{ editStatus }}</span>
      </div>
      <div v-if="editBusy" class="edit-progress"><span :style="{ width: `${editProgress}%` }"></span></div>
      <p v-if="editError" class="error edit-error">{{ editError }}</p>
      <p v-if="editSubtitlePath" class="edit-subtitle-path">字幕文件：<span class="mono">{{ editSubtitlePath }}</span></p>
      <p class="edit-model-note">首次运行需联网下载约 500 MB 的中文语音模型，模型缓存在当前下载目录；后续识别可离线使用。</p>
    </section>

    <!-- ---------- 视频结果（仅展示该商品的同款带货作品） ---------- -->
    <div class="table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th class="c-check">勾选</th>
            <th class="c-video">视频状态</th>
            <th class="c-url">带货作品</th>
          </tr>
        </thead>
        <tbody>
          <template v-if="foundProduct">
            <tr v-for="(video, videoIndex) in promotionVideos" :key="`${foundProduct.product_id}-${video.videoId}`" class="checked">
              <td class="c-check">
                <input type="checkbox" checked disabled />
              </td>
              <td class="c-video">
                <span class="q-video ok">
                  视频 {{ videoIndex + 1 }} / {{ foundVideoUrls.length }}
                </span>
              </td>
              <td class="c-url" :title="video.title">
                {{ video.author }}｜{{ video.title }}（作品 ID：{{ video.videoId }}）
              </td>
            </tr>
            <tr v-if="!foundVideoUrls.length"><td colspan="3">百应尚未返回该商品的同款带货作品视频。</td></tr>
          </template>
          <template v-else>
            <tr class="empty-row"><td colspan="3">搜索商品 ID 后展示真实商品视频直链</td></tr>
          </template>
        </tbody>
      </table>
    </div>

    <p v-if="dirNotice" class="notice">{{ dirNotice }}</p>
    <p class="dir-line">当前下载目录：<span class="mono">{{ downloadDir }}</span></p>
    <p v-if="queryAccountId !== null && foundProduct" class="dir-line">本商品自动分类目录：<span class="mono">{{ queryAccountDownloadPath || `${downloadDir}\\账号-${queryAccountId}` }}\{{ localDate() }}\{{ foundProduct.product_id }}</span></p>

    <!-- ---------- 百应批量抓取（T-15） ---------- -->
    <div class="queue-panel">
      <div class="queue-head">
        <b>百应批量抓取</b>
        <label class="cfg">
          采集账号
          <select v-model.number="batchAccountId" class="input" :disabled="batchRunning || batchAccountsBusy">
            <option :value="null">{{ batchAccountsBusy ? '正在读取账号…' : '请选择账号' }}</option>
            <option v-for="account in batchAccounts" :key="account.id" :value="account.id">{{ account.name }}（ID {{ account.id }}）</option>
          </select>
        </label>
        <button class="btn" :disabled="batchRunning || batchAccountId === null" @click="openBatchAccountLogin">打开该账号百应登录</button>
        <button class="btn" :disabled="batchRunning || batchAccountsBusy" @click="loadBaiyingBatchAccounts">刷新账号</button>
        <label class="cfg">
          间隔（秒）
          <input v-model.number="batchInterval" class="input cfg-input" type="number" min="2" max="60" />
        </label>
        <button class="btn primary" :disabled="batchRunning || batchAccountId === null" @click="startBatch">开始抓取</button>
        <button class="btn danger" :disabled="!batchRunning" @click="stopBatch">停止</button>
      </div>

      <textarea
        v-model="batchIdsText"
        class="batch-input"
        rows="3"
        placeholder="每行一个商品 ID（8~20 位数字），或 商品ID:commodityID。客户端将逐个打开百应详情页并缓存商品视频直链"
        :disabled="batchRunning"
      />

      <p v-if="batchSummary" class="notice">{{ batchSummary }}</p>

      <ul v-if="batchItems.length > 0" class="qlist">
        <li v-for="it in batchItems" :key="it.productId">
          <span class="q-status" :class="it.status">{{ BATCH_STATUS_TEXT[it.status] }}</span>
          <span class="q-id mono">{{ it.productId }}</span>
          <span class="q-msg">{{ it.message }}</span>
        </li>
      </ul>
      <p v-else class="q-empty">
        先选择采集账号，并用“打开该账号百应登录”进入它的专属 Chrome profile。批量抓取只使用选中账号的登录态；缓存记录会保存本地账号 ID，未能确认来源的旧记录显示为未知。
      </p>
    </div>

    <!-- ---------- 百应缓存商品（真实数据 + 下载） ---------- -->
    <div class="queue-panel">
      <div class="queue-head">
        <b>百应缓存商品</b>
        <button class="btn" :disabled="cachedBusy || queueRunning" @click="refreshCached">刷新</button>
        <button class="btn" :disabled="cachedProducts.length === 0" @click="selectAllCached">全选有视频</button>
        <button class="btn primary" :disabled="selectedCachedRows.length === 0 || queueRunning" @click="downloadCached">
          {{ queueRunning ? '队列运行中…' : `下载选中（${selectedCachedRows.length}）` }}
        </button>
        <span class="count">缓存 <b>{{ cachedProducts.length }}</b> 个</span>
      </div>

      <p v-if="cachedNotice" class="notice">{{ cachedNotice }}</p>

      <ul v-if="cachedProducts.length > 0" class="qlist">
        <li v-for="p in cachedProducts" :key="p.product_id" :class="{ 'checked': cachedSelected.has(p.product_id) }">
          <span class="q-check">
            <input
              type="checkbox"
              :checked="cachedSelected.has(p.product_id)"
              :disabled="!cachedHasVideo(p)"
              @change="toggleCached(p.product_id)"
            />
          </span>
          <span class="q-id mono" :title="p.title ?? ''">{{ p.product_id }}</span>
          <span class="q-title" :title="p.title ?? ''">{{ p.title ?? '（未抓取详情）' }}</span>
          <span class="q-source" :title="p.account_id ? `百应本地账号 ID ${p.account_id}` : '旧缓存或通用登录视图，无法确认账号来源'">{{ p.account_id ? `账号-${p.account_id}` : '账号-未知' }}</span>
          <span v-if="p.cos_ratio != null" class="q-rate">佣金 {{ p.cos_ratio }}%</span>
          <span class="q-video" :class="cachedHasVideo(p) ? 'ok' : 'bad'">
            {{ cachedHasVideo(p) ? `${videoUrlsOf(p).length} 条视频直链` : '无视频' }}
          </span>
          <span class="q-time">{{ p.captured_at.slice(0, 16) }}</span>
        </li>
      </ul>
      <p v-else class="q-empty">缓存为空：在百应打开商品详情或先用上方「批量抓取」收集商品</p>
    </div>

    <!-- ---------- 通用链接批量解析下载（二期 Step 10） ---------- -->
    <div class="queue-panel">
      <div class="queue-head">
        <b>通用链接批量解析下载</b>
        <button class="btn primary" :disabled="dyRunning" @click="startDyBatch">
          {{ dyRunning ? '解析中…' : '开始解析下载' }}
        </button>
        <button class="btn" :disabled="dyRunning" @click="importToutiaoVod">导入头条直链</button>
      </div>

      <textarea
        v-model="dyLinksText"
        class="batch-input"
        rows="3"
        placeholder="每行一条：抖音分享链接 / 视频页链接 / 口令文本 / 视频ID（如 https://v.douyin.com/xxxx/ 或 7693062724168249969）。解析成功后使用无水印视频源并进入下载队列"
        :disabled="dyRunning"
      />

      <p v-if="dySummary" class="notice">{{ dySummary }}</p>

      <ul v-if="dyItems.length > 0" class="qlist">
        <li v-for="t in dyItems" :key="t.tid">
          <span class="q-status" :class="t.status">{{ DY_STATUS_TEXT[t.status] }}</span>
          <span class="q-id mono" :title="t.input">{{ t.input }}</span>
          <span class="q-msg" :title="t.message">{{ t.message }}</span>
        </li>
      </ul>
      <p v-else class="q-empty">
        说明：支持抖音四类输入（分享短链/视频页链接/口令文本/视频ID），解析为无水印地址后自动批量下载到「抖音素材」目录；头条/小红书待 POC
      </p>
    </div>

    <!-- ---------- 下载任务队列（批量下载） ---------- -->
    <div class="queue-panel">
      <div class="queue-head">
        <b>下载任务队列</b>
        <label class="cfg">
          并发数
          <input v-model.number="workers" class="input cfg-input" type="number" min="1" max="8" />
        </label>
        <label class="cfg">
          最大重试
          <input v-model.number="maxRetries" class="input cfg-input" type="number" min="0" max="10" />
        </label>
      </div>

      <p v-if="queueSummary" class="notice">{{ queueSummary }}</p>

      <ul v-if="queueTasks.length > 0" class="qlist">
        <li v-for="t in queueTasks" :key="t.id" :class="t.status">
          <span class="q-status" :class="t.status">{{ statusText(t.status) }}</span>
          <span class="q-id mono" :title="t.url">{{ t.id }}</span>
          <span class="q-bar"><i :style="{ width: t.percent + '%' }" /></span>
          <span class="q-pct">{{ t.percent }}%</span>
          <span class="q-attempts">重试 {{ t.attempts }}</span>
          <span class="q-msg">{{ t.message }}</span>
          <template v-if="(t.status === 'success' || t.status === 'skipped') && t.file_path">
            <button class="btn tiny" title="打开所在文件夹" @click="openQueueFolder(t.file_path!)">📁</button>
            <button class="btn tiny" :disabled="wmRunning" title="手动校正移动水印位置" @click="openWatermark(t)">手动去水印</button>
            <span class="q-path mono" :title="t.file_path">{{ t.file_path }}</span>
          </template>
        </li>
      </ul>
      <p v-else class="q-empty">勾选视频后点「下载全部」开始批量下载</p>

      <div v-if="queueHistory.length > 0" class="q-history">
        <div class="q-history-title">最近任务记录（SQLite 持久化，重启后仍可查）</div>
        <ul>
          <li v-for="h in queueHistory" :key="h.id">
            <span class="q-status" :class="h.status">{{ statusText(h.status) }}</span>
            <span class="q-id mono" :title="String(h.message)">{{ h.message }}</span>
            <span class="q-h-meta">尝试 {{ h.attempts }} 次</span>
          </li>
        </ul>
      </div>
    </div>

    <!-- ---------- 预览区 ---------- -->
    <pre v-if="previewText" class="preview">{{ previewText }}</pre>

    <!-- ---------- 去水印：框选水印模板 ---------- -->
    <div v-if="wmShow" class="wm-mask" @click.self="wmClose">
      <div class="wm-modal">
        <h3>去水印 — {{ wmTask ? (wmTask.file_path ?? '').split('\\').pop() : '' }}</h3>
        <p class="wm-hint">第 {{ wmIdx + 1 }}/{{ wmFrames.length }} 帧（约 {{ wmCur?.t }}s）：<b>点击水印文字中心</b>标注位置，共需 ≥2 帧；再点同一红点可取消。</p>
        <div class="wm-frame-box">
          <img v-if="wmCur" ref="wmImgEl" :src="wmFrameUrl" class="wm-frame-img" @click="wmClick" alt="当前视频帧；可点击水印位置标注，也可使用下方自动找水印按钮" />
          <div
            v-if="wmCur && wmMarks[wmIdx]"
            class="wm-dot"
            :style="{ left: wmMarks[wmIdx]!.x + 'px', top: wmMarks[wmIdx]!.y + 'px' }"
          ></div>
        </div>
        <div class="wm-nav">
          <button class="btn" :disabled="wmIdx <= 0" @click="wmPrev">◀ 上一帧</button>
          <span class="wm-nav-info">已标注 {{ wmMarkedCount }}/{{ wmFrames.length }}</span>
          <button class="btn" :disabled="wmIdx >= wmFrames.length - 1" @click="wmNext">下一帧 ▶</button>
        </div>
        <div class="wm-auto">
          <button class="btn primary" :disabled="wmRunning || wmAutoRunning" @click="autoDetectWatermark">
            {{ wmAutoRunning ? '引擎检测中…' : '🔍 自动找水印' }}
          </button>
          <span class="wm-auto-hint">自动在每帧标出检测到的水印（可点错重标/取消），标完确认去水印</span>
        </div>
        <div v-if="wmRunning" class="wm-progress">逐帧处理中… {{ wmProgress }}%</div>
        <div v-if="wmError" class="wm-error">{{ wmError }}</div>
        <div v-if="wmResult" class="wm-result">
          {{ wmResult }}
          <template v-if="wmOutputPath">
            <button class="btn tiny" @click="openQueueFolder(wmOutputPath)">📁 打开</button>
            <span class="q-path mono" :title="wmOutputPath">{{ wmOutputPath }}</span>
          </template>
        </div>
        <div class="wm-ops">
          <button class="btn" :disabled="wmRunning" @click="wmClose">取消</button>
          <button class="btn primary" :disabled="wmRunning || wmMarkedCount < 2" @click="confirmWatermark">确认去水印</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.query {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
  /* 内容超高时自身滚动（子元素不压缩，保证溢出产生滚动条） */
  overflow: hidden auto;
  box-shadow: var(--shadow-card);
}

.query > * {
  flex-shrink: 0;
}
.query-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
.query-head h1 { margin: 0; color: var(--text); font-size: 18px; }
.query-head span { color: var(--text-muted); font-size: 11px; }

/* ---------- 搜索区 ---------- */
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  flex-shrink: 0;
}

.search-feedback {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.fld {
  display: flex;
  align-items: center;
  gap: 6px;
}

.fld-label {
  color: var(--text-muted);
  font-size: 13px;
}

.input {
  height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
}

.id-input {
  width: 240px;
}

.limit-input {
  width: 64px;
}

.chk {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: var(--text);
}

.btn {
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-status);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
}

.btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn.primary {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}

.btn.primary:hover:not(:disabled) {
  background: #2f6fd8;
}

/* 行内小按钮（下载任务行「打开文件夹」） */
.btn.tiny {
  height: 20px;
  padding: 0 6px;
  font-size: 11px;
  flex-shrink: 0;
}

.error {
  color: #dc2626;
  font-size: 13px;
}

/* ---------- 真实数据标注 ---------- */
.real-tag {
  margin: 0;
  padding: 6px 10px;
  background: #e7f8f0;
  border: 1px solid #b7e0cc;
  border-radius: var(--radius);
  color: #0f7a52;
  font-size: 12px;
  flex-shrink: 0;
}

/* ---------- 商品信息 ---------- */
.product-card {
  display: flex;
  gap: 24px;
  padding: 10px 12px;
  background: linear-gradient(105deg, #f7faff, #ffffff);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 13px;
  color: var(--text);
  flex-shrink: 0;
  flex-wrap: wrap;
}

.p-col {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.p-col.p-name {
  min-width: 240px;
}

.p-col.p-rate b {
  color: #d97706;
  font-size: 15px;
}

.p-label {
  color: var(--text-muted);
  font-size: 11px;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
  word-break: break-all;
}

/* ---------- 表格 ---------- */
.table-wrap {
  flex: 1;
  min-height: 120px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.grid {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  white-space: nowrap;
}

.grid thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-top);
  color: var(--text-on-dark);
  font-weight: 500;
  padding: 7px 8px;
  text-align: left;
  border-bottom: 1px solid var(--border-dark);
}

.grid tbody td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.grid tbody tr:hover {
  background: var(--bg-placeholder);
}

.grid tbody tr.checked {
  background: var(--brand-soft);
}

.c-check {
  width: 32px;
  text-align: center;
}

.c-seq {
  width: 44px;
  color: var(--text-muted);
}

.c-cover {
  width: 56px;
}

.cover {
  width: 44px;
  height: 44px;
  border-radius: 4px;
  border: 1px solid var(--border);
}

.c-title {
  min-width: 180px;
}

.c-author {
  min-width: 90px;
}

.c-date {
  width: 90px;
}

.c-money,
.c-ctr,
.c-deal {
  width: 92px;
}

.c-license {
  width: 80px;
}

.c-url {
  min-width: 200px;
  color: var(--text-muted);
}

.c-video {
  width: 100px;
}

.license {
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
}

.license.ok {
  background: #e7f8f0;
  color: #0f7a52;
}

.license.bad {
  background: #fdecea;
  color: #b73b2f;
}

.license.pending {
  background: #fff4dd;
  color: #b45309;
}

.empty-row td {
  text-align: center;
  color: var(--text-muted);
  padding: 24px 8px;
}

/* ---------- 底部操作条 ---------- */
.action-bar {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: rgba(255, 255, 255, .97);
  box-shadow: var(--shadow-card);
}

.spacer {
  flex: 1;
}

.count {
  font-size: 13px;
  color: var(--text-muted);
}

.count b {
  color: var(--brand);
}

.notice {
  margin: 0;
  padding: 6px 10px;
  background: #fff8e6;
  border: 1px solid #f0d9a0;
  border-radius: var(--radius);
  color: #7a5a10;
  font-size: 12px;
  flex-shrink: 0;
}

.dir-line {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  flex-shrink: 0;
}

/* ---------- 去水印模态 ---------- */
.wm-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: grid;
  place-items: center;
  z-index: 60;
}

.wm-modal {
  background: #fff;
  border-radius: var(--radius);
  padding: 16px;
  width: 620px;
  max-width: 92vw;
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.2);
}

.wm-modal h3 {
  margin: 0 0 8px;
  font-size: 15px;
}

.wm-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin: 0 0 10px;
}

.wm-frame-box {
  position: relative;
  width: 100%;
  height: 340px;
  background: #111;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.wm-frame-img {
  width: 100%;
  height: 100%;
  object-fit: fill;
  display: block;
  cursor: pointer;
}

.wm-dot {
  position: absolute;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #e11;
  border: 2px solid #fff;
  transform: translate(-50%, -50%);
  pointer-events: none;
  box-shadow: 0 0 4px rgba(0, 0, 0, 0.6);
}

.wm-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.wm-nav-info {
  font-size: 12px;
  color: var(--text-muted);
}

.wm-auto {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
}

.wm-auto-hint {
  font-size: 11px;
  color: var(--text-muted);
}

.wm-progress {
  margin-top: 8px;
  font-size: 13px;
  color: var(--brand);
}

.wm-error {
  margin-top: 8px;
  font-size: 13px;
  color: #d64545;
}

.wm-result {
  margin-top: 8px;
  font-size: 13px;
  color: #1e6fd9;
  display: flex;
  align-items: center;
  gap: 8px;
}

.wm-ops {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}

/* ---------- 下载任务队列 ---------- */
.queue-panel {
  flex-shrink: 0;
  max-height: 260px;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-status);
}

.queue-head {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 13px;
  color: var(--text);
}

.cfg {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.cfg-input {
  width: 48px;
}

.qlist {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.qlist li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

/* 下载成功后的落盘路径（超长省略） */
.q-path {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
}

.path-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.q-status {
  flex-shrink: 0;
  width: 44px;
  text-align: center;
  padding: 1px 0;
  border-radius: 4px;
  font-size: 11px;
  background: var(--bg-placeholder);
  color: var(--text-muted);
}

.q-status.running,
.q-status.retrying,
.q-status.loading {
  background: var(--brand-soft);
  color: var(--brand);
}

.q-status.success {
  background: #e7f8f0;
  color: #0f7a52;
}

.q-status.failed {
  background: #fdecea;
  color: #b73b2f;
}

.q-status.skipped {
  background: #fff4dd;
  color: #b45309;
}

.q-id {
  width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}

.q-bar {
  flex: 1;
  min-width: 80px;
  height: 10px;
  background: var(--bg-placeholder);
  border-radius: 5px;
  overflow: hidden;
}

.q-bar i {
  display: block;
  height: 100%;
  background: var(--brand);
  transition: width 0.2s ease;
}

.q-pct {
  width: 36px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.q-attempts {
  width: 52px;
  color: var(--text-muted);
}

.q-msg {
  flex: 1;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.q-empty {
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.batch-input {
  width: 100%;
  margin-top: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 12px;
  font-family: Consolas, 'Courier New', monospace;
  resize: vertical;
  box-sizing: border-box;
}

.batch-input:disabled {
  opacity: 0.6;
}

/* ---------- 百应缓存商品列表 ---------- */
.qlist li.checked {
  background: var(--brand-soft);
}

.q-check {
  flex-shrink: 0;
  width: 20px;
  text-align: center;
}

.q-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}

.q-rate {
  flex-shrink: 0;
  color: #d97706;
}

.q-source {
  flex-shrink: 0;
  width: 76px;
  color: var(--text-muted);
  font-size: 11px;
  text-align: center;
}

.q-video {
  flex-shrink: 0;
  width: 86px;
  text-align: center;
  font-size: 11px;
  padding: 1px 0;
  border-radius: 4px;
}

.q-video.ok {
  background: #e7f8f0;
  color: #0f7a52;
}

.q-video.bad {
  background: #fdecea;
  color: #b73b2f;
}

.q-time {
  flex-shrink: 0;
  width: 100px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.q-history {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--border);
}

.q-history-title {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 4px;
}

.q-history ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
}

.q-history li {
  display: flex;
  align-items: center;
  gap: 8px;
}

.q-h-meta {
  color: var(--text-muted);
  flex-shrink: 0;
}

/* ---------- 预览区 ---------- */
.preview {
  margin: 0;
  padding: 8px 10px;
  max-height: 140px;
  overflow: auto;
  background: #f8fafc;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-all;
  flex-shrink: 0;
}

/* ---------- 本地视频自动剪辑 ---------- */
.edit-panel {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: linear-gradient(110deg, #f5f8ff, var(--bg-panel) 55%);
  flex-shrink: 0;
}

.edit-heading,
.edit-source-row,
.edit-options,
.edit-actions {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  flex-wrap: wrap;
}

.edit-heading {
  justify-content: space-between;
}

.edit-heading > div {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.edit-heading b {
  color: var(--text);
  font-size: 14px;
}

.edit-heading span,
.edit-model-note,
.edit-output-hint,
.edit-status,
.edit-subtitle-path {
  color: var(--text-muted);
  font-size: 11px;
}

.edit-path {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
  font-size: 12px;
}

.edit-fields {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 8px;
}

.edit-fields label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  color: var(--text-muted);
  font-size: 11px;
}

.edit-fields .input {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
}

.edit-options label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text-muted);
  font-size: 12px;
}

.edit-time-input {
  width: 84px;
}

.edit-output-hint {
  min-width: 200px;
  flex: 1;
  overflow-wrap: anywhere;
}

.edit-actions {
  min-height: 28px;
}

.edit-status {
  flex: 1;
  min-width: 180px;
}

.edit-progress {
  height: 5px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--border);
}

.edit-progress span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--brand);
  transition: width 180ms ease;
}

.edit-error {
  margin: 0;
}

.edit-subtitle-path,
.edit-model-note {
  margin: 0;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .edit-fields {
    grid-template-columns: repeat(2, minmax(140px, 1fr));
  }
}

@media (max-width: 540px) {
  .edit-fields {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
