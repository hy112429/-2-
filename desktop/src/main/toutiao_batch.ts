import { ipcMain, BrowserWindow, type WebContents } from 'electron'
import { onVodCaptured } from './probe'
import { ensureLoginView, attachLoginViewAt, detachLoginView } from './login'

/**
 * 头条/西瓜「页面批量解析」驱动（Step 10 头条补充）
 * ─────────────────────────────────────────────────────────────
 * 背景：头条视频页有字节 JS 防护（_$jsvmprt），纯 requests 拿不到数据；
 * 但客户端内置的 WebContentsView 是真浏览器，天然过防护——
 * 页面播放视频时会产生 toutiaovod.com 直链，探针（probe.ts）捕获后通知。
 *
 * 流程：链接列表 → 逐个在登录视图打开页面 → 等探针捕获 vod 直链 →
 * 固定间隔限速 → 下一个。完成后一次性回传结果，供界面入下载队列。
 */

/** 单个链接最大等待时间：页面加载 + 播放触发 30s */
const LOAD_TIMEOUT_MS = 20_000
const VOD_TIMEOUT_MS = 30_000
const MAX_BATCH = 200
const MIN_INTERVAL_MS = 3_000

interface BatchRun {
  flag: { canceled: boolean }
}

const RUNNING = new Map<string, BatchRun>()
let seq = 0

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms))

/** 打开页面，等 did-finish-load / did-fail-load（超时兜底） */
function loadPage(wc: WebContents, url: string): Promise<{ ok: boolean; desc?: string }> {
  return new Promise((resolve) => {
    let settled = false
    const finish = (r: { ok: boolean; desc?: string }): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      wc.removeListener('did-finish-load', onFinish)
      wc.removeListener('did-fail-load', onFail)
      resolve(r)
    }
    const onFinish = (): void => finish({ ok: true })
    const onFail = (_e: unknown, code: number, desc: string): void => finish({ ok: false, desc: `${desc}（${code}）` })
    const timer = setTimeout(() => finish({ ok: false, desc: '页面加载超时' }), LOAD_TIMEOUT_MS)

    wc.on('did-finish-load', onFinish)
    wc.on('did-fail-load', onFail)
    void wc.loadURL(url).catch(() => finish({ ok: false, desc: 'loadURL 抛出异常' }))
  })
}

/** 等待探针捕获 vod 直链（相对 before 出现新直链），超时返回 null */
function waitVod(
  before: Set<string>,
  getVod: () => Set<string>,
  timeoutMs: number,
  flag: { canceled: boolean }
): Promise<string | null> {
  return new Promise((resolve) => {
    const started = Date.now()
    const timer = setInterval(() => {
      if (flag.canceled) {
        clearInterval(timer)
        resolve(null)
        return
      }
      const cur = getVod()
      if (cur.size > before.size) {
        for (const u of cur) {
          if (!before.has(u)) {
            clearInterval(timer)
            resolve(u)
            return
          }
        }
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
  links: string[],
  intervalMs: number,
  flag: { canceled: boolean }
): Promise<void> {
  const emit = (payload: Record<string, unknown>): void => {
    if (!sender.isDestroyed()) sender.send('toutiao:event', { taskId, ...payload })
  }

  const view = ensureLoginView()
  const wc = view.webContents

  const host = BrowserWindow.getAllWindows().find((w) => !w.isDestroyed())
  if (host) {
    const [width, height] = host.getContentSize()
    attachLoginViewAt({ x: 0, y: 0, width, height })
  }

  let vodSet = new Set<string>()
  const offVod = onVodCaptured((v) => {
    vodSet = new Set([...vodSet, v.url])
  })

  const total = links.length
  let success = 0
  let failed = 0
  const items: Array<{ input: string; ok: boolean; vod_url: string | null; error: string | null }> = []

  try {
    for (let i = 0; i < total; i++) {
      if (flag.canceled) break
      const link = links[i]
      emit({
        type: 'stage',
        index: i,
        total,
        percent: Math.round((i / total) * 100),
        input: link,
        message: `第 ${i + 1}/${total} 个：${link.slice(0, 60)}`
      })

      const before = new Set(vodSet)
      const loaded = await loadPage(wc, link)
      if (flag.canceled) break

      if (loaded.ok) {
        const vodUrl = await waitVod(before, () => vodSet, VOD_TIMEOUT_MS, flag)
        if (flag.canceled) break
        if (vodUrl) {
          success++
          items.push({ input: link, ok: true, vod_url: vodUrl, error: null })
          emit({ type: 'item', index: i, input: link, status: 'success', message: '已捕获 vod 直链' })
        } else {
          failed++
          items.push({ input: link, ok: false, vod_url: null, error: '未捕获到视频直链（页面可能未播放视频，或该内容非视频）' })
          emit({ type: 'item', index: i, input: link, status: 'failed', message: '未捕获到视频直链' })
        }
      } else {
        failed++
        items.push({ input: link, ok: false, vod_url: null, error: `页面加载失败：${loaded.desc}` })
        emit({ type: 'item', index: i, input: link, status: 'failed', message: `页面加载失败：${loaded.desc}` })
      }

      if (i < total - 1 && !flag.canceled) {
        await sleep(intervalMs + Math.round(Math.random() * 2000))
      }
    }
  } finally {
    offVod()
    // 停止后台播放：清空页面后再摘除视图，避免视频在后台继续播
    try {
      wc.stop()
      void wc.loadURL('about:blank').catch(() => undefined)
    } catch {
      /* 忽略 */
    }
    detachLoginView()
    RUNNING.delete(taskId)
  }

  emit({ type: 'result', total, success, failed, items, canceled: flag.canceled })
}

// ---------------------------------------------------------------- IPC 注册

export function registerToutiaoBatchIpc(): void {
  ipcMain.handle(
    'toutiao:batchStart',
    async (event, params: { links?: string[]; intervalMs?: number }) => {
      const links = (Array.isArray(params?.links) ? params.links : [])
        .map((s) => String(s).trim())
        .filter((s) => /toutiao\.com|ixigua\.com|toutiaovod\.com|365yg\.com/.test(s))
      if (links.length === 0) {
        return { ok: false, error: '链接列表为空或不是头条系链接' }
      }
      if (links.length > MAX_BATCH) return { ok: false, error: `单批最多 ${MAX_BATCH} 条，请分批` }

      const intervalMs = Math.max(MIN_INTERVAL_MS, Math.min(60_000, params?.intervalMs ?? 6_000))
      const taskId = `toutiao-batch-${++seq}`
      const flag = { canceled: false }
      RUNNING.set(taskId, { flag })

      void runBatch(taskId, event.sender, links, intervalMs, flag)
      return { ok: true, taskId }
    }
  )

  ipcMain.handle('toutiao:batchCancel', (_e, taskId: string) => {
    const run = RUNNING.get(taskId)
    if (!run) return { ok: true, alreadyEnded: true }
    run.flag.canceled = true
    return { ok: true }
  })
}

export function disposeToutiaoBatch(): void {
  for (const run of RUNNING.values()) {
    run.flag.canceled = true
  }
  RUNNING.clear()
}
