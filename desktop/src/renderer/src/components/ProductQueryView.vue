<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

/**
 * 商品查询主界面（真实数据链路）
 *
 * 数据链路：搜索商品 ID → engine:run 拉起 engine/baiying_cache.py get →
 * 读取百应缓存库（探针在用户浏览百应时自动采集）。命中返回真实商品信息 + 视频直链。
 */

interface CachedProduct {
  product_id: string
  commodity_id?: string | null
  title?: string | null
  cover_url?: string | null
  cos_ratio?: number | null
  video_url?: string | null
  captured_at: string
}

// ---------------------------------------------------------------- 搜索状态

const productId = ref('')
const searchBusy = ref(false)
const error = ref('')
const foundProduct = ref<CachedProduct | null>(null)

// 下载目录
const downloadDir = ref('')
const dirNotice = ref('')

// 预览
const previewText = ref('')

// ---------------------------------------------------------------- 引擎调用

/** 查百应缓存商品（baiying_cache.py get --product-id） */
function queryCachedProduct(id: string): Promise<{ found: boolean; product?: CachedProduct }> {
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

    // 先订阅再启动：引擎脚本秒回，先 run 再订阅会丢 result 事件（竞态）
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'result') {
        const d = e.data as { found?: boolean; product?: CachedProduct | null }
        settle(() => resolve({ found: Boolean(d?.found), product: d?.product ?? undefined }))
      } else if (e.type === 'error') {
        settle(() => reject(new Error(e.message ?? '引擎返回错误')))
      }
    })

    window.setTimeout(() => settle(() => reject(new Error('引擎查询超时（30 秒）'))), 30_000)

    void window.api.engine
      .run({ script: 'baiying_cache.py', args: ['get', '--product-id', id] })
      .then((reply) => {
        if (!reply.taskId) {
          settle(() => reject(new Error(reply.error ?? '引擎任务启动失败')))
          return
        }
        taskId = reply.taskId
      })
  })
}

async function search(): Promise<void> {
  const id = productId.value.trim()
  if (!id) {
    error.value = '请输入商品 ID'
    return
  }
  searchBusy.value = true
  error.value = ''
  foundProduct.value = null
  previewText.value = ''
  try {
    const r = await queryCachedProduct(id)
    if (!r.found || !r.product) {
      error.value = '缓存中无此商品：先在百应打开该商品详情，或用下方「批量抓取」收集后重试'
    } else {
      foundProduct.value = r.product
    }
  } catch (err) {
    error.value = String(err)
  } finally {
    searchBusy.value = false
  }
}

// ---------------------------------------------------------------- 预览 / 下载目录

function videoPreview(): void {
  const p = foundProduct.value
  if (!p) {
    previewText.value = '（未选择商品）请先搜索'
    return
  }
  if (!p.video_url) {
    previewText.value = '该商品暂无视频直链（需先在百应打开详情刷新缓存）'
    return
  }
  previewText.value = `【${p.title ?? p.product_id}】\n商品ID：${p.product_id}\n佣金率：${p.cos_ratio ?? '-'}%\n视频地址：${p.video_url}`
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

const batchIdsText = ref('')
const batchInterval = ref(8) // 秒
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
  const reply = await window.api.baiying.batchStart({
    productIds: ids,
    intervalMs: Math.max(2, Math.round(batchInterval.value)) * 1000
  })
  if (!reply.taskId) {
    batchSummary.value = `批量抓取启动失败：${reply.error ?? '未知错误'}`
    return
  }
  batchTaskId.value = reply.taskId
  batchRunning.value = true
  batchItems.value = []
  batchSummary.value = `已启动：${ids.length} 个商品，间隔 ${batchInterval.value}s`
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
  return Boolean(p.video_url)
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
  const date = new Date().toISOString().slice(0, 10)
  const tasks = rows.map((p) => ({
    id: p.product_id,
    url: p.video_url as string,
    dest: `${downloadDir.value}\\百应素材\\${date}\\${p.product_id}\\${p.product_id}.mp4`
  }))
  queueTasks.value = tasks.map((t) => ({
    ...t,
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
async function downloadFound(): Promise<void> {
  const p = foundProduct.value
  if (!p?.video_url || queueRunning.value) return
  const date = new Date().toISOString().slice(0, 10)
  const tasks = [
    {
      id: p.product_id,
      url: p.video_url,
      dest: `${downloadDir.value}\\百应素材\\${date}\\${p.product_id}\\${p.product_id}.mp4`
    }
  ]
  queueTasks.value = tasks.map((t) => ({
    ...t,
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
})

onBeforeUnmount(() => {
  offQueue?.()
  offBatch?.()
  offDy?.()
  offToutiao?.()
})
</script>

<template>
  <section class="query">
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

      <button class="btn primary" :disabled="searchBusy" @click="search">
        {{ searchBusy ? '查询中…' : '搜索' }}
      </button>
      <span v-if="error" class="error">{{ error }}</span>
    </div>

    <!-- ---------- 真实数据标注 ---------- -->
    <p v-if="foundProduct" class="real-tag">数据来源：百应缓存库（用户在百应浏览时自动采集）</p>

    <!-- ---------- 商品信息 ---------- -->
    <div v-if="foundProduct" class="product-card">
      <div class="p-col p-name">
        <span class="p-label">商品名称</span><b>{{ foundProduct.title ?? '（未抓取详情）' }}</b>
      </div>
      <div class="p-col"><span class="p-label">商品 ID</span><span class="mono">{{ foundProduct.product_id }}</span></div>
      <div class="p-col p-rate"><span class="p-label">佣金率</span><b>{{ foundProduct.cos_ratio ?? '-' }}%</b></div>
      <div class="p-col"><span class="p-label">抓取时间</span>{{ foundProduct.captured_at.slice(0, 16) }}</div>
    </div>

    <!-- ---------- 视频结果（单条：商品素材视频直链） ---------- -->
    <div class="table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th class="c-check">勾选</th>
            <th class="c-video">视频状态</th>
            <th class="c-url">视频地址</th>
          </tr>
        </thead>
        <tbody>
          <template v-if="foundProduct">
            <tr :class="{ checked: foundProduct.video_url }">
              <td class="c-check">
                <input type="checkbox" :checked="Boolean(foundProduct.video_url)" disabled />
              </td>
              <td class="c-video">
                <span class="q-video" :class="foundProduct.video_url ? 'ok' : 'bad'">
                  {{ foundProduct.video_url ? '有视频直链' : '无视频' }}
                </span>
              </td>
              <td class="c-url mono" :title="foundProduct.video_url ?? ''">
                {{ foundProduct.video_url ?? '（需先在百应打开该商品详情刷新缓存）' }}
              </td>
            </tr>
          </template>
          <template v-else>
            <tr class="empty-row"><td colspan="3">搜索商品 ID 后展示真实商品视频直链</td></tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- ---------- 底部操作条 ---------- -->
    <div class="action-bar">
      <button class="btn primary" :disabled="!foundProduct?.video_url || queueRunning" @click="downloadFound">
        {{ queueRunning ? '队列运行中…' : '下载该视频' }}
      </button>
      <button class="btn danger" :disabled="!queueRunning" @click="cancelAll">取消全部</button>
      <button class="btn" @click="chooseDir">设置下载目录</button>
      <button class="btn" :disabled="!foundProduct" @click="videoPreview">视频预览</button>
      <button class="btn" @click="clearPreview">清空预览</button>

      <span class="spacer" />
    </div>

    <p v-if="dirNotice" class="notice">{{ dirNotice }}</p>
    <p class="dir-line">当前下载目录：<span class="mono">{{ downloadDir }}</span></p>

    <!-- ---------- 百应批量抓取（T-15） ---------- -->
    <div class="queue-panel">
      <div class="queue-head">
        <b>百应批量抓取</b>
        <label class="cfg">
          间隔（秒）
          <input v-model.number="batchInterval" class="input cfg-input" type="number" min="2" max="60" />
        </label>
        <button class="btn primary" :disabled="batchRunning" @click="startBatch">开始抓取</button>
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
        说明：需先在「巨量百应」工作台扫码登录；抓取在登录视图中后台进行，成功后商品进入百应缓存库
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
          <span v-if="p.cos_ratio != null" class="q-rate">佣金 {{ p.cos_ratio }}%</span>
          <span class="q-video" :class="cachedHasVideo(p) ? 'ok' : 'bad'">
            {{ cachedHasVideo(p) ? '有视频直链' : '无视频' }}
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
        placeholder="每行一条：抖音分享链接 / 视频页链接 / 口令文本 / 视频ID（如 https://v.douyin.com/xxxx/ 或 7693062724168249969）。解析成功后自动去水印并进入下载队列"
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
            <button class="btn tiny" title="去除移动水印（框选模板）" @click="openWatermark(t)">去水印</button>
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
          <img v-if="wmCur" ref="wmImgEl" :src="wmFrameUrl" class="wm-frame-img" @click="wmClick" alt="帧" />
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
}

.query > * {
  flex-shrink: 0;
}

/* ---------- 搜索区 ---------- */
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  flex-shrink: 0;
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
  background: var(--bg-status);
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
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
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
</style>