<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'

interface QueueRow {
  id: number
  account_id: number
  account_name: string
  product_id: string
  product_name: string
  title: string
  video_path: string
  schedule_at: string
  status: string
  platform_status: string
  platform_error: string
  updated_at: string
}

interface SelectedProduct {
  account_id: number
  product_id: string
  product_name: string
  selected_at: string
}

interface QueueAccount {
  id: number
  name: string
  status: string
}

const emit = defineEmits<{ (event: 'navigate', value: string): void }>()

const rows = ref<QueueRow[]>([])
const products = ref<SelectedProduct[]>([])
const accounts = ref<QueueAccount[]>([])
const loading = ref(false)
const loadError = ref('')
const accountFilter = ref('all')
const statusFilter = ref('all')
const searchText = ref('')
const dateFrom = ref('')
const dateTo = ref('')
const currentPage = ref(1)
const pageSize = 10

watch(loadError, (message) => {
  if (message) notifyGlobal(`看板加载失败：${message}`, 'error')
})

function runList(): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let taskId: string | null = null
    let settled = false
    let timer = 0
    let unsubscribe: () => void = () => undefined
    const pending: EngineEventPayload[] = []

    const finish = (callback: () => void): void => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      unsubscribe()
      callback()
    }
    const onEvent = (event: EngineEventPayload): void => {
      if (taskId && event.taskId !== taskId) return
      if (event.type === 'result') {
        finish(() => resolve((event.data ?? {}) as Record<string, unknown>))
      } else if (event.type === 'error') {
        finish(() => reject(new Error(event.message ?? '看板数据读取失败')))
      }
    }

    unsubscribe = window.api.engine.onEvent((event) => {
      if (!taskId) pending.push(event)
      else onEvent(event)
    })
    timer = window.setTimeout(() => finish(() => reject(new Error('读取看板数据超时'))), 30_000)
    void window.api.engine.run({
      script: 'publish_queue.py',
      args: ['--db', 'D:\\software\\投广客户端\\data\\ad_client.db', 'list', '--limit', '1000']
    }).then((reply) => {
      if (!reply.taskId) {
        finish(() => reject(new Error(reply.error ?? '看板数据读取任务未启动')))
        return
      }
      taskId = reply.taskId
      for (const event of pending.splice(0)) onEvent(event)
    }).catch((error: unknown) => {
      finish(() => reject(error instanceof Error ? error : new Error(String(error))))
    })
  })
}

async function refresh(): Promise<void> {
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const result = await runList()
    if (result.ok !== true) throw new Error(String(result.error ?? '发布队列未返回有效结果'))
    rows.value = (Array.isArray(result.rows) ? result.rows : []) as QueueRow[]
    products.value = (Array.isArray(result.products) ? result.products : []) as SelectedProduct[]
    accounts.value = (Array.isArray(result.accounts) ? result.accounts : []) as QueueAccount[]
    if (accountFilter.value !== 'all' && !accounts.value.some((account) => String(account.id) === accountFilter.value)) {
      accountFilter.value = 'all'
    }
  } catch (error) {
    loadError.value = `读取运营看板失败：${String(error)}`
  } finally {
    loading.value = false
  }
}

const activeRows = computed(() => rows.value.filter((row) => row.status !== 'cancelled'))
const attentionRows = computed(() => rows.value.filter((row) =>
  row.status === 'awaiting_confirmation' ||
  ['needs_review', 'submission_uncertain'].includes(row.platform_status)
).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 3))
const metrics = computed(() => [
  { label: '已选商品', value: products.value.length, note: '所有账号', tone: 'blue', mark: '品' },
  { label: '需要处理', value: rows.value.filter((row) =>
    row.status === 'awaiting_confirmation' || ['needs_review', 'submission_uncertain'].includes(row.platform_status)
  ).length, note: '待确认或待核查', tone: 'amber', mark: '待' },
  { label: '已发布', value: rows.value.filter((row) => row.platform_status === 'published').length, note: '平台已确认', tone: 'green', mark: '发' }
])

const filteredRows = computed(() => {
  const query = searchText.value.trim().toLocaleLowerCase()
  return rows.value.filter((row) => {
    if (accountFilter.value !== 'all' && String(row.account_id) !== accountFilter.value) return false
    if (statusFilter.value !== 'all' && row.platform_status !== statusFilter.value && row.status !== statusFilter.value) return false
    const planDate = String(row.schedule_at ?? '').slice(0, 10)
    if (dateFrom.value && planDate < dateFrom.value) return false
    if (dateTo.value && planDate > dateTo.value) return false
    if (query && ![row.product_name, row.title, row.product_id, row.account_name]
      .some((value) => String(value ?? '').toLocaleLowerCase().includes(query))) return false
    return true
  }).sort((a, b) => {
    const riskA = ['needs_review', 'submission_uncertain', 'submitting'].includes(a.platform_status) ? 0 : a.status === 'awaiting_confirmation' ? 1 : 2
    const riskB = ['needs_review', 'submission_uncertain', 'submitting'].includes(b.platform_status) ? 0 : b.status === 'awaiting_confirmation' ? 1 : 2
    return riskA - riskB || String(b.updated_at).localeCompare(String(a.updated_at))
  })
})

const pageCount = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize)))
const visibleRows = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return filteredRows.value.slice(start, start + pageSize)
})
const firstVisibleRow = computed(() => filteredRows.value.length === 0 ? 0 : (currentPage.value - 1) * pageSize + 1)
const lastVisibleRow = computed(() => Math.min(currentPage.value * pageSize, filteredRows.value.length))

watch([accountFilter, statusFilter, searchText, dateFrom, dateTo], () => {
  currentPage.value = 1
})

watch(pageCount, (count) => {
  if (currentPage.value > count) currentPage.value = count
})

function changePage(offset: number): void {
  currentPage.value = Math.min(pageCount.value, Math.max(1, currentPage.value + offset))
}

function queueStatus(row: QueueRow): string {
  if (row.status === 'cancelled') return '已取消'
  if (row.status === 'awaiting_confirmation') return '待确认'
  return '已排期'
}

function platformStatus(status: string): string {
  const labels: Record<string, string> = {
    not_uploaded: '待上传',
    uploading: '上传中',
    uploaded_waiting_publish: '待手动发布',
    submitting: '发布提交中',
    submitted_review: '审核中',
    needs_review: '需核查',
    submission_uncertain: '发布结果待核查',
    published: '已发布'
  }
  return labels[status] ?? '未知状态'
}

function platformTone(status: string): string {
  if (status === 'needs_review' || status === 'submission_uncertain') return 'danger'
  if (status === 'published') return 'success'
  if (status === 'submitted_review' || status === 'uploading' || status === 'submitting') return 'info'
  if (status === 'uploaded_waiting_publish') return 'warning'
  return 'muted'
}

function formatTime(value: string): string {
  if (!value) return '—'
  const normalized = value.replace('T', ' ')
  return normalized.length > 16 ? normalized.slice(0, 16) : normalized
}

function fileName(value: string): string {
  return value.split(/[\\/]/).filter(Boolean).pop() ?? '视频文件未读取'
}

onMounted(() => void refresh())
</script>

<template>
  <section class="dashboard">
    <header class="dashboard-heading">
      <div>
        <div class="eyebrow">工作台</div>
        <h1>今天的工作</h1>
        <p>从选品开始，随时查看发布和千川素材进度。</p>
      </div>
      <div class="heading-actions">
        <button type="button" class="button button-quiet" :disabled="loading" @click="refresh">
          <span class="refresh-mark" :class="{ spinning: loading }">↻</span>{{ loading ? '正在更新' : '刷新数据' }}
        </button>
        <button type="button" class="button button-primary" @click="emit('navigate', '选品设置')">开始选品</button>
      </div>
    </header>

    <div v-if="loadError" class="load-error" role="alert">
      <span>{{ loadError }}</span>
      <button type="button" @click="refresh">重试</button>
    </div>

    <section class="metric-grid" aria-label="运营数据概览">
      <article v-for="metric in metrics" :key="metric.label" class="metric-card">
        <div class="metric-top">
          <span class="metric-label">{{ metric.label }}</span>
          <span class="metric-mark" :class="metric.tone">{{ metric.mark }}</span>
        </div>
        <div class="metric-value">{{ metric.value }}</div>
        <div class="metric-note">{{ metric.note }}</div>
      </article>
    </section>

    <section class="workflow-card" aria-label="业务流程">
      <div class="section-heading">
        <div>
          <h2>常用操作</h2>
          <span>按顺序查看每一步</span>
        </div>
        <div class="account-scope"><i></i>统计所有账号</div>
      </div>
      <div class="workflow-steps">
        <button type="button" class="workflow-step" @click="emit('navigate', '选品设置')">
          <span class="step-icon step-blue">01</span>
          <span class="step-copy"><b>开始选品</b><small>{{ products.length }} 个商品已选</small></span>
          <span class="step-arrow">›</span>
        </button>
        <button type="button" class="workflow-step" @click="emit('navigate', '发布队列')">
          <span class="step-icon step-purple">02</span>
          <span class="step-copy"><b>查看发布</b><small>{{ activeRows.length }} 条发布任务</small></span>
          <span class="step-arrow">›</span>
        </button>
        <button type="button" class="workflow-step" @click="emit('navigate', '千川管理')">
          <span class="step-icon step-green">03</span>
          <span class="step-copy"><b>查看千川素材</b><small>上传和审核状态</small></span>
          <span class="step-arrow">›</span>
        </button>
      </div>
    </section>

    <section class="task-card">
      <div class="task-heading">
        <div>
          <h2>需要你处理</h2>
          <span>没有待处理事项时，流程会继续运行</span>
        </div>
        <button type="button" class="text-action" @click="emit('navigate', '发布队列')">查看发布进度 <span>→</span></button>
      </div>

      <div v-if="attentionRows.length" class="attention-list">
        <button v-for="row in attentionRows" :key="row.id" type="button" class="attention-item" @click="emit('navigate', '发布队列')">
          <span><strong>{{ row.product_name || row.title || '未命名商品' }}</strong><small>{{ row.account_name }} · {{ row.status === 'awaiting_confirmation' ? '待确认' : platformStatus(row.platform_status) }}</small></span>
          <span class="attention-arrow">查看 ›</span>
        </button>
      </div>
      <p v-else class="attention-empty">当前没有需要你手动处理的发布任务。</p>

      <details class="task-details">
        <summary>查看全部任务与筛选（{{ rows.length }}）</summary>

      <div class="filters">
        <label class="search-field">
          <span aria-hidden="true">⌕</span>
          <input v-model="searchText" type="search" placeholder="搜索商品名称或商品 ID" aria-label="搜索商品名称或商品 ID" />
        </label>
        <label class="filter-select">
          <span>账号</span>
          <select v-model="accountFilter" aria-label="按账号筛选">
            <option value="all">全部账号</option>
            <option v-for="account in accounts" :key="account.id" :value="String(account.id)">{{ account.name }}</option>
          </select>
        </label>
        <label class="filter-select">
          <span>状态</span>
          <select v-model="statusFilter" aria-label="按状态筛选">
            <option value="all">全部状态</option>
            <option value="awaiting_confirmation">待确认</option>
            <option value="scheduled">已排期</option>
            <option value="not_uploaded">待上传</option>
            <option value="uploading">上传中</option>
            <option value="uploaded_waiting_publish">待手动发布</option>
            <option value="submitting">发布提交中</option>
            <option value="submitted_review">审核中</option>
            <option value="needs_review">需核查</option>
            <option value="submission_uncertain">发布结果待核查</option>
            <option value="published">已发布</option>
            <option value="cancelled">已取消</option>
          </select>
        </label>
        <label class="filter-select date-filter">
          <span>计划日期</span>
          <input v-model="dateFrom" type="date" aria-label="计划日期起始" />
        </label>
        <span class="date-separator">至</span>
        <label class="filter-select date-filter date-to">
          <span class="visually-hidden">计划日期结束</span>
          <input v-model="dateTo" type="date" aria-label="计划日期结束" />
        </label>
        <button v-if="dateFrom || dateTo || searchText || accountFilter !== 'all' || statusFilter !== 'all'" type="button" class="clear-filters" @click="dateFrom = ''; dateTo = ''; searchText = ''; accountFilter = 'all'; statusFilter = 'all'">清除筛选</button>
      </div>

      <div class="table-meta"><span>共 <b>{{ filteredRows.length }}</b> 条任务</span><span v-if="loading" class="loading-copy">正在读取本地队列…</span></div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>账号</th><th>商品 / 视频</th><th>队列状态</th><th>平台状态</th><th>计划时间</th><th>最近更新</th><th class="action-column">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in visibleRows" :key="row.id">
              <td><span class="account-name"><i :class="{ 'account-a': row.account_id === 1, 'account-c': row.account_id === 3 }"></i>{{ row.account_name }}</span></td>
              <td class="product-cell">
                <b :title="row.product_name || row.title">{{ row.product_name || row.title || '未命名商品' }}</b>
                <small :title="row.product_id">商品 ID：{{ row.product_id || '—' }}<template v-if="row.video_path"> · {{ fileName(row.video_path) }}</template></small>
                <small v-if="row.platform_error" class="row-error" :title="row.platform_error">{{ row.platform_error }}</small>
              </td>
              <td><span class="queue-pill" :class="{ 'queue-pending': row.status === 'awaiting_confirmation', 'queue-cancelled': row.status === 'cancelled' }">{{ queueStatus(row) }}</span></td>
              <td><span class="platform-pill" :class="platformTone(row.platform_status)"><i></i>{{ platformStatus(row.platform_status) }}</span></td>
              <td class="time-cell">{{ formatTime(row.schedule_at) }}</td>
              <td class="time-cell">{{ formatTime(row.updated_at) }}</td>
              <td class="action-column"><button type="button" class="row-action" @click="emit('navigate', '发布队列')">查看任务</button></td>
            </tr>
            <tr v-if="!loading && !loadError && filteredRows.length === 0">
          <td colspan="7" class="empty-state">
                <span class="empty-icon">▤</span>
                <b>{{ rows.length ? '没有符合筛选条件的任务' : '还没有发布任务' }}</b>
                <span>{{ rows.length ? '调整账号、状态或搜索条件后再试。' : '完成选品并生成 9:16 视频后，任务会自动进入发布队列。' }}</span>
                <button v-if="!rows.length" type="button" class="button button-primary" @click="emit('navigate', '选品设置')">去选品</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <footer class="table-footer">
        <span>列表按需处理顺序排列；“需核查”任务请先在创作者中心确认，避免重复上传。</span>
        <div class="table-pagination">
          <span>{{ firstVisibleRow }}–{{ lastVisibleRow }} / {{ filteredRows.length }}</span>
          <button type="button" aria-label="上一页" :disabled="currentPage <= 1" @click="changePage(-1)">‹</button>
          <b>{{ currentPage }}</b><span class="page-total">/ {{ pageCount }}</span>
          <button type="button" aria-label="下一页" :disabled="currentPage >= pageCount" @click="changePage(1)">›</button>
          <button type="button" class="button button-quiet refresh-list" :disabled="loading" @click="refresh">更新列表</button>
        </div>
      </footer>
      </details>
    </section>
  </section>
</template>

<style scoped>
.dashboard { display:flex; flex-direction:column; gap:16px; min-width:0; min-height:0; width:100%; overflow:auto; padding:2px 2px 10px; color:#20283a; }
.dashboard-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; min-height:70px; }
.eyebrow { color:#78839a; font-size:11px; font-weight:700; letter-spacing:.11em; text-transform:uppercase; }
h1,h2,p { margin:0; }
.dashboard-heading h1 { margin-top:2px; font-size:22px; line-height:1.35; font-weight:700; letter-spacing:-.02em; }
.dashboard-heading p { margin-top:4px; color:#778198; font-size:12px; }
.heading-actions { display:flex; align-items:center; gap:9px; }
.button { display:inline-flex; align-items:center; justify-content:center; gap:7px; min-height:34px; padding:0 13px; border:1px solid #dce2eb; border-radius:6px; background:#fff; color:#37435a; font-size:12px; font-weight:600; transition:background .15s,border-color .15s,transform .15s; }
.button:hover:not(:disabled) { border-color:#aebdda; background:#f8faff; }
.button:disabled { cursor:wait; opacity:.7; }
.button-primary { border-color:#397af5; background:#397af5; color:#fff; box-shadow:0 3px 8px rgba(57,122,245,.16); }
.button-primary:hover:not(:disabled) { border-color:#2768df; background:#2768df; }
.button-quiet { background:#fff; }
.refresh-mark { display:inline-block; font-size:17px; line-height:1; }
.spinning { animation:spin .9s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.load-error { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 13px; border:1px solid #f0c8c5; border-radius:6px; background:#fff4f3; color:#ad3f3a; font-size:12px; }
.load-error button,.clear-filters { border:0; background:none; color:#397af5; font:inherit; cursor:pointer; }
.metric-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:11px; }
.metric-card,.workflow-card,.task-card { border:1px solid var(--border); border-radius:var(--radius); background:#fff; box-shadow:var(--shadow-card); }
.metric-card { min-height:112px; padding:13px 15px 12px; border-top:3px solid #a9c4f7; }
.metric-card:nth-child(2) { border-top-color:#e9c181; }.metric-card:nth-child(3) { border-top-color:#8ac8d6; }.metric-card:nth-child(4) { border-top-color:#e6a29e; }.metric-card:nth-child(5) { border-top-color:#8fc9ae; }
.metric-top { display:flex; justify-content:space-between; align-items:center; }
.metric-label { color:#68738a; font-size:12px; font-weight:600; }
.metric-mark { display:grid; place-items:center; width:25px; height:25px; border-radius:7px; font-size:11px; font-weight:700; }
.metric-mark.blue { color:#3477ee; background:#edf4ff; }.metric-mark.amber { color:#c88325; background:#fff5e8; }.metric-mark.cyan { color:#198b9b; background:#eaf9fb; }.metric-mark.red { color:#d64c47; background:#fff0ef; }.metric-mark.green { color:#288361; background:#eaf7f1; }
.metric-value { margin-top:2px; color:#26314a; font-size:26px; font-weight:700; line-height:1.25; font-variant-numeric:tabular-nums; }
.metric-note { margin-top:4px; color:#929bad; font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.workflow-card { padding:14px 16px 15px; }
.section-heading,.task-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; }
.section-heading h2,.task-heading h2 { color:#26314a; font-size:14px; font-weight:700; }
.section-heading>div:first-child span,.task-heading>div:first-child span { margin-left:9px; color:#97a0b0; font-size:11px; }
.account-scope { display:flex; align-items:center; gap:7px; color:#778198; font-size:11px; }
.account-scope i { width:7px; height:7px; border-radius:50%; background:#55ae83; }
.workflow-steps { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:12px; border:1px solid #edf0f5; border-radius:7px; background:#fbfcfe; }
.workflow-step { display:flex; align-items:center; min-width:0; gap:10px; padding:10px 14px; text-align:left; }
.workflow-step+.workflow-step { border-left:1px solid #e9edf3; }
.workflow-step:hover { background:#f5f8fd; }
.step-icon { display:grid; place-items:center; width:31px; height:31px; flex-shrink:0; border-radius:8px; font-size:10px; font-weight:700; }
.step-blue { color:#3477ee; background:#edf4ff; }.step-purple { color:#7c67d6; background:#f1efff; }.step-green { color:#288361; background:#eaf7f1; }
.step-copy { display:flex; flex-direction:column; min-width:0; gap:2px; }.step-copy b { color:#35405a; font-size:11px; }.step-copy small { color:#909bad; font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.step-arrow { margin-left:auto; color:#b1bac8; font-size:20px; line-height:1; }
.task-card { min-height:0; padding:14px 16px 0; display:flex; flex-direction:column; }
.task-heading { padding-bottom:12px; }
.attention-list { display:flex; flex-direction:column; gap:7px; padding-bottom:13px; }
.attention-item { display:flex; align-items:center; justify-content:space-between; gap:12px; width:100%; padding:10px 12px; border:1px solid #edf0f5; border-radius:7px; background:#fbfcff; text-align:left; color:#35415a; }
.attention-item:hover { background:#f2f6ff; border-color:#c9d9f6; }
.attention-item span:first-child { display:flex; flex-direction:column; min-width:0; gap:4px; }
.attention-item strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; }
.attention-item small { color:#a24d41; font-size:11px; }
.attention-arrow { flex-shrink:0; color:#397af5; font-size:11px; }
.attention-empty { margin:0 0 13px; padding:13px; border-radius:7px; background:#f1f8f4; color:#3f7557; font-size:12px; }
.task-details { border-top:1px solid #edf0f5; padding:11px 0 14px; }
.task-details summary { width:max-content; color:#397af5; cursor:pointer; font-size:12px; font-weight:600; }
.task-details[open] summary { margin-bottom:12px; }
.text-action { border:0; background:none; color:#397af5; font-size:11px; font-weight:600; cursor:pointer; }
.text-action span { padding-left:3px; font-size:14px; }
.filters { display:flex; align-items:end; gap:9px; flex-wrap:wrap; padding:10px; border:1px solid #e9edf3; border-radius:7px; background:#f8f9fb; }
.search-field { display:flex; align-items:center; gap:7px; min-width:215px; height:33px; padding:0 9px; border:1px solid #dfe4ec; border-radius:5px; background:#fff; color:#8a95a8; }
.search-field>span { font-size:20px; line-height:1; transform:rotate(-15deg); }
.search-field input { width:100%; min-width:0; border:0; outline:0; background:transparent; color:#313c53; font:inherit; font-size:11px; }
.search-field input::placeholder { color:#a3aaba; }
.filter-select { display:flex; align-items:center; gap:6px; height:33px; padding:0 8px; border:1px solid #dfe4ec; border-radius:5px; background:#fff; color:#8490a3; font-size:10px; }
.filter-select select,.filter-select input { border:0; outline:0; background:transparent; color:#3a465e; font:inherit; font-size:11px; }
.filter-select select { min-width:84px; cursor:pointer; }
.date-filter { gap:4px; }.date-filter input { width:116px; }.date-to { margin-left:-8px; }
.date-separator { align-self:center; margin:0 -5px; color:#9aa3b1; font-size:10px; }
.clear-filters { height:32px; padding:0 3px; font-size:10px; }
.table-meta { display:flex; justify-content:space-between; padding:10px 2px 7px; color:#8e98aa; font-size:10px; }
.table-meta b { color:#4c5870; }
.loading-copy { color:#397af5; }
.table-wrap { min-height:140px; overflow:auto; border:1px solid #e8ecf2; border-radius:6px; }
table { width:100%; min-width:920px; border-collapse:collapse; text-align:left; }
thead { position:sticky; top:0; z-index:1; background:#f6f8fb; }
th { height:34px; padding:0 10px; color:#7d879a; font-size:10px; font-weight:600; white-space:nowrap; }
td { height:55px; padding:7px 10px; border-top:1px solid #edf0f4; color:#48536a; font-size:10px; }
tbody tr:hover { background:#fafcff; }
.account-name { display:flex; align-items:center; gap:7px; white-space:nowrap; color:#46516a; font-size:10px; font-weight:600; }
.account-name i { width:7px; height:7px; border-radius:50%; background:#548ef1; }.account-name i.account-c { background:#8b76df; }
.product-cell { max-width:270px; }.product-cell b,.product-cell small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.product-cell b { max-width:255px; color:#35415a; font-size:11px; font-weight:600; }.product-cell small { max-width:260px; margin-top:2px; color:#9aa3b1; font-size:9px; }
.product-cell .row-error { color:#d1504a; }
.queue-pill,.platform-pill { display:inline-flex; align-items:center; gap:5px; white-space:nowrap; font-size:10px; }
.queue-pill { padding:3px 7px; border-radius:4px; background:#f0f3f8; color:#67738a; }.queue-pending { background:#fff5e7; color:#bb7c25; }.queue-cancelled { color:#98a0ad; background:#f3f4f6; }
.platform-pill { color:#778198; }.platform-pill i { width:6px; height:6px; border-radius:50%; background:#a9b1be; }.platform-pill.danger { color:#c84944; }.platform-pill.danger i { background:#dc5a55; }.platform-pill.success { color:#27825f; }.platform-pill.success i { background:#45a67c; }.platform-pill.info { color:#3574dc; }.platform-pill.info i { background:#4788ed; }.platform-pill.warning { color:#be7b22; }.platform-pill.warning i { background:#d59a42; }
.time-cell { color:#778198; font-size:10px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.action-column { position:sticky; right:0; z-index:2; text-align:right; background:#fff; box-shadow:-5px 0 8px -8px #63718a; }
thead .action-column { z-index:3; background:#f6f8fb; }
tbody tr:hover .action-column { background:#fafcff; }
.row-action { border:1px solid #dce6f8; border-radius:4px; background:#fff; color:#397af5; padding:4px 8px; font-size:10px; white-space:nowrap; }.row-action:hover { background:#f2f6ff; }
.empty-state { height:172px; text-align:center; color:#8b95a7; }.empty-state>* { display:block; margin:4px auto; }.empty-state b { color:#49556d; font-size:12px; }.empty-state>span:not(.empty-icon) { font-size:10px; }.empty-state .button { display:flex; width:max-content; margin-top:10px; }
.empty-icon { color:#b9c3d2; font-size:24px; }
.table-footer { display:flex; align-items:center; justify-content:space-between; gap:14px; min-height:48px; color:#929bad; font-size:10px; }
.table-pagination { display:flex; align-items:center; gap:6px; white-space:nowrap; }
.table-pagination button:not(.button) { display:grid; place-items:center; width:24px; height:24px; border:1px solid #e0e5ed; border-radius:4px; background:#fff; color:#68738a; font-size:16px; }.table-pagination button:disabled { cursor:default; color:#c0c6d0; background:#f7f8fa; }
.table-pagination b { display:grid; place-items:center; width:23px; height:23px; border-radius:4px; background:#397af5; color:#fff; font-size:10px; }
.page-total { margin-left:-5px; }
.table-footer .refresh-list { min-height:26px; margin-left:7px; padding:0 9px; font-size:10px; }
.visually-hidden { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
@media (max-width:1450px) { .metric-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }.filters { gap:7px; }.search-field { flex:1 1 220px; }.dashboard-heading { gap:10px; }.heading-actions { flex-wrap:wrap; justify-content:flex-end; } }
@media (max-width:760px) { .dashboard-heading { align-items:flex-start; flex-direction:column; }.heading-actions { width:100%; }.metric-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }.workflow-steps { grid-template-columns:1fr; }.workflow-step+.workflow-step { border-left:0; border-top:1px solid #e9edf3; }.section-heading,.task-heading { align-items:flex-start; }.account-scope { display:none; } }
</style>
