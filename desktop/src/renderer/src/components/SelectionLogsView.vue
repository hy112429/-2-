<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { toUserSelectionError, toUserSelectionMessage } from '../utils/selection_error_ux'

interface SelectionBatchSummary {
  batchId: string
  state: string
  createdAt: string
  updatedAt: string
  accountCount: number
  logCount: number
  accountNames: string
}

interface SelectionAccountOption {
  accountId: number
  accountName: string
}

interface SelectionBatchAccount {
  accountId: number
  accountName: string
  sequence: number
  state: string
  stage: string
  error: string
  updatedAt: string
}

interface SelectionRunLog {
  id: number
  batchId: string
  accountId: number
  accountName: string
  productId: string
  productTitle: string
  stage: string
  code: string
  message: string
  detail: string
  createdAt: string
}

function userMessage(value: unknown): string {
  return toUserSelectionMessage(value)
}

function diagnosticMessage(value: unknown): string {
  return toUserSelectionError(value).diagnostic ?? ''
}

interface BatchListResult {
  ok: boolean
  items: SelectionBatchSummary[]
  accountOptions: SelectionAccountOption[]
  total: number
  limit: number
  offset: number
  error?: string
}

interface BatchDetailsResult {
  ok: boolean
  batch: Pick<SelectionBatchSummary, 'batchId' | 'state' | 'createdAt' | 'updatedAt'>
  accounts: SelectionBatchAccount[]
  runLogs: SelectionRunLog[]
  error?: string
}

type LogLevel = 'success' | 'info' | 'warning' | 'error'

const pageSize = 25
const filters = ref({ accountId: '', state: '', startDate: '', endDate: '' })
const currentPage = ref(1)
const batches = ref<SelectionBatchSummary[]>([])
const accountOptions = ref<SelectionAccountOption[]>([])
const total = ref(0)
const selectedBatchId = ref('')
const selectedBatch = ref<BatchDetailsResult['batch'] | null>(null)
const batchAccounts = ref<SelectionBatchAccount[]>([])
const runLogs = ref<SelectionRunLog[]>([])
const databaseAvailable = ref<boolean | null>(null)
const listLoading = ref(false)
const detailsLoading = ref(false)
const pageError = ref('')
const detailsError = ref('')
const logAccountId = ref('')
let databasePath = ''
let stopEngineEvents: (() => void) | undefined
let detailsRequest = 0

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const visibleLogs = computed(() => {
  if (!logAccountId.value) return runLogs.value
  const accountId = Number(logAccountId.value)
  // 批次级事件（账号 ID 0）在按账号查看时仍保留，方便理解账号处理前后的批次状态。
  return runLogs.value.filter((entry) => entry.accountId === 0 || entry.accountId === accountId)
})

function runRecovery(args: string[]): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let taskId = ''
    let settled = false
    const queuedEvents: EngineEventPayload[] = []
    let unsubscribe = (): void => undefined
    let timer = 0
    const finish = (error?: Error, value?: Record<string, unknown>): void => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      unsubscribe()
      if (error) reject(error)
      else resolve(value ?? {})
    }
    const accept = (event: EngineEventPayload): void => {
      if (event.taskId !== taskId) return
      if (event.type === 'error') {
        finish(new Error(event.message ?? '读取选品日志失败'))
      } else if (event.type === 'result') {
        const result = (event.data ?? {}) as Record<string, unknown>
        if (result.ok === false) finish(new Error(String(result.error ?? '读取选品日志失败')))
        else finish(undefined, result)
      }
    }
    unsubscribe = window.api.engine.onEvent((event) => {
      if (!taskId) queuedEvents.push(event)
      else accept(event)
    })
    timer = window.setTimeout(() => finish(new Error('读取选品日志超时')), 30_000)
    void window.api.engine.run({
      script: 'selection_recovery.py',
      args: ['--db', databasePath, ...args]
    }).then((reply) => {
      if (!reply.taskId) {
        finish(new Error(reply.error ?? '无法启动选品日志查询'))
        return
      }
      taskId = reply.taskId
      queuedEvents.splice(0).forEach(accept)
    }).catch((error: unknown) => {
      finish(error instanceof Error ? error : new Error(String(error)))
    })
  })
}

function stateLabel(state: string): string {
  const labels: Record<string, string> = {
    in_progress: '处理中',
    needs_retry: '待恢复',
    completed: '已完成',
    abandoned: '已结束',
    queued: '等待处理',
    running: '处理中',
    failed: '失败',
    partial: '部分完成',
    skipped: '已跳过'
  }
  return (labels[state] ?? state) || '未知'
}

function stateTone(state: string): string {
  if (state === 'completed') return 'success'
  if (state === 'needs_retry' || state === 'failed' || state === 'abandoned') return 'warning'
  return 'info'
}

function logLevel(entry: SelectionRunLog): LogLevel {
  const code = String(entry.code ?? '')
  if (code.startsWith('SUCCESS_')) return 'success'
  if (code.startsWith('INFO_')) return 'info'
  if (code.startsWith('WARNING_') || [
    'PRODUCT_ANALYSIS_FILTERED', 'PRODUCT_SKIPPED', 'CATEGORY_QUERY_SKIPPED',
    'PRIORITY_SOURCE_READ_SKIPPED', 'DOUYIN_PRODUCT_ALREADY_QUEUED'
  ].includes(code)) return 'warning'
  return 'error'
}

function logLevelLabel(entry: SelectionRunLog): string {
  const level = logLevel(entry)
  return level === 'success' ? '成功' : level === 'info' ? '记录' : level === 'warning' ? '跳过 / 提醒' : '失败'
}

function logActionLabel(entry: SelectionRunLog): string {
  const labels: Record<string, string> = {
    INFO_BATCH_STARTED: '批次开始',
    INFO_BATCH_RESUMED: '批次恢复',
    SUCCESS_BATCH_COMPLETED: '批次完成',
    ERROR_BATCH_FAILED: '批次失败结束',
    WARNING_BATCH_NEEDS_RETRY: '批次待恢复',
    INFO_ACCOUNT_STARTED: '账号开始处理',
    SUCCESS_ACCOUNT_COMPLETED: '账号处理完成',
    WARNING_ACCOUNT_PARTIAL: '账号部分完成',
    SUCCESS_PRODUCT_SELECTED: '候选初筛通过',
    INFO_CANDIDATE_POOL_READY: '补位候选已就绪',
    SUCCESS_PRODUCT_CART_ADDED: '加车成功',
    SUCCESS_PRIORITY_SOURCE_READ: '优先来源读取成功',
    INFO_PRIORITY_SOURCE_READ: '优先来源读取记录',
    INFO_PRIORITY_SOURCE_EMPTY: '优先来源无商品',
    INFO_PRIORITY_SOURCE_NO_MATCH: '优先来源未匹配当前类目',
    PRIORITY_SOURCE_READ_SKIPPED: '优先来源已跳过',
    WARNING_PRODUCT_ANALYSIS_FILTERED: '数据筛选跳过',
    PRODUCT_ANALYSIS_FILTERED: '数据筛选跳过',
    WARNING_PRODUCT_SKIPPED: '商品已跳过',
    PRODUCT_SKIPPED: '商品已跳过',
    DOUYIN_PRODUCT_ALREADY_QUEUED: '重复商品已拦截',
    CATEGORY_QUERY_SKIPPED: '失败类目已跳过',
    CART_NOT_CONFIRMED: '加车未确认',
    DETAIL_OR_ASSET_FAILED: '详情或素材处理失败',
    PRODUCT_STEP_FAILED: '商品处理失败',
    AUTO_PUBLISH_FAILED: '自动发布失败',
    AUTO_PUBLISH_NOT_STARTED: '自动发布未启动',
    PUBLISH_QUEUE_FAILED: '发布队列写入失败',
    IMAGE_TO_VIDEO_FAILED: '图转视频失败',
    DETAIL_IMAGE_RECOVERY_FAILED: '详情图恢复失败',
    DETAIL_IMAGES_MISSING: '详情图校验失败',
    ACCOUNT_STAGE_FAILED: '账号阶段失败',
    ACCOUNT_TASK_FAILED: '账号任务失败'
  }
  return labels[entry.code] ?? (logLevel(entry) === 'error' ? `${entry.stage}失败` : entry.stage)
}

function formatDateTime(value: string): string {
  return value ? value.replace('T', ' ') : '时间未知'
}

async function loadBatchDetails(batchId: string): Promise<void> {
  const requestId = ++detailsRequest
  selectedBatchId.value = batchId
  detailsError.value = ''
  selectedBatch.value = null
  batchAccounts.value = []
  runLogs.value = []
  logAccountId.value = ''
  if (!batchId) return
  detailsLoading.value = true
  try {
    const result = await runRecovery(['batch', 'logs', '--batch-id', batchId]) as unknown as BatchDetailsResult
    if (requestId !== detailsRequest) return
    selectedBatch.value = result.batch
    batchAccounts.value = result.accounts ?? []
    runLogs.value = result.runLogs ?? []
  } catch (error) {
    if (requestId === detailsRequest) detailsError.value = error instanceof Error ? error.message : String(error)
  } finally {
    if (requestId === detailsRequest) detailsLoading.value = false
  }
}

async function loadBatches(): Promise<void> {
  if (!databasePath || !databaseAvailable.value || listLoading.value) return
  listLoading.value = true
  pageError.value = ''
  const args = ['batch', 'list', '--limit', String(pageSize), '--offset', String((currentPage.value - 1) * pageSize)]
  if (filters.value.accountId) args.push('--account-id', filters.value.accountId)
  if (filters.value.state) args.push('--state', filters.value.state)
  if (filters.value.startDate) args.push('--start-date', filters.value.startDate)
  if (filters.value.endDate) args.push('--end-date', filters.value.endDate)
  try {
    const result = await runRecovery(args) as unknown as BatchListResult
    batches.value = result.items ?? []
    accountOptions.value = result.accountOptions ?? []
    total.value = Number(result.total ?? 0)
    const stillVisible = batches.value.some((batch) => batch.batchId === selectedBatchId.value)
    if (!stillVisible) await loadBatchDetails(batches.value[0]?.batchId ?? '')
    else if (selectedBatchId.value) await loadBatchDetails(selectedBatchId.value)
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : String(error)
    batches.value = []
    total.value = 0
    await loadBatchDetails('')
  } finally {
    listLoading.value = false
  }
}

function search(): void {
  currentPage.value = 1
  void loadBatches()
}

function resetFilters(): void {
  filters.value = { accountId: '', state: '', startDate: '', endDate: '' }
  currentPage.value = 1
  void loadBatches()
}

function changePage(nextPage: number): void {
  if (nextPage < 1 || nextPage > pageCount.value || nextPage === currentPage.value) return
  currentPage.value = nextPage
  void loadBatches()
}

onMounted(async () => {
  try {
    const paths = await window.api.appPaths.get()
    databasePath = paths.databasePath
    databaseAvailable.value = paths.databaseExists
    if (databaseAvailable.value) await loadBatches()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : String(error)
  }
})

onUnmounted(() => {
  detailsRequest += 1
})
</script>

<template>
  <section class="logs-page">
    <header class="page-header">
      <div>
        <h1>选品日志</h1>
        <p>按批次查看本机保存的选品过程、账号状态和技术原因。此页面只读，不会恢复或更改任务。</p>
      </div>
      <button class="btn" type="button" :disabled="listLoading || !databaseAvailable" @click="loadBatches">刷新</button>
    </header>

    <section class="filters-card">
      <label>账号
        <select v-model="filters.accountId" :disabled="listLoading">
          <option value="">全部账号</option>
          <option v-for="account in accountOptions" :key="account.accountId" :value="String(account.accountId)">
            {{ account.accountName }}（ID {{ account.accountId }}）
          </option>
        </select>
      </label>
      <label>批次状态
        <select v-model="filters.state" :disabled="listLoading">
          <option value="">全部状态</option>
          <option value="in_progress">处理中</option>
          <option value="needs_retry">待恢复</option>
          <option value="completed">已完成</option>
          <option value="failed">失败结束</option>
          <option value="abandoned">已结束</option>
        </select>
      </label>
      <label>开始日期<input v-model="filters.startDate" type="date" :disabled="listLoading"></label>
      <label>结束日期<input v-model="filters.endDate" type="date" :disabled="listLoading"></label>
      <div class="filter-actions">
        <button class="btn primary" type="button" :disabled="listLoading" @click="search">筛选</button>
        <button class="btn" type="button" :disabled="listLoading" @click="resetFilters">重置</button>
      </div>
    </section>

    <p v-if="pageError" class="error-banner">{{ userMessage(pageError) }}</p>
    <details v-if="pageError && diagnosticMessage(pageError)"><summary>查看诊断详情</summary><pre>{{ diagnosticMessage(pageError) }}</pre></details>
    <p v-else-if="databaseAvailable === false" class="empty-state">本机还没有选品日志数据库。完成一次选品后，历史批次会显示在这里。</p>

    <div v-else class="logs-layout">
      <section class="batch-panel">
        <div class="panel-title"><strong>历史批次</strong><span>{{ total }} 个批次</span></div>
        <div v-if="listLoading" class="panel-message">正在读取历史批次…</div>
        <div v-else-if="!batches.length" class="panel-message">没有符合条件的选品批次</div>
        <button
          v-for="batch in batches"
          :key="batch.batchId"
          class="batch-item"
          :class="{ selected: selectedBatchId === batch.batchId }"
          type="button"
          @click="loadBatchDetails(batch.batchId)"
        >
          <span class="batch-item-head">
            <b>{{ formatDateTime(batch.createdAt) }}</b>
            <i class="state-pill" :class="'is-' + stateTone(batch.state)">{{ stateLabel(batch.state) }}</i>
          </span>
          <span class="batch-item-accounts">{{ batch.accountNames || '账号信息缺失' }}</span>
          <span class="batch-item-meta">{{ batch.accountCount }} 个账号 · {{ batch.logCount }} 条日志</span>
          <small class="batch-id">{{ batch.batchId }}</small>
        </button>
        <div v-if="total > pageSize" class="pagination">
          <button class="btn" type="button" :disabled="listLoading || currentPage <= 1" @click="changePage(currentPage - 1)">上一页</button>
          <span>第 {{ currentPage }} / {{ pageCount }} 页</span>
          <button class="btn" type="button" :disabled="listLoading || currentPage >= pageCount" @click="changePage(currentPage + 1)">下一页</button>
        </div>
      </section>

      <section class="detail-panel">
        <div v-if="detailsLoading" class="panel-message">正在读取批次详情…</div>
        <div v-else-if="detailsError">
          <p class="error-banner">{{ userMessage(detailsError) }}</p>
          <details v-if="diagnosticMessage(detailsError)"><summary>查看诊断详情</summary><pre>{{ diagnosticMessage(detailsError) }}</pre></details>
        </div>
        <div v-else-if="selectedBatch" class="batch-detail">
          <header class="detail-header">
            <div>
              <div class="detail-title"><h2>批次详情</h2><i class="state-pill" :class="'is-' + stateTone(selectedBatch.state)">{{ stateLabel(selectedBatch.state) }}</i></div>
              <p>{{ selectedBatch.batchId }}</p>
              <small>创建：{{ formatDateTime(selectedBatch.createdAt) }}　最近更新：{{ formatDateTime(selectedBatch.updatedAt) }}</small>
            </div>
          </header>

          <section class="accounts-section">
            <h3>账号处理情况</h3>
            <article v-for="account in batchAccounts" :key="account.accountId" class="account-row">
              <div class="account-row-head">
                <strong>{{ account.accountName }}（ID {{ account.accountId }}）</strong>
                <span class="state-pill" :class="'is-' + stateTone(account.state)">{{ stateLabel(account.state) }}</span>
              </div>
              <small>阶段：{{ account.stage || '未记录' }} · 更新时间：{{ formatDateTime(account.updatedAt) }}</small>
              <p v-if="account.error" class="account-error">{{ userMessage(account.error) }}</p>
              <details v-if="account.error && diagnosticMessage(account.error)"><summary>查看诊断详情</summary><pre>{{ diagnosticMessage(account.error) }}</pre></details>
            </article>
            <p v-if="!batchAccounts.length" class="panel-message">此批次没有账号处理记录。</p>
          </section>

          <section class="timeline-section">
            <div class="timeline-header">
              <div><h3>过程记录</h3><span>{{ visibleLogs.length }} 条</span></div>
              <label v-if="batchAccounts.length">账号
                <select v-model="logAccountId">
                  <option value="">全部账号</option>
                  <option v-for="account in batchAccounts" :key="account.accountId" :value="String(account.accountId)">
                    {{ account.accountName }}（ID {{ account.accountId }}）
                  </option>
                </select>
              </label>
            </div>
            <p v-if="!visibleLogs.length" class="panel-message">当前筛选下没有过程日志。旧批次可能没有保存成功事件。</p>
            <article v-for="entry in visibleLogs" :key="entry.id" class="log-entry">
              <div class="log-entry-head">
                <strong>{{ entry.accountId === 0 ? '批次' : entry.accountName }}<span v-if="entry.productTitle"> · {{ entry.productTitle }}</span></strong>
                <time>{{ formatDateTime(entry.createdAt) }}</time>
              </div>
              <div class="log-action">
                <i class="log-level" :class="'is-' + logLevel(entry)">{{ logLevelLabel(entry) }}</i>
                <b>{{ logActionLabel(entry) }}</b>
                <span>{{ entry.stage }}</span>
              </div>
              <p class="log-message" :class="'is-' + logLevel(entry)">{{ userMessage(entry.message) }}</p>
              <small v-if="entry.productId" class="product-id">商品 ID：{{ entry.productId }}</small>
              <details v-if="entry.detail || entry.code || diagnosticMessage(entry.message)">
                <summary>查看技术细节 · {{ entry.code }}</summary>
                <pre>{{ entry.detail || diagnosticMessage(entry.message) || '没有附加技术信息。' }}</pre>
              </details>
            </article>
          </section>
        </div>
        <div v-else class="panel-message">选择左侧批次查看账号状态和过程记录。</div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.logs-page { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; gap: 12px; overflow: auto; color: var(--text); }
.page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.page-header h1 { margin: 0; font-size: 20px; }
.page-header p { margin: 5px 0 0; color: var(--text-muted); font-size: 12px; }
.filters-card { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)) auto; align-items: end; gap: 10px; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.filters-card label, .timeline-header label { display: flex; flex-direction: column; gap: 5px; color: var(--text-muted); font-size: 11px; }
.filters-card input, .filters-card select, .timeline-header select { width: 100%; min-width: 0; height: 32px; padding: 0 8px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); color: var(--text); }
.filter-actions { display: flex; gap: 7px; }
.btn { min-height: 32px; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); color: var(--text); cursor: pointer; }
.btn.primary { border-color: var(--brand); background: var(--brand); color: #fff; }
.btn:disabled { cursor: default; opacity: .5; }
.logs-layout { display: grid; flex: 1; grid-template-columns: minmax(280px, 360px) minmax(0, 1fr); gap: 12px; min-height: 420px; }
.batch-panel, .detail-panel { min-width: 0; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.batch-panel { display: flex; flex-direction: column; align-self: start; max-height: 100%; overflow: auto; }
.panel-title { display: flex; justify-content: space-between; padding: 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
.panel-title span, .timeline-header > div > span { color: var(--text-muted); font-size: 11px; }
.batch-item { display: flex; flex-direction: column; align-items: stretch; gap: 5px; padding: 11px 12px; border-bottom: 1px solid var(--border); background: transparent; color: var(--text); text-align: left; cursor: pointer; }
.batch-item:hover, .batch-item.selected { background: var(--brand-soft); }
.batch-item.selected { box-shadow: inset 3px 0 var(--brand); }
.batch-item-head, .account-row-head, .log-entry-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.batch-item-head b, .log-entry-head strong { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
.batch-item-accounts { color: var(--text); font-size: 12px; }
.batch-item-meta, .batch-id, .account-row small, .log-entry time, .product-id { color: var(--text-muted); font-size: 10px; }
.batch-id { overflow-wrap: anywhere; }
.state-pill, .log-level { display: inline-flex; flex: 0 0 auto; align-items: center; min-height: 19px; padding: 1px 7px; border: 0; border-radius: 10px; font-size: 10px; font-style: normal; font-weight: 500; }
.state-pill.is-success, .log-level.is-success { background: #e7f6ee; color: #087443; }
.state-pill.is-info, .log-level.is-info { background: #eaf1ff; color: #2458a6; }
.state-pill.is-warning, .log-level.is-warning { background: #fff4dc; color: #9a6700; }
.log-level.is-error { background: #fdecea; color: #a33125; }
.pagination { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px; }
.pagination span { color: var(--text-muted); font-size: 11px; }
.pagination .btn { min-height: 28px; padding: 0 8px; font-size: 11px; }
.detail-panel { min-height: 420px; overflow: auto; }
.batch-detail { padding: 14px; }
.detail-header { padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.detail-title { display: flex; align-items: center; gap: 9px; }
.detail-title h2 { margin: 0; font-size: 16px; }
.detail-header p { margin: 6px 0; overflow-wrap: anywhere; font: 11px Consolas, monospace; color: var(--text-muted); }
.detail-header small { color: var(--text-muted); font-size: 10px; }
.accounts-section, .timeline-section { padding-top: 12px; }
.accounts-section h3, .timeline-section h3 { margin: 0; font-size: 13px; }
.account-row { margin-top: 8px; padding: 9px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); }
.account-row-head { align-items: center; margin-bottom: 4px; font-size: 12px; }
.account-row small { display: block; }
.account-error { margin: 6px 0 0; color: #a33125; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.timeline-header { display: flex; align-items: end; justify-content: space-between; gap: 12px; padding-bottom: 7px; border-bottom: 1px solid var(--border); }
.timeline-header > div { display: flex; align-items: baseline; gap: 8px; }
.timeline-header label { width: min(240px, 45%); }
.log-entry { padding: 10px 0; border-bottom: 1px solid var(--border); }
.log-entry-head strong { color: var(--text); }
.log-entry time { white-space: nowrap; }
.log-action { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 6px; color: var(--text-muted); font-size: 10px; }
.log-action b { color: var(--text); font-size: 11px; }
.log-message { margin: 5px 0; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.log-message.is-success { color: #087443; }
.log-message.is-info { color: var(--text); }
.log-message.is-warning { color: #8a5b00; }
.log-message.is-error { color: #a33125; }
.product-id { display: block; margin: 3px 0 5px; }
.log-entry details { color: var(--text-muted); font-size: 10px; }
.log-entry summary { cursor: pointer; }
.log-entry pre { max-height: 240px; margin: 6px 0 0; padding: 8px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; border-radius: var(--radius); background: var(--bg-app); font: 10px/1.45 Consolas, monospace; }
.panel-message, .empty-state { padding: 20px 12px; color: var(--text-muted); text-align: center; font-size: 12px; }
.empty-state, .error-banner { margin: 0; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.error-banner { color: #a33125; background: #fdecea; font-size: 12px; }
@media (max-width: 1050px) {
  .filters-card { grid-template-columns: repeat(2, minmax(130px, 1fr)); }
  .filter-actions { grid-column: 1 / -1; }
  .logs-layout { grid-template-columns: minmax(220px, 300px) minmax(0, 1fr); }
}
@media (max-width: 760px) { .logs-layout { grid-template-columns: 1fr; } .batch-panel { max-height: 330px; } }
</style>
