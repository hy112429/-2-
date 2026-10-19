<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 单条链接解析下载工具（T-11 真实任务接入 UI）
 * ─────────────────────────────────────────────
 * 打通「粘贴一条链接 → 解析 → 下载 → 打开文件」闭环：
 *   · 解析：engine.run link_parser.py（统一入口，抖音/头条/小红书自动识别；
 *           小红书登录 cookie 走 stdin，不经过进程命令行）
 *   · 下载：复用 download_queue.py（并发/退避重试/断点续传/SQLite 记账）
 *           落盘 {下载目录}\抖音素材\{日期}\{vid}.mp4
 * 复用产出：engine.ts（子进程管家 + NDJSON 事件流 + 进程树取消）、
 *           link_parser.py / link_batch.py、download_queue.py、app-dirs.ts
 */

// ---- 连通性测试（沿袭 EngineConsole） ----
const pingState = ref<'idle' | 'running' | 'ok' | 'fail'>('idle')
const pingDetail = ref('')

async function testPing(): Promise<void> {
  if (pingState.value === 'running') return
  pingState.value = 'running'
  pingDetail.value = ''
  const res = await window.api.engine.ping()
  if (res.ok) {
    pingState.value = 'ok'
    pingDetail.value = `Python ${res.python} ｜ requests=${res.requests ?? '?'} ｜ ${res.engine_dir ?? ''}`
  } else {
    pingState.value = 'fail'
    pingDetail.value = res.error ?? '连通性测试失败'
  }
}

// ---- 单条链接解析 ----
interface ParseOutcome {
  ok?: boolean
  platform?: string
  input?: string
  title?: string | null
  author?: string | null
  duration_ms?: number | null
  cover_url?: string | null
  no_watermark_url?: string | null
  watermarked_url?: string | null
  aweme_id?: string | null
  note_id?: string | null
  video_id?: string | null
  error?: string | null
}

const linkText = ref('')
const parsing = ref(false)
const parseEngineId = ref('')
const parseOutcome = ref<ParseOutcome | null>(null)
const parseErr = ref('')

const PLATFORM_TEXT: Record<string, string> = {
  douyin: '抖音',
  xiaohongshu: '小红书',
  toutiao: '头条/西瓜'
}

function formatDuration(ms?: number | null): string {
  if (!ms) return '—'
  return `${(ms / 1000).toFixed(1)} 秒`
}

function parseVideoId(): string {
  const o = parseOutcome.value
  return o?.video_id ?? o?.note_id ?? o?.aweme_id ?? 'video'
}

async function parseLink(): Promise<void> {
  const link = linkText.value.trim()
  if (!link || parsing.value) return
  parseOutcome.value = null
  parseErr.value = ''
  // 小红书需登录态：导出小红书 cookie（未登录则为空，小红书条目会提示登录）
  let cookie = ''
  try {
    const sess = await window.api.login.exportForEngine('xiaohongshu')
    cookie = sess?.cookieHeader ?? ''
  } catch {
    cookie = ''
  }
  const args = ['--input', link, '--timeout', '15', '--max-attempts', '8']
  parsing.value = true
  const reply = await window.api.engine.run({
    script: 'link_parser.py',
    args,
    stdinJson: { cookie } // 登录态走 stdin，不落盘、不过命令行
  })
  if (!reply.taskId) {
    parsing.value = false
    parseErr.value = reply.error ?? '解析启动失败'
    return
  }
  parseEngineId.value = reply.taskId
}

const copied = ref(false)
async function copyUrl(): Promise<void> {
  const url = parseOutcome.value?.no_watermark_url
  if (!url) return
  try {
    await navigator.clipboard.writeText(url)
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 1500)
  } catch {
    /* 剪贴板不可用时静默 */
  }
}

// ---- 下载（单任务，走 download_queue.py） ----
interface QueueTaskView {
  id: string
  status: 'pending' | 'running' | 'retrying' | 'success' | 'failed' | 'skipped'
  percent: number
  attempts: number
  message: string
  file_path?: string
}

const downloadDir = ref('')
const queueRunning = ref(false)
const queueEngineId = ref('')
const queueTask = ref<QueueTaskView | null>(null)
const queueSummary = ref('')

const STATUS_TEXT: Record<QueueTaskView['status'], string> = {
  pending: '等待',
  running: '下载中',
  retrying: '重试',
  success: '成功',
  failed: '失败',
  skipped: '跳过'
}

async function downloadParsed(): Promise<void> {
  const url = parseOutcome.value?.no_watermark_url
  if (!url || queueRunning.value) return
  const date = new Date().toISOString().slice(0, 10)
  const vid = parseVideoId()
  const tasks = [
    {
      id: vid,
      url,
      dest: `${downloadDir.value}\\抖音素材\\${date}\\${vid}.mp4`
    }
  ]
  queueTask.value = {
    id: vid,
    status: 'pending',
    percent: 0,
    attempts: 0,
    message: '等待中'
  }
  queueSummary.value = ''
  const reply = await window.api.engine.run({
    script: 'download_queue.py',
    args: ['--workers', '1', '--max-retries', '3'],
    stdinJson: tasks
  })
  if (!reply.taskId) {
    queueSummary.value = `队列启动失败：${reply.error ?? '未知错误'}`
    return
  }
  queueEngineId.value = reply.taskId
  queueRunning.value = true
}

async function cancelQueue(): Promise<void> {
  if (!queueEngineId.value) return
  await window.api.engine.cancel(queueEngineId.value)
  // 终态 error 事件由主进程补发
}

async function openQueueFolder(filePath: string): Promise<void> {
  await window.api.assets.openFolder(filePath)
}

// ---- 事件流（先订阅再启动，避免竞态） ----
let off: (() => void) | undefined

function onEngineEvent(e: EngineEventPayload): void {
  if (e.taskId === parseEngineId.value) {
    if (e.type === 'result') {
      parsing.value = false
      parseEngineId.value = ''
      parseOutcome.value = (e.data ?? {}) as ParseOutcome
      parseErr.value = parseOutcome.value?.ok
        ? ''
        : (parseOutcome.value?.error ?? '解析失败')
    } else if (e.type === 'error') {
      parsing.value = false
      parseEngineId.value = ''
      parseErr.value = e.message ?? '解析出错'
    }
    return
  }
  if (e.taskId === queueEngineId.value) {
    if (e.type === 'task' && e.tid) {
      queueTask.value = {
        id: e.tid,
        status: (e.status ?? 'running') as QueueTaskView['status'],
        percent: e.percent ?? 0,
        attempts: e.attempts ?? 0,
        message: e.message ?? '',
        file_path: e.file_path
      }
    } else if (e.type === 'stage') {
      queueSummary.value = e.stage ?? ''
    } else if (e.type === 'result') {
      queueRunning.value = false
      const d = e.data as { success?: number; failed?: number; skipped?: number } | undefined
      queueSummary.value =
        `下载完成：成功 ${d?.success ?? 0} / 失败 ${d?.failed ?? 0} / 跳过 ${d?.skipped ?? 0}`
    } else if (e.type === 'error') {
      queueRunning.value = false
      queueSummary.value = e.message ?? '下载出错'
    }
  }
}

onMounted(async () => {
  downloadDir.value = await window.api.dir.getDownloadDir()
  off = window.api.engine.onEvent(onEngineEvent)
})
onBeforeUnmount(() => {
  off?.()
})
</script>

<template>
  <section class="link-tool">
    <header class="head">
      <h3>链接解析下载</h3>
      <button class="btn" :disabled="pingState === 'running'" @click="testPing">
        测试连通性
      </button>
      <span class="badge" :class="pingState">
        {{ pingState === 'idle' ? '未测试' : pingState === 'running' ? '检测中…' : pingState === 'ok' ? '正常' : '失败' }}
      </span>
    </header>
    <p class="detail">{{ pingDetail }}</p>

    <!-- 链接输入 + 解析 -->
    <textarea
      v-model="linkText"
      class="link-input"
      rows="2"
      placeholder="粘贴一条链接：抖音分享链接 / 视频页链接 / 口令文本 / 视频ID / 小红书笔记链接"
      :disabled="parsing"
    />
    <div class="row">
      <button class="btn primary" :disabled="parsing || !linkText.trim()" @click="parseLink">
        {{ parsing ? '解析中…' : '解析链接' }}
      </button>
      <span class="hint">link_parser.py ｜ 抖音/小红书/头条自动识别，小红书需先登录</span>
    </div>
    <p v-if="parseErr" class="error-text">{{ parseErr }}</p>

    <!-- 解析结果卡 -->
    <div v-if="parseOutcome" class="result-card" :class="parseOutcome.ok ? 'ok' : 'bad'">
      <div class="r-top">
        <span class="r-platform">{{ PLATFORM_TEXT[parseOutcome.platform ?? ''] ?? parseOutcome.platform ?? '未知平台' }}</span>
        <span class="r-state" :class="parseOutcome.ok ? 'ok' : 'bad'">
          {{ parseOutcome.ok ? '解析成功' : '解析失败' }}
        </span>
      </div>
      <div v-if="parseOutcome.title" class="r-row r-title">
        <span class="r-label">标题</span><span class="r-val">{{ parseOutcome.title }}</span>
      </div>
      <div class="r-row">
        <span class="r-label">作者</span><span class="r-val">{{ parseOutcome.author || '—' }}</span>
        <span class="r-label r-sep">时长</span><span class="r-val">{{ formatDuration(parseOutcome.duration_ms) }}</span>
      </div>
      <div v-if="parseOutcome.no_watermark_url" class="r-row r-url">
        <span class="r-label">无水印地址</span>
        <span class="r-val mono" :title="parseOutcome.no_watermark_url">{{ parseOutcome.no_watermark_url }}</span>
        <button class="btn tiny" :disabled="copied" @click="copyUrl">
          {{ copied ? '已复制' : '复制' }}
        </button>
      </div>
      <div v-if="!parseOutcome.ok && parseOutcome.error" class="r-error">{{ parseOutcome.error }}</div>
    </div>

    <!-- 下载 -->
    <div class="row">
      <button
        class="btn primary"
        :disabled="!parseOutcome?.ok || !parseOutcome?.no_watermark_url || queueRunning"
        @click="downloadParsed"
      >
        {{ queueRunning ? '下载中…' : '下载该视频' }}
      </button>
      <button class="btn danger" :disabled="!queueRunning" @click="cancelQueue">取消</button>
      <span v-if="queueSummary" class="summary">{{ queueSummary }}</span>
    </div>

    <div v-if="queueTask" class="bar">
      <div class="bar-fill" :style="{ width: queueTask.percent + '%' }" />
      <span class="bar-text">
        {{ STATUS_TEXT[queueTask.status] }} {{ queueTask.percent }}%
        {{ queueTask.attempts > 0 ? `（重试 ${queueTask.attempts} 次）` : '' }}
      </span>
    </div>
    <p v-if="queueTask?.message" class="q-msg">{{ queueTask.message }}</p>

    <template v-if="queueTask?.file_path">
      <div class="path-row">
        <button class="btn tiny" title="打开所在文件夹" @click="openQueueFolder(queueTask!.file_path!)">📁</button>
        <span class="path mono" :title="queueTask.file_path">{{ queueTask.file_path }}</span>
      </div>
    </template>
  </section>
</template>

<style scoped>
.link-tool {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  padding: 12px;
  gap: 8px;
  overflow-y: auto;
}

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.btn {
  border: 1px solid var(--border);
  background: var(--bg-status);
  color: var(--text);
  border-radius: var(--radius);
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
}

.btn:hover:not(:disabled) {
  border-color: var(--brand);
  color: var(--brand);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn.primary {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}

.btn.danger {
  border-color: #dc2626;
  color: #dc2626;
}

.btn.tiny {
  padding: 1px 8px;
  font-size: 11px;
  flex-shrink: 0;
}

.badge {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--bg-placeholder);
  color: var(--text-muted);
}

.badge.ok {
  background: rgba(22, 163, 74, 0.12);
  color: #16a34a;
}

.badge.fail {
  background: rgba(220, 38, 38, 0.12);
  color: #dc2626;
}

.detail {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  word-break: break-all;
  min-height: 16px;
}

.link-input {
  flex-shrink: 0;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex-shrink: 0;
}

.hint {
  font-size: 12px;
  color: var(--text-muted);
}

.error-text {
  margin: 0;
  font-size: 12px;
  color: #dc2626;
  word-break: break-all;
}

.summary {
  font-size: 12px;
  color: var(--text-muted);
}

/* ---------- 解析结果卡 ---------- */
.result-card {
  flex-shrink: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg-status);
}

.result-card.bad {
  border-color: rgba(220, 38, 38, 0.5);
}

.r-top {
  display: flex;
  align-items: center;
  gap: 8px;
}

.r-platform {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--bg-placeholder);
  color: var(--text-muted);
}

.r-state {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 10px;
}

.r-state.ok {
  background: rgba(22, 163, 74, 0.12);
  color: #16a34a;
}

.r-state.bad {
  background: rgba(220, 38, 38, 0.12);
  color: #dc2626;
}

.r-row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  min-width: 0;
}

.r-title {
  flex-direction: column;
  gap: 2px;
}

.r-label {
  flex-shrink: 0;
  color: var(--text-muted);
  font-size: 12px;
}

.r-label.r-sep {
  margin-left: 12px;
}

.r-val {
  color: var(--text);
  word-break: break-all;
  min-width: 0;
}

.r-url .r-val {
  flex: 1;
  font-size: 11px;
}

.mono {
  font-family: Consolas, monospace;
  word-break: break-all;
}

.r-error {
  font-size: 12px;
  color: #dc2626;
  word-break: break-all;
}

/* ---------- 下载进度 ---------- */
.bar {
  position: relative;
  height: 18px;
  flex-shrink: 0;
  background: var(--bg-placeholder);
  border-radius: var(--radius);
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  background: var(--brand);
  transition: width 0.15s ease;
  width: 0;
}

.bar-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--text);
}

.q-msg {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
  word-break: break-all;
}

.path-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.path {
  font-size: 11px;
  color: var(--text-muted);
  word-break: break-all;
}
</style>
