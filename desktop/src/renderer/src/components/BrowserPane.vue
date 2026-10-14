<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * 登录面板 = 工具栏 + 内置浏览器视图的宿主容器
 *
 * 关键机制：WebContentsView 不是 DOM 元素，而是**覆盖在页面之上的独立图层**。
 * 所以这里只负责测出「宿主容器在窗口里的位置」并上报给主进程，
 * 主进程再照着这个位置摆放真正的浏览器视图。
 */

const targets = ref<LoginTarget[]>([])
const targetId = ref('')
const viewState = ref<BrowserViewState>({ open: false })
const loginState = ref<LoginState | null>(null)
const busy = ref(false)
const notice = ref('')

/** 最近一次「导出给引擎」的自检结果（不含任何 cookie 值） */
const lastExport = ref<{
  cookieCount: number
  headerLength: number
  names: string[]
  userAgent: string
} | null>(null)

const hostEl = ref<HTMLDivElement | null>(null)

let resizeObserver: ResizeObserver | null = null
const unsubscribers: Array<() => void> = []

// ---------------------------------------------------------------- 位置同步

/** 测出宿主容器的位置与尺寸，上报给主进程 */
function syncBounds(): void {
  const el = hostEl.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  window.api.browser.setBounds({
    x: Math.round(rect.left),
    y: Math.round(rect.top),
    width: Math.round(rect.width),
    height: Math.round(rect.height)
  })
}

// ---------------------------------------------------------------- 操作

async function openLogin(): Promise<void> {
  busy.value = true
  notice.value = ''
  try {
    // 必须先上报位置再打开：主进程是按「上次上报的位置」摆放视图的，
    // 顺序反了会先闪一个错位的窗口
    syncBounds()
    await window.api.browser.open(targetId.value)
    viewState.value = await window.api.browser.getState()
  } catch (error) {
    notice.value = `打开登录页失败：${String(error)}`
  } finally {
    busy.value = false
  }
}

async function closeView(): Promise<void> {
  await window.api.browser.close()
  viewState.value = await window.api.browser.getState()
}

// 模板里不能直接写 window.xxx（Vue 模板作用域不含 window），必须包一层
async function goBack(): Promise<void> {
  await window.api.browser.goBack()
}

async function goForward(): Promise<void> {
  await window.api.browser.goForward()
}

async function reload(): Promise<void> {
  await window.api.browser.reload()
}

async function checkLogin(): Promise<void> {
  busy.value = true
  try {
    loginState.value = await window.api.login.check()
  } finally {
    busy.value = false
  }
}

async function logout(): Promise<void> {
  const ok = window.confirm('确定要退出登录吗？将清除抖音 / 百应相关的 cookie。')
  if (!ok) return
  busy.value = true
  try {
    await window.api.login.clear()
    loginState.value = await window.api.login.check()
    lastExport.value = null
    notice.value = '已清除登录态'
  } finally {
    busy.value = false
  }
}

/**
 * 自检：导出登录态给 Python 引擎用。
 *
 * 注意这里**只显示条数、长度、名字**，不显示任何 cookie 值 ——
 * 凭据不该出现在界面上，更不该被截屏或录屏带出去。
 */
async function exportForEngine(): Promise<void> {
  busy.value = true
  notice.value = ''
  try {
    const session = await window.api.login.exportForEngine(targetId.value)
    lastExport.value = {
      cookieCount: session.cookieCount,
      headerLength: session.cookieHeader.length,
      names: session.cookieNames,
      userAgent: session.userAgent
    }
    if (session.cookieCount === 0) {
      notice.value = '没有可导出的 cookie，请先完成扫码登录'
    }
  } catch (error) {
    notice.value = `导出失败：${String(error)}`
  } finally {
    busy.value = false
  }
}

// ---------------------------------------------------------------- 状态派生

/** 当前目标的登录态 */
function currentTargetState(): TargetLoginState | null {
  return loginState.value?.targets.find((t) => t.id === targetId.value) ?? null
}

/** 凭证加密备份的显示文本（只显示元信息，不显示任何值） */
const backupText = computed(() => {
  const b = loginState.value?.credentialBackup
  if (!b) return '—'
  if (b.error) return `保存失败（${b.error}）`
  if (!b.exists) return '无'
  const t = b.updatedAt ? new Date(b.updatedAt).toLocaleTimeString('zh-CN', { hour12: false }) : ''
  return `已加密保存 ${b.cookieCount ?? 0} 条（${t}）`
})

// ---------------------------------------------------------------- 生命周期

onMounted(async () => {
  targets.value = await window.api.browser.getTargets()
  if (targets.value.length > 0) targetId.value = targets.value[0].id

  viewState.value = await window.api.browser.getState()
  loginState.value = await window.api.login.check()

  // 监听宿主容器尺寸变化（侧栏折叠、窗口缩放都会触发）
  if (hostEl.value) {
    resizeObserver = new ResizeObserver(() => syncBounds())
    resizeObserver.observe(hostEl.value)
  }

  unsubscribers.push(window.api.browser.onStateChanged((s) => (viewState.value = s)))

  // 主进程在窗口 resize 时会主动要一次位置
  unsubscribers.push(window.api.browser.onNeedBounds(() => syncBounds()))

  unsubscribers.push(
    window.api.browser.onLoadFailed((e) => {
      notice.value = `页面加载失败（${e.code} ${e.desc}）`
    })
  )

  // 扫码成功后主进程会自动推送，不用手动点按钮
  unsubscribers.push(window.api.login.onStateChanged((s) => (loginState.value = s)))

  // 布局稳定后再量一次，避免首帧尺寸为 0
  requestAnimationFrame(() => syncBounds())
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  unsubscribers.forEach((off) => off())
  unsubscribers.length = 0
  // 组件卸载时把覆盖层收掉，避免残留一个看不见的浏览器视图挡住界面
  void window.api.browser.close()
})
</script>

<template>
  <section class="pane">
    <!-- ---------- 工具栏 ---------- -->
    <div class="toolbar">
      <span class="label">登录账号</span>

      <select v-model="targetId" class="select" :disabled="busy">
        <option v-for="t in targets" :key="t.id" :value="t.id">{{ t.name }}</option>
      </select>

      <button class="btn primary" :disabled="busy" @click="openLogin">
        {{ viewState.open ? '重新打开登录页' : '打开登录页' }}
      </button>

      <button class="btn" :disabled="busy || !viewState.open" @click="closeView">关闭视图</button>

      <span class="divider" />

      <button class="btn" :disabled="busy || !viewState.open" title="后退" @click="goBack">←</button>
      <button class="btn" :disabled="busy || !viewState.open" title="前进" @click="goForward">→</button>
      <button class="btn" :disabled="busy || !viewState.open" title="刷新" @click="reload">⟳</button>

      <span class="spacer" />

      <button class="btn" :disabled="busy" @click="checkLogin">检查登录状态</button>
      <button class="btn" :disabled="busy" @click="exportForEngine">导出给引擎（自检）</button>
      <button class="btn danger" :disabled="busy" @click="logout">登出</button>
    </div>

    <!-- ---------- 登录态摘要 ---------- -->
    <div class="status">
      <span class="badge" :class="currentTargetState()?.loggedIn ? 'on' : 'off'">
        {{ currentTargetState()?.loggedIn ? '● 已登录' : '○ 未登录' }}
      </span>

      <span class="meta">
        命中凭证：<b>{{ currentTargetState()?.matchedCookies.join('、') || '无' }}</b>
      </span>
      <span class="meta">
        该域 cookie 条数：<b>{{ currentTargetState()?.cookieCount ?? 0 }}</b>
      </span>
      <span class="meta">
        加密：<b>{{ loginState?.encryptionAvailable ? '可用' : '不可用' }}</b>
        （{{ loginState?.encryptionBackend || '未知' }}）
      </span>
      <span class="meta">
        凭证副本：
        <b :class="loginState?.credentialBackup?.exists ? 'ok' : ''">
          {{ backupText }}
        </b>
      </span>
    </div>

    <!-- ---------- 提示 ---------- -->
    <p v-if="notice" class="notice">{{ notice }}</p>

    <!-- ---------- 导出自检结果 ---------- -->
    <div v-if="lastExport" class="export">
      <div class="export-title">最近一次导出（仅自检，<b>cookie 值不显示</b>）</div>
      <div class="export-row">cookie 条数：<b>{{ lastExport.cookieCount }}</b></div>
      <div class="export-row">请求头长度：<b>{{ lastExport.headerLength }}</b> 字符</div>
      <div class="export-row break">
        包含的 cookie：<b>{{ lastExport.names.join('、') || '（无）' }}</b>
      </div>
      <div class="export-row break">User-Agent：<span class="mono">{{ lastExport.userAgent }}</span></div>
      <div class="export-note">
        引擎发请求时必须同时带上面这个 UA —— 抖音的风控会把登录态和 UA 关联，
        UA 不一致可能导致会话被判异常。
      </div>
    </div>

    <!-- ---------- 浏览器视图宿主 ---------- -->
    <div ref="hostEl" class="host">
      <div v-if="!viewState.open" class="host-placeholder">
        <div class="ph-title">内置浏览器视图</div>
        <div class="ph-sub">选择目标后点「打开登录页」，用手机扫码完成登录</div>
        <div class="ph-hint">登录态会加密保存在本地，下次启动自动恢复</div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  height: 100%;
}

/* ---------- 工具栏 ---------- */
.toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  flex-shrink: 0;
}

.label {
  color: var(--text-muted);
  font-size: 13px;
}

.select {
  height: 28px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
}

.btn {
  height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text);
  font-size: 13px;
  transition: background 0.15s, border-color 0.15s;
}

.btn:hover:not(:disabled) {
  background: var(--brand-soft);
  border-color: var(--brand);
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn.primary {
  background: var(--brand);
  border-color: var(--brand);
  color: #ffffff;
}

.btn.primary:hover:not(:disabled) {
  background: #2f6fd8;
}

.btn.danger:hover:not(:disabled) {
  background: #fdecea;
  border-color: #e0523f;
  color: #b73b2f;
}

.divider {
  width: 1px;
  height: 18px;
  background: var(--border);
  margin: 0 2px;
}

.spacer {
  flex: 1;
}

/* ---------- 状态摘要 ---------- */
.status {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 6px 10px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.badge {
  padding: 2px 8px;
  border-radius: var(--radius);
  font-weight: 600;
}

.badge.on {
  background: #e7f8f0;
  color: #0f7a52;
}

.badge.off {
  background: #f0f1f3;
  color: #6b7280;
}

.meta b {
  color: var(--text);
}

.meta b.ok {
  color: #0f7a52;
}

/* ---------- 提示 / 导出 ---------- */
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

.export {
  padding: 8px 10px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.export-title {
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}

.export-row {
  line-height: 1.7;
}

.export-row.break {
  word-break: break-all;
}

.export-row b {
  color: var(--text);
}

.export-note {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
  color: #7a5a10;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
}

/* ---------- 宿主容器（浏览器视图会盖在这上面） ---------- */
.host {
  position: relative;
  flex: 1;
  min-height: 160px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-placeholder);
}

.host-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.ph-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.ph-sub {
  color: var(--text-muted);
  font-size: 13px;
}

.ph-hint {
  color: var(--text-muted);
  font-size: 12px;
  opacity: 0.8;
}
</style>