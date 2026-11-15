<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface PlanLogSummary {
  id: number
  account_id: number
  account_name?: string
  qianchuan_account_id: number
  qc_name?: string
  advertiser_id: string
  product_id: string
  product_name: string
  plan_name: string
  creation_source: string
  daily_budget_yuan: number
  commission_rate: number
  roi_target: number
  status: string
  remote_plan_id: string
  reason: string
  screenshot_path: string
  created_at: string
  updated_at: string
  event_count: number
  last_event_at?: string
  upload_summary?: UploadSummary
}
interface UploadSummary {
  total: number
  succeeded: number
  needsReview: number
  failed: number
  pending: number
  remoteMaterialIds?: string[]
  lastFailure?: string
}
interface PlanEvent {
  id: number
  event_type: string
  stage: string
  status: string
  message: string
  detail: string
  remote_plan_id: string
  screenshot_path: string
  supplement_id: number
  created_at: string
}
interface AccountOption { accountId: number; accountName: string }

const filters = ref({ accountId: '', status: '', startDate: '', endDate: '' })
const items = ref<PlanLogSummary[]>([])
const accountOptions = ref<AccountOption[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 25
const selectedId = ref(0)
const selectedJob = ref<PlanLogSummary | null>(null)
const events = ref<PlanEvent[]>([])
const loading = ref(false)
const detailLoading = ref(false)
const error = ref('')
const detailError = ref('')

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function statusLabel(status: string): string {
  return ({ ready: '待发布', submitting: '发布中', published: '已发布', blocked: '发布前停止', needs_review: '待核查' } as Record<string, string>)[status] ?? status
}
function statusTone(status: string): string {
  return status === 'published' ? 'success' : status === 'needs_review' || status === 'blocked' ? 'warning' : 'info'
}
function eventLabel(type: string): string {
  return ({
    PLAN_DRAFT_CREATED: '生成待发布计划',
    PLAN_SUBMIT_STARTED: '开始提交千川',
    PLAN_PUBLISHED: '千川计划发布成功',
    PLAN_SUBMISSION_UNCERTAIN: '提交结果待核查',
    PLAN_BLOCKED: '发布前停止',
    PLAN_RECOVERED_TO_REVIEW: '中断后转待核查',
    PLAN_RETRY_QUEUED: '重新排队',
    PLAN_MANUALLY_RESOLVED: '人工核查结论',
    SUPPLEMENT_SUBMIT_STARTED: '开始追加素材',
    SUPPLEMENT_APPLIED: '追加素材成功',
    SUPPLEMENT_ALREADY_PRESENT: '素材已存在',
    SUPPLEMENT_WAITING: '追加重新排队',
    SUPPLEMENT_SUBMISSION_UNCERTAIN: '追加结果待核查',
    SUPPLEMENT_BLOCKED: '追加前停止'
  } as Record<string, string>)[type] ?? type
}
function eventTone(type: string, status: string): string {
  if (type.includes('PUBLISHED') || type.includes('APPLIED') || type.includes('ALREADY')) return 'success'
  if (type.includes('UNCERTAIN') || type.includes('BLOCKED') || status === 'needs_review') return 'warning'
  return 'info'
}
function format(value: string | undefined): string { return value ? value.replace('T', ' ') : '时间未知' }
function uploadLabel(summary?: UploadSummary): string {
  if (!summary) return '未读取'
  return `已上传 ${summary.succeeded}/${summary.total}，待核查 ${summary.needsReview}，失败 ${summary.failed}，待处理 ${summary.pending}`
}
function currentStage(): string {
  const stage = events.value[events.value.length - 1]?.stage
  return stage || '尚未记录'
}
function materialIds(summary?: UploadSummary): string {
  const ids = summary?.remoteMaterialIds ?? []
  return ids.length ? ids.join('、') : '尚未回读'
}
function parseRows(result: Record<string, unknown>): PlanLogSummary[] {
  return Array.isArray(result.items) ? result.items as PlanLogSummary[] : []
}
function checked(result: Record<string, unknown>): Record<string, unknown> {
  if (result.ok === false) throw new Error(String(result.error ?? '读取千川发布日志失败'))
  return result
}

async function loadDetails(id: number): Promise<void> {
  selectedId.value = id
  selectedJob.value = null
  events.value = []
  detailError.value = ''
  if (!id) return
  detailLoading.value = true
  try {
    const result = checked(await window.api.qianchuan.planLogs({ id }))
    selectedJob.value = result.job as PlanLogSummary
    events.value = Array.isArray(result.events) ? result.events as PlanEvent[] : []
  } catch (e) { detailError.value = e instanceof Error ? e.message : String(e) }
  finally { detailLoading.value = false }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = checked(await window.api.qianchuan.planLogs({
      accountId: filters.value.accountId ? Number(filters.value.accountId) : undefined,
      status: filters.value.status || undefined,
      startDate: filters.value.startDate || undefined,
      endDate: filters.value.endDate || undefined,
      offset: (page.value - 1) * pageSize,
      limit: pageSize
    }))
    items.value = parseRows(result)
    accountOptions.value = Array.isArray(result.accountOptions) ? result.accountOptions as AccountOption[] : []
    total.value = Number(result.total ?? items.value.length)
    const current = items.value.find(row => row.id === selectedId.value)
    await loadDetails(current?.id ?? items.value[0]?.id ?? 0)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    items.value = []
    total.value = 0
    await loadDetails(0)
  } finally { loading.value = false }
}
function search(): void { page.value = 1; void load() }
function reset(): void { filters.value = { accountId: '', status: '', startDate: '', endDate: '' }; page.value = 1; void load() }
function move(next: number): void { if (next < 1 || next > pageCount.value || next === page.value) return; page.value = next; void load() }
async function openScreenshot(path: string): Promise<void> {
  if (!path) return
  const result = await window.api.publish.openScreenshot(path)
  if (!result.ok) error.value = result.error ?? '无法打开诊断截图'
}

onMounted(() => { void load() })
</script>

<template>
  <section class="logs-page">
    <header class="page-header">
      <div><h1>千川发布日志</h1><p>按计划查看素材准备、预算确认、千川提交、平台回执和人工核查记录。此页面只读，不会重复发布。</p></div>
      <button class="btn" type="button" :disabled="loading" @click="load">刷新</button>
    </header>
    <section class="filters-card">
      <label>账号<select v-model="filters.accountId" :disabled="loading"><option value="">全部账号</option><option v-for="account in accountOptions" :key="account.accountId" :value="String(account.accountId)">{{ account.accountName || '账号' }}（ID {{ account.accountId }}）</option></select></label>
      <label>计划状态<select v-model="filters.status" :disabled="loading"><option value="">全部状态</option><option value="ready">待发布</option><option value="submitting">发布中</option><option value="published">已发布</option><option value="needs_review">待核查</option><option value="blocked">发布前停止</option></select></label>
      <label>开始日期<input v-model="filters.startDate" type="date" :disabled="loading"></label>
      <label>结束日期<input v-model="filters.endDate" type="date" :disabled="loading"></label>
      <div class="filter-actions"><button class="btn primary" type="button" :disabled="loading" @click="search">筛选</button><button class="btn" type="button" :disabled="loading" @click="reset">重置</button></div>
    </section>
    <p v-if="error" class="error-banner">{{ error }}</p>
    <div class="logs-layout">
      <section class="plan-panel">
        <div class="panel-title"><strong>历史计划</strong><span>{{ total }} 条</span></div>
        <div v-if="loading" class="panel-message">正在读取千川发布日志…</div>
        <div v-else-if="!items.length" class="panel-message">还没有千川计划发布记录。生成待发布计划后，这里会自动记录。</div>
        <button v-for="item in items" :key="item.id" class="plan-item" :class="{ selected: selectedId === item.id }" type="button" @click="loadDetails(item.id)">
          <span class="plan-head"><b>{{ format(item.last_event_at || item.updated_at) }}</b><i class="state-pill" :class="'is-' + statusTone(item.status)">{{ statusLabel(item.status) }}</i></span>
          <strong>{{ item.product_name || '未填写商品名' }}</strong>
          <span>{{ item.account_name || `账号 ${item.account_id}` }} · 商品 ID {{ item.product_id }}</span>
          <small>{{ item.event_count || 0 }} 条过程记录 · 计划任务 #{{ item.id }}<template v-if="item.remote_plan_id"> · 千川计划 {{ item.remote_plan_id }}</template></small>
          <small>素材：{{ uploadLabel(item.upload_summary) }}</small>
        </button>
        <div v-if="total > pageSize" class="pagination"><button class="btn" :disabled="loading || page <= 1" @click="move(page - 1)">上一页</button><span>第 {{ page }} / {{ pageCount }} 页</span><button class="btn" :disabled="loading || page >= pageCount" @click="move(page + 1)">下一页</button></div>
      </section>
      <section class="detail-panel">
        <div v-if="detailLoading" class="panel-message">正在读取计划详情…</div>
        <p v-else-if="detailError" class="error-banner">{{ detailError }}</p>
        <div v-else-if="selectedJob" class="plan-detail">
          <header class="detail-header"><div><div class="detail-title"><h2>计划发布详情</h2><i class="state-pill" :class="'is-' + statusTone(selectedJob.status)">{{ statusLabel(selectedJob.status) }}</i></div><p>{{ selectedJob.plan_name }} · 任务 #{{ selectedJob.id }}</p><small>创建：{{ format(selectedJob.created_at) }}　最近更新：{{ format(selectedJob.updated_at) }}</small></div></header>
          <section class="summary-card"><div><b>账号</b><span>{{ selectedJob.account_name || selectedJob.account_id }}</span></div><div><b>商品 ID</b><span>{{ selectedJob.product_id }}</span></div><div><b>广告主 ID</b><span>{{ selectedJob.advertiser_id || '未记录' }}</span></div><div><b>日预算</b><span>{{ selectedJob.daily_budget_yuan }} 元</span></div><div><b>保本 ROI</b><span>{{ Number(selectedJob.roi_target || 0).toFixed(2) }}</span></div><div><b>千川计划 ID</b><span>{{ selectedJob.remote_plan_id || '尚未确认' }}</span></div><div class="wide"><b>素材上传结果</b><span>{{ uploadLabel(selectedJob.upload_summary) }}</span></div><div class="wide"><b>远端素材 ID</b><span class="material-ids">{{ materialIds(selectedJob.upload_summary) }}</span></div><div><b>当前阶段</b><span>{{ currentStage() }}</span></div></section>
          <p v-if="selectedJob.upload_summary?.lastFailure" class="reason">素材待核查：{{ selectedJob.upload_summary.lastFailure }}</p>
          <p v-if="selectedJob.reason" class="reason">当前说明：{{ selectedJob.reason }}</p>
          <section class="timeline"><div class="timeline-title"><h3>发布过程记录</h3><span>{{ events.length }} 条</span></div><p v-if="!events.length" class="panel-message">该计划没有可显示的过程记录。</p><article v-for="event in events" :key="event.id" class="event"><div class="event-head"><strong>{{ eventLabel(event.event_type) }}</strong><time>{{ format(event.created_at) }}</time></div><div class="event-meta"><i class="log-level" :class="'is-' + eventTone(event.event_type, event.status)">{{ event.status || '记录' }}</i><span>阶段：{{ event.stage || '未记录' }}</span><span v-if="event.supplement_id">追加任务 #{{ event.supplement_id }}</span></div><p :class="'message is-' + eventTone(event.event_type, event.status)">{{ event.message }}</p><small v-if="event.remote_plan_id">千川计划 ID：{{ event.remote_plan_id }}</small><details v-if="event.detail || event.screenshot_path"><summary>查看技术细节</summary><pre v-if="event.detail">{{ event.detail }}</pre><button v-if="event.screenshot_path" class="link-button" type="button" @click="openScreenshot(event.screenshot_path)">打开诊断截图</button></details></article></section>
        </div>
        <div v-else class="panel-message">选择左侧计划查看发布过程。</div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.logs-page { display:flex; flex:1; flex-direction:column; min-width:0; min-height:0; gap:12px; overflow:auto; color:var(--text); }
.page-header { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
.page-header h1 { margin:0; font-size:20px; }.page-header p { margin:5px 0 0; color:var(--text-muted); font-size:12px; }
.filters-card { display:grid; grid-template-columns:repeat(4,minmax(130px,1fr)) auto; align-items:end; gap:10px; padding:12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-panel); box-shadow:var(--shadow-card); }
.filters-card label { display:flex; flex-direction:column; gap:5px; color:var(--text-muted); font-size:11px; }.filters-card input,.filters-card select { width:100%; min-width:0; height:32px; padding:0 8px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-app); color:var(--text); }.filter-actions { display:flex; gap:7px; }
.btn { min-height:32px; padding:0 12px; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-panel); color:var(--text); cursor:pointer; }.btn.primary { border-color:var(--brand); background:var(--brand); color:#fff; }.btn:disabled { cursor:default; opacity:.5; }
.logs-layout { display:grid; flex:1; grid-template-columns:minmax(280px,360px) minmax(0,1fr); gap:12px; min-height:420px; }.plan-panel,.detail-panel { min-width:0; border:1px solid var(--border); border-radius:var(--radius); background:var(--bg-panel); box-shadow:var(--shadow-card); }.plan-panel { display:flex; flex-direction:column; align-self:start; max-height:none; overflow:visible; }.panel-title { display:flex; justify-content:space-between; padding:12px; border-bottom:1px solid var(--border); font-size:13px; }.panel-title span,.timeline-title span { color:var(--text-muted); font-size:11px; }
.plan-item { display:flex; flex-direction:column; align-items:stretch; gap:5px; padding:11px 12px; border:0; border-bottom:1px solid var(--border); background:transparent; color:var(--text); text-align:left; cursor:pointer; }.plan-item:hover,.plan-item.selected { background:var(--brand-soft); }.plan-item.selected { box-shadow:inset 3px 0 var(--brand); }.plan-item strong { overflow-wrap:anywhere; font-size:12px; }.plan-item span,.plan-item small { color:var(--text-muted); font-size:10px; }.plan-head,.event-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }.plan-head b,.event-head strong { min-width:0; font-size:11px; }.state-pill,.log-level { display:inline-flex; flex:0 0 auto; align-items:center; min-height:19px; padding:1px 7px; border:0; border-radius:10px; font-size:10px; font-style:normal; font-weight:500; }.state-pill.is-success,.log-level.is-success { background:#e7f6ee; color:#087443; }.state-pill.is-info,.log-level.is-info { background:#eaf1ff; color:#2458a6; }.state-pill.is-warning,.log-level.is-warning { background:#fff4dc; color:#9a6700; }.pagination { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:10px; }.pagination span { color:var(--text-muted); font-size:11px; }
.detail-panel { min-height:420px; overflow:visible; }.plan-detail { padding:14px; }.detail-header { padding-bottom:12px; border-bottom:1px solid var(--border); }.detail-title { display:flex; align-items:center; gap:9px; }.detail-title h2 { margin:0; font-size:16px; }.detail-header p { margin:6px 0; overflow-wrap:anywhere; font:11px Consolas,monospace; color:var(--text-muted); }.detail-header small { color:var(--text-muted); font-size:10px; }.summary-card { display:grid; grid-template-columns:repeat(3,minmax(120px,1fr)); gap:8px; padding:12px 0; border-bottom:1px solid var(--border); }.summary-card div { display:flex; flex-direction:column; gap:3px; padding:8px; border-radius:var(--radius); background:var(--bg-app); }.summary-card .wide { grid-column:1/-1; }.summary-card b { color:var(--text-muted); font-size:10px; }.summary-card span { overflow-wrap:anywhere; font-size:11px; }.material-ids { font:10px/1.4 Consolas,monospace; }.reason { margin:10px 0; padding:8px; border-radius:var(--radius); color:#8a5b00; background:#fff4dc; font-size:11px; line-height:1.5; }.timeline { padding-top:12px; }.timeline-title { display:flex; align-items:baseline; justify-content:space-between; padding-bottom:7px; border-bottom:1px solid var(--border); }.timeline-title h3 { margin:0; font-size:13px; }.event { padding:10px 0; border-bottom:1px solid var(--border); }.event-head time,.event-meta,.event small { color:var(--text-muted); font-size:10px; }.event-meta { display:flex; align-items:center; flex-wrap:wrap; gap:8px; margin-top:6px; }.message { margin:5px 0; font-size:11px; line-height:1.5; overflow-wrap:anywhere; }.message.is-success { color:#087443; }.message.is-warning { color:#8a5b00; }.message.is-info { color:var(--text); }.event details { margin-top:5px; color:var(--text-muted); font-size:10px; }.event summary { cursor:pointer; }.event pre { max-height:200px; margin:6px 0; padding:8px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; border-radius:var(--radius); background:var(--bg-app); font:10px/1.45 Consolas,monospace; }.link-button { padding:0; border:0; background:none; color:var(--brand); cursor:pointer; font-size:10px; }.panel-message,.error-banner { padding:20px 12px; color:var(--text-muted); text-align:center; font-size:12px; }.error-banner { margin:0; padding:12px; border:1px solid var(--border); border-radius:var(--radius); background:#fdecea; color:#a33125; }
@media (max-width:1350px) { .filters-card { grid-template-columns:repeat(2,minmax(130px,1fr)); }.filter-actions { grid-column:1/-1; }.logs-layout { grid-template-columns:1fr; }.plan-panel { max-height:none; overflow:visible; }.detail-panel { min-height:300px; } }.@media (max-width:760px) { .summary-card { grid-template-columns:repeat(2,minmax(120px,1fr)); }.summary-card .wide { grid-column:1/-1; }.logs-layout { grid-template-columns:1fr; } }
</style>
