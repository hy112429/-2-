<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

// ---- 连通性测试 ----
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

// ---- 任务演示与取消 ----
const taskId = ref<string | null>(null)
const events = ref<Array<{ time: string; type: string; text: string }>>([])
const finalText = ref('')
const percent = ref(0)

function now(): string {
  const d = new Date()
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function tagText(type: string): string {
  return { stage: '进度', log: '日志', error: '错误', result: '结果' }[type] ?? type
}

function onEvent(e: EngineEventPayload): void {
  if (e.taskId !== taskId.value) return
  if (e.type === 'stage') {
    percent.value = e.percent ?? percent.value
    events.value.push({ time: now(), type: 'stage', text: e.stage ?? '' })
  } else if (e.type === 'log') {
    events.value.push({ time: now(), type: 'log', text: e.message ?? '' })
  } else if (e.type === 'result') {
    percent.value = 100
    finalText.value = JSON.stringify(e.data ?? null, null, 2)
    taskId.value = null
    events.value.push({ time: now(), type: 'result', text: '完成' })
  } else if (e.type === 'error') {
    finalText.value = e.message ?? '引擎返回错误'
    taskId.value = null
    events.value.push({ time: now(), type: 'error', text: e.message ?? '' })
  }
}

async function startDemo(): Promise<void> {
  if (taskId.value) return
  events.value = []
  finalText.value = ''
  percent.value = 0
  const res = await window.api.engine.run({ script: 'demo_task.py' })
  if (res.taskId) {
    taskId.value = res.taskId
  } else {
    finalText.value = res.error ?? '任务启动失败'
  }
}

async function cancelDemo(): Promise<void> {
  if (!taskId.value) return
  await window.api.engine.cancel(taskId.value)
  // 终态 error 事件会由主进程补发，这里不用重复处理
}

let off: (() => void) | undefined
onMounted(() => {
  off = window.api.engine.onEvent(onEvent)
})
onBeforeUnmount(() => {
  off?.()
})
</script>

<template>
  <section class="console">
    <header class="head">
      <h3>Python 引擎</h3>
      <button
        class="btn"
        :disabled="pingState === 'running'"
        @click="testPing"
      >
        测试连通性
      </button>
      <span class="badge" :class="pingState">
        {{ pingState === 'idle' ? '未测试' : pingState === 'running' ? '检测中…' : pingState === 'ok' ? '正常' : '失败' }}
      </span>
    </header>
    <p class="detail">{{ pingDetail }}</p>

    <div class="task-row">
      <button class="btn primary" :disabled="!!taskId" @click="startDemo">
        {{ taskId ? '任务运行中…' : '开始演示任务' }}
      </button>
      <button class="btn danger" :disabled="!taskId" @click="cancelDemo">
        取消任务
      </button>
      <span class="hint">demo_task.py ｜ 离线模拟 8 步约 4 秒，验证实时进度与取消</span>
    </div>

    <div class="bar">
      <div class="bar-fill" :style="{ width: percent + '%' }" />
      <span class="bar-text">{{ percent }}%</span>
    </div>

    <ul class="events">
      <li v-if="events.length === 0" class="empty">还没有事件 —— 点「开始演示任务」看实时进度</li>
      <li v-for="(ev, i) in events" :key="i" :class="ev.type">
        <span class="time">{{ ev.time }}</span>
        <span class="tag" :class="ev.type">{{ tagText(ev.type) }}</span>
        <span class="text">{{ ev.text }}</span>
      </li>
    </ul>

    <pre class="final">{{ finalText || '（暂无结果）' }}</pre>
  </section>
</template>

<style scoped>
.console {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  padding: 12px;
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
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--text-muted);
  word-break: break-all;
  min-height: 18px;
}

.task-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  flex-shrink: 0;
}

.hint {
  font-size: 12px;
  color: var(--text-muted);
}

.bar {
  position: relative;
  height: 18px;
  margin-top: 10px;
  background: var(--bg-placeholder);
  border-radius: var(--radius);
  overflow: hidden;
  flex-shrink: 0;
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

.events {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
  min-height: 60px;
  max-height: 180px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-status);
  font-size: 12px;
}

.events li {
  display: flex;
  gap: 6px;
  padding: 3px 8px;
}

.events li.empty {
  color: var(--text-muted);
}

.time {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.tag {
  flex-shrink: 0;
  border-radius: 4px;
  padding: 0 4px;
  font-size: 11px;
}

.tag.stage {
  background: rgba(59, 130, 246, 0.12);
  color: var(--brand);
}

.tag.log {
  background: var(--bg-placeholder);
  color: var(--text-muted);
}

.tag.error {
  background: rgba(220, 38, 38, 0.12);
  color: #dc2626;
}

.text {
  color: var(--text);
  word-break: break-all;
}

.final {
  margin: 8px 0 0;
  padding: 8px;
  max-height: 120px;
  overflow: auto;
  background: #f8fafc;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  font-size: 11px;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-all;
  flex-shrink: 0;
}
</style>