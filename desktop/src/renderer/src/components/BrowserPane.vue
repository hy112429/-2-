<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'

const props = defineProps<{ defaultTarget?: string; profileKey?: string; profilePath?: string }>()

const targets = ref<LoginTarget[]>([])
const targetId = ref('')
const loginState = ref<LoginState | null>(null)
const busy = ref(false)
const notice = ref('')

watch(notice, (message) => {
  if (message) notifyGlobal(message)
})

/** 最近一次「导出给引擎」的自检结果（不含任何 cookie 值） */
const lastExport = ref<{
  cookieCount: number
  headerLength: number
  names: string[]
  userAgent: string
} | null>(null)

const unsubscribers: Array<() => void> = []

// ---------------------------------------------------------------- 操作

async function openLogin(): Promise<void> {
  busy.value = true
  notice.value = ''
  try {
    const result = await window.api.browser.open(targetId.value, {
      profileKey: props.profileKey ?? 'general',
      profilePath: props.profilePath || undefined
    })
    if (!result.ok) throw new Error(result.error ?? '托管 Chrome 打开失败')
    const targetName = targets.value.find((item) => item.id === targetId.value)?.name ?? '登录页'
    let destination = ''
    let platformLoginLanding = false
    try {
      const current = new URL(result.url)
      destination = `${current.origin}${current.pathname}`
      platformLoginLanding = targetId.value === 'baiying' &&
        (current.hostname.endsWith('douyinec.com') || current.pathname.includes('/mpa/account/login'))
    } catch {
      /* 页面地址解析失败不影响已打开的 Chrome 会话 */
    }
    notice.value = platformLoginLanding
      ? `已在托管 Chrome 打开百应入口，但平台当前跳转到了登录页/首页${destination ? `（${destination}）` : ''}。请在这个 Chrome profile 登录百应，再点击一次“在托管 Chrome 打开”。CDP 已连接。`
      : `已在独立 Chrome profile 打开${targetName}${destination ? `（${destination}）` : ''}。CDP 已连接，自动化会使用同一 profile；Electron 内置缓存仍相互独立。`
  } catch (error) {
    notice.value = `打开登录页失败：${String(error)}`
  } finally {
    busy.value = false
  }
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
  const ok = window.confirm('确定清除客户端内置登录缓存吗？这不会退出托管 Chrome profile 中的账号。')
  if (!ok) return
  busy.value = true
  try {
    await window.api.login.clear()
    loginState.value = await window.api.login.check()
    lastExport.value = null
    notice.value = '已清除客户端内置登录缓存；托管 Chrome profile 登录态未更改。'
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
      notice.value = '客户端内置分区没有可导出的 cookie。托管 Chrome profile 登录态不会复制到这里。'
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
  // 切换版本后若进程里还挂着旧内置登录视图，先摘除覆盖层；登录缓存本身不清除。
  await window.api.browser.close()
  targets.value = await window.api.browser.getTargets()
  if (targets.value.length > 0) {
    targetId.value = targets.value.find((target) => target.id === props.defaultTarget)?.id ?? targets.value[0].id
  }

  loginState.value = await window.api.login.check()
  // 只显示客户端内置分区的状态；托管 Chrome profile 的状态由 CDP 连接使用。
  unsubscribers.push(window.api.login.onStateChanged((s) => (loginState.value = s)))
})

onBeforeUnmount(() => {
  void window.api.browser.close()
  unsubscribers.forEach((off) => off())
  unsubscribers.length = 0
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

      <button class="btn primary" :disabled="busy" @click="openLogin">在托管 Chrome 打开</button>

      <span class="spacer" />

      <button class="btn" :disabled="busy" @click="checkLogin">检查客户端缓存态</button>
      <button class="btn" :disabled="busy" @click="exportForEngine">检查客户端缓存导出</button>
      <button class="btn danger" :disabled="busy" @click="logout">清除客户端缓存</button>
    </div>

    <!-- ---------- 登录态摘要 ---------- -->
    <div class="session-note">以下只显示客户端内置分区缓存；托管 Chrome profile 登录态由 Playwright CDP 使用。</div>
    <div class="status">
      <span class="badge" :class="currentTargetState()?.loggedIn ? 'on' : 'off'">
        {{ currentTargetState()?.loggedIn ? '● 客户端缓存已登录' : '○ 客户端缓存未登录' }}
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

    <div class="external-note">
      <div class="ph-title">登录页面在 Electron 外部的托管 Chrome 打开</div>
      <div class="ph-sub">客户端通过仅限本机的 CDP 连接复用该 Chrome profile，供页面捕获和自动化使用；登录态不会复制进 Electron 内置缓存。</div>
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

.session-note,
.external-note {
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.external-note { margin-top: auto; }
.ph-title { margin-bottom: 4px; color: var(--text); font-weight: 600; }

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
