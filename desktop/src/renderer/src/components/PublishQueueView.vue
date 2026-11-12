<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'

const props = defineProps<{ productDraft?: { accountId: number; productId: string; title: string; sourceUrl: string } | null }>()
const emit = defineEmits<{ (event: 'product-draft-consumed'): void }>()

interface QueueAccount {
  id: number
  name: string
  status: string
}

interface QueueRow {
  id: number
  account_id: number
  account_name: string
  product_id: string
  product_name: string
  product_url: string
  video_path: string
  title: string
  description: string
  schedule_at: string
  mode: 'manual' | 'scheduled'
  auto_publish: number | boolean
  status: 'awaiting_confirmation' | 'scheduled' | 'cancelled'
  platform_status: 'not_uploaded' | 'uploading' | 'uploaded_waiting_publish' | 'submitting' | 'submitted_review' | 'needs_review' | 'submission_uncertain' | 'published' | 'isolated'
  platform_error: string
  screenshot_path: string
  execution_started_at: string | null
  platform_updated_at: string | null
  published_at: string | null
  created_at: string
  updated_at: string
}

interface PublishEvent {
  id: number
  stage: string
  outcome: string
  reason: string
  screenshot_path: string
  cleared_at: string | null
  created_at: string
}

interface SelectedProductOption {
  account_id: number
  product_id: string
  product_name: string
  product_url: string
  description: string
  selected_at: string
}

interface PublishForm {
  productId: string
  videoPath: string
  title: string
  description: string
  plannedAt: string
  mode: 'manual' | 'scheduled'
}

const DB_PATH = 'D:\\software\\投广客户端\\data\\ad_client.db'
const accounts = ref<QueueAccount[]>([])
const rows = ref<QueueRow[]>([])
const products = ref<SelectedProductOption[]>([])
const loading = ref(false)
const saving = ref(false)
const busyId = ref<number | null>(null)
const notice = ref('')
const noticeError = ref(false)
const activeAccountId = ref(0)
const autoPublishEnabled = ref(false)
const manualReviewActive = ref(false)
const autoPublishToggleSaving = ref(false)
const createFormOpen = ref(false)
const formCardRef = ref<HTMLElement | null>(null)
const schedulerDetails = ref<HTMLDetailsElement | null>(null)
const publishProgress = ref<PublishProgressPayload | null>(null)
const manualRetryNoDraftIds = ref<Record<number, boolean>>({})
const selectedRetryIds = ref<number[]>([])
const eventQueueId = ref<number | null>(null)
const receiptEvents = ref<PublishEvent[]>([])
const schedulerSettings = reactive({ jitterMinSeconds: 5, jitterMaxSeconds: 15, catchupMaxLateHours: 0 })
const schedulerSettingsSaving = ref(false)
let refreshTimer: number | undefined
let stopAutoProgress: (() => void) | undefined
let stopBrowserProgress: (() => void) | undefined
let progressEventsReceived = 0

watch(notice, (message) => {
  if (message) notifyGlobal(message, noticeError.value ? 'error' : undefined)
})

function receivePublishProgress(progress: PublishProgressPayload): void {
  progressEventsReceived += 1
  publishProgress.value = progress
  if (progress.state === 'warning' && progress.message) notifyGlobal(progress.message, 'warning')
  if (progress.state === 'succeeded' || progress.state === 'warning' || progress.state === 'failed') void refresh()
}

function progressForRow(row: QueueRow): PublishProgressPayload | null {
  const current = publishProgress.value
  return current?.queueId === row.id && current.accountId === row.account_id ? current : null
}

function localDateTimeAfter(minutes: number): string {
  const value = new Date(Date.now() + minutes * 60_000)
  value.setSeconds(0, 0)
  const pad = (part: number): string => String(part).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}`
}

function createForm(): PublishForm {
  return {
    productId: '',
    videoPath: '',
    title: '',
    description: '',
    plannedAt: localDateTimeAfter(10),
    mode: 'manual'
  }
}

// 每个账号保留自己的表单状态，切换账号时不会串用商品、视频或简介。
const forms = reactive<Record<number, PublishForm>>({})
const form = computed(() => forms[activeAccountId.value] ?? createForm())
const accountTabs = computed(() => accounts.value)
const activeAccount = computed(() => accountTabs.value.find((account) => account.id === activeAccountId.value) ?? null)
const accountRows = computed(() => rows.value.filter((row) => row.account_id === activeAccountId.value))
const accountPageSize = 30
const accountPage = ref(1)
const accountPageCount = computed(() => Math.max(1, Math.ceil(accountRows.value.length / accountPageSize)))
const visibleAccountRows = computed(() => {
  const start = (accountPage.value - 1) * accountPageSize
  return accountRows.value.slice(start, start + accountPageSize)
})
watch(accountRows, () => {
  if (accountPage.value > accountPageCount.value) accountPage.value = accountPageCount.value
})

function setAccountPage(page: number): void {
  accountPage.value = Math.max(1, Math.min(accountPageCount.value, page))
}

const quarantinedAutoTasks = computed(() => rows.value.filter((row) =>
  Boolean(row.auto_publish) &&
  ['uploading', 'uploaded_waiting_publish', 'needs_review', 'submitting', 'submission_uncertain'].includes(row.platform_status)
))
function conflictingPublishTask(row: QueueRow): QueueRow | undefined {
  return rows.value.find((other) => other.id !== row.id &&
    other.account_id === row.account_id && other.product_id === row.product_id &&
    (other.status !== 'cancelled' || !['not_uploaded', 'isolated'].includes(other.platform_status)))
}
const activeCount = computed(() => accountRows.value.filter((row) => row.status !== 'cancelled').length)
const pendingCount = computed(() => accountRows.value.filter((row) => row.status === 'awaiting_confirmation').length)
const videoName = computed(() => form.value.videoPath.split(/[\\/]/).filter(Boolean).pop() ?? '')
const accountProducts = computed(() => products.value.filter((product) => product.account_id === activeAccountId.value))
const selectedProduct = computed(() => accountProducts.value.find((product) => product.product_id === form.value.productId) ?? null)

function applyProductDraft(): void {
  const draft = props.productDraft
  if (!draft || !accounts.value.some((account) => account.id === draft.accountId)) return
  activeAccountId.value = draft.accountId
  forms[draft.accountId] = {
    ...createForm(),
    productId: draft.productId,
    title: draft.title,
    description: draft.title
  }
  createFormOpen.value = true
  emit('product-draft-consumed')
  requestAnimationFrame(() => formCardRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

watch(() => props.productDraft, () => applyProductDraft(), { immediate: true })
watch(() => accounts.value.length, () => applyProductDraft())

function accountPlanCount(accountId: number): number {
  return rows.value.filter((row) => row.account_id === accountId && row.status !== 'cancelled').length
}

function selectAccount(accountId: number): void {
  if (accountTabs.value.some((account) => account.id === accountId)) {
    activeAccountId.value = accountId
    accountPage.value = 1
    createFormOpen.value = false
    selectedRetryIds.value = []
    eventQueueId.value = null
  }
}

function openSchedulerSettings(): void {
  if (!schedulerDetails.value) return
  schedulerDetails.value.open = true
  requestAnimationFrame(() => schedulerDetails.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

function openCreateForm(): void {
  createFormOpen.value = true
  requestAnimationFrame(() => formCardRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
}

async function toggleManualReview(): Promise<void> {
  if (schedulerSettingsSaving.value) return
  const next = !manualReviewActive.value
  if (!next && autoPublishEnabled.value && !window.confirm('关闭人工复核后，到期的自动任务可能直接提交最终发布。确认关闭？')) return
  schedulerSettingsSaving.value = true
  try {
    const saved = await runCli(['auto-settings', 'set', '--manual-review-enabled', String(next)])
    if (saved.ok !== true) throw new Error(String(saved.error ?? '人工复核设置未保存'))
    manualReviewActive.value = saved.manualReviewEnabled === true
    noticeError.value = false
    notice.value = manualReviewActive.value ? '人工复核已开启：自动任务停在最终发布前。' : '人工复核已关闭：自动任务按原计划执行。'
  } catch (error) {
    noticeError.value = true
    notice.value = `人工复核设置失败：${String(error)}`
  } finally {
    schedulerSettingsSaving.value = false
  }
}

function syncDescriptionWithProduct(): void {
  form.value.description = selectedProduct.value?.product_name ?? ''
}

function runCli(args: string[], timeoutMs = 30_000, script = 'publish_queue.py'): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let taskId: string | null = null
    let settled = false
    let timer = 0
    let off: () => void = () => undefined
    const pendingEvents: EngineEventPayload[] = []
    const finish = (callback: () => void): void => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      off()
      callback()
    }
    const handleEvent = (event: EngineEventPayload): void => {
      if (taskId && event.taskId !== taskId) return
      if (event.type === 'result') {
        finish(() => resolve((event.data ?? {}) as Record<string, unknown>))
      } else if (event.type === 'error') {
        finish(() => reject(new Error(event.message ?? '发布队列命令失败')))
      }
    }
    off = window.api.engine.onEvent((event) => {
      if (!taskId) pendingEvents.push(event)
      else handleEvent(event)
    })
    timer = window.setTimeout(() => finish(() => reject(new Error('发布队列命令超时'))), timeoutMs)
    void window.api.engine.run({ script, args: ['--db', DB_PATH, ...args] })
      .then((reply) => {
        if (!reply.taskId) finish(() => reject(new Error(reply.error ?? '发布队列命令启动失败')))
        else {
          taskId = reply.taskId
          for (const event of pendingEvents.splice(0)) handleEvent(event)
        }
      })
      .catch((error: unknown) => finish(() => reject(error instanceof Error ? error : new Error(String(error)))))
  })
}

async function refresh(): Promise<void> {
  loading.value = true
  try {
    const result = await runCli(['list'])
    const publishSettings = await runCli(['auto-settings', 'get'])
    accounts.value = (result.accounts ?? []) as QueueAccount[]
    rows.value = (result.rows ?? []) as QueueRow[]
    products.value = (result.products ?? []) as SelectedProductOption[]
    autoPublishEnabled.value = publishSettings.autoPublishEnabled === true
    manualReviewActive.value = publishSettings.manualReviewEnabled === true
    schedulerSettings.jitterMinSeconds = Number(publishSettings.publishJitterMinSeconds ?? 5)
    schedulerSettings.jitterMaxSeconds = Number(publishSettings.publishJitterMaxSeconds ?? 15)
    schedulerSettings.catchupMaxLateHours = Number(publishSettings.publishCatchupMaxLateHours ?? 0)
    for (const account of accounts.value) {
      if (!forms[account.id]) forms[account.id] = createForm()
    }
    if (!accountTabs.value.some((account) => account.id === activeAccountId.value)) {
      const nextAccountId = accountTabs.value.find((account) => account.status === 'active')?.id ?? accountTabs.value[0]?.id ?? 0
      if (nextAccountId !== activeAccountId.value) accountPage.value = 1
      activeAccountId.value = nextAccountId
    }
    for (const account of accounts.value) {
      const accountId = account.id
      const accountForm = forms[accountId]
      const selected = products.value.find((product) => product.account_id === accountId && product.product_id === accountForm.productId)
      if (selected && !accountForm.description.trim()) accountForm.description = selected.product_name
    }
    applyProductDraft()
    noticeError.value = false
    if (accountTabs.value.length === 0) notice.value = '尚未添加账号。请先在账号管理中添加并启用账号。'
    else if (!notice.value) notice.value = ''
  } catch (error) {
    noticeError.value = true
    notice.value = `读取发布队列失败：${String(error)}`
  } finally {
    loading.value = false
  }
}

async function resumeAutoPublishIfClear(): Promise<string> {
  if (autoPublishEnabled.value) return ''
  try {
    const result = await runCli(['auto-settings', 'set', '--enabled', 'true'])
    if (result.ok !== true) {
      return `自动发布暂未恢复：${String(result.error ?? '设置未能保存')}`
    }
    autoPublishEnabled.value = result.autoPublishEnabled === true
    return autoPublishEnabled.value
      ? `全自动发布已恢复；${quarantinedAutoTasks.value.length ? `${quarantinedAutoTasks.value.length} 条待核查任务继续隔离，` : ''}其他可执行任务按队列继续。`
      : ''
  } catch (error) {
    return `自动发布暂未恢复：${String(error)}`
  }
}

async function toggleAutoPublish(): Promise<void> {
  if (autoPublishToggleSaving.value) return
  const next = !autoPublishEnabled.value
  const actionText = next
    ? manualReviewActive.value
      ? '到期的自动任务将继续上传，并停在最终发布前，等待你手动点击“发布”。'
      : '到期的自动任务将按计划自动上传并提交发布。'
    : '全自动调度将暂停，已保存任务会留在队列中；你仍可使用手动计划和手动发布。正在执行的任务不会被强制中断。'
  const quarantineText = next && quarantinedAutoTasks.value.length
    ? `\n另有 ${quarantinedAutoTasks.value.length} 条待核查任务会继续单独隔离，不会自动重试；其他可执行任务不受影响。`
    : ''
  if (!window.confirm(`${actionText}${quarantineText}\n确认${next ? '开启' : '暂停'}全自动发布？`)) return
  autoPublishToggleSaving.value = true
  try {
    const result = await runCli(['auto-settings', 'set', '--enabled', String(next)])
    if (result.ok !== true) throw new Error(String(result.error ?? '全自动设置未保存'))
    autoPublishEnabled.value = result.autoPublishEnabled === true
    noticeError.value = false
    notice.value = autoPublishEnabled.value
      ? manualReviewActive.value
        ? `全自动已开启；人工复核仍开启，最终发布需要你手动确认。${quarantinedAutoTasks.value.length ? ` ${quarantinedAutoTasks.value.length} 条待核查任务仍隔离，其他任务继续。` : ''}`
        : `全自动已开启；到期自动任务将按队列执行。${quarantinedAutoTasks.value.length ? ` ${quarantinedAutoTasks.value.length} 条待核查任务仍隔离，其他任务继续。` : ''}手动计划仍可单独创建。`
      : '全自动已暂停；队列任务保留，手动发布入口仍可使用。'
    await refresh()
  } catch (error) {
    noticeError.value = true
    notice.value = `更新全自动状态失败：${String(error)}`
  } finally {
    autoPublishToggleSaving.value = false
  }
}

async function saveSchedulerSettings(): Promise<void> {
  const { jitterMinSeconds, jitterMaxSeconds, catchupMaxLateHours } = schedulerSettings
  if (!Number.isInteger(jitterMinSeconds) || jitterMinSeconds < 0 || jitterMinSeconds > 3600 ||
      !Number.isInteger(jitterMaxSeconds) || jitterMaxSeconds < 0 || jitterMaxSeconds > 3600 ||
      jitterMinSeconds > jitterMaxSeconds ||
      !Number.isInteger(catchupMaxLateHours) || catchupMaxLateHours < 0 || catchupMaxLateHours > 8760) {
    noticeError.value = true
    notice.value = '调度设置无效：错峰范围需为 0–3600 秒且最大值不小于最小值；逾期补发期限需为 0–8760 小时。'
    return
  }
  schedulerSettingsSaving.value = true
  try {
    const saved = await runCli([
      'auto-settings', 'set',
      '--jitter-min-seconds', String(jitterMinSeconds),
      '--jitter-max-seconds', String(jitterMaxSeconds),
      '--catchup-max-late-hours', String(catchupMaxLateHours)
    ])
    if (saved.ok !== true) throw new Error(String(saved.error ?? '调度设置未保存'))
    schedulerSettings.jitterMinSeconds = Number(saved.publishJitterMinSeconds ?? jitterMinSeconds)
    schedulerSettings.jitterMaxSeconds = Number(saved.publishJitterMaxSeconds ?? jitterMaxSeconds)
    schedulerSettings.catchupMaxLateHours = Number(saved.publishCatchupMaxLateHours ?? catchupMaxLateHours)
    manualReviewActive.value = saved.manualReviewEnabled === true
    noticeError.value = false
    notice.value = '已保存本机调度设置。'
  } catch (error) {
    noticeError.value = true
    notice.value = `保存调度设置失败：${String(error)}`
  } finally {
    schedulerSettingsSaving.value = false
  }
}

async function chooseVideo(): Promise<void> {
  const path = await window.api.dir.choosePublishVideoFile()
  if (!path) return
  form.value.videoPath = path
  if (!form.value.title.trim()) form.value.title = path.split(/[\\/]/).pop()?.replace(/\.mp4$/i, '') ?? ''
}

async function getAccountProfilePath(accountId: number): Promise<string | undefined> {
  const result = await runCli(
    ['get', '--account-id', String(accountId)],
    30_000,
    'selection_settings.py'
  )
  const settings = result.settings as { account?: Record<string, unknown> } | undefined
  const profilePath = settings?.account?.browserProfilePath
  return typeof profilePath === 'string' && profilePath.trim() ? profilePath : undefined
}

async function openCreatorCenter(accountId: number): Promise<void> {
  const account = accounts.value.find((item) => item.id === accountId)
  if (!account || account.status !== 'active') {
    noticeError.value = true
    notice.value = '请先在账号管理中启用此账号，再打开创作者中心。'
    return
  }
  busyId.value = -accountId
  try {
    const profilePath = await getAccountProfilePath(accountId)
    const validation = await window.api.browser.validateProfilePath({ profileKey: `account-${accountId}`, profilePath })
    if (!validation.ok) throw new Error(validation.error ?? '账号浏览器目录校验失败')
    const result = await window.api.browser.openDouyinCreator({ accountId, profilePath })
    if (result.ok !== true) throw new Error(String(result.error ?? '创作者中心打开失败'))
    noticeError.value = false
    notice.value = `已在${account.name}专属浏览器中打开抖音创作者中心。`
  } catch (error) {
    noticeError.value = true
    notice.value = `打开创作者中心失败：${String(error)}`
  } finally {
    busyId.value = null
  }
}

async function publishImmediately(row: QueueRow): Promise<void> {
  if (row.status !== 'scheduled' || row.platform_status !== 'not_uploaded' || !row.auto_publish) {
    noticeError.value = true
    notice.value = '只有尚未上传的全自动排期任务可以马上发布；请刷新队列后再试。'
    return
  }
  const account = accounts.value.find((item) => item.id === row.account_id)
  if (!account || account.status !== 'active') {
    noticeError.value = true
    notice.value = '请先启用此任务所属账号，再马上发布。'
    return
  }
  const conflict = conflictingPublishTask(row)
  if (conflict) {
    noticeError.value = true
    notice.value = `同账号的同一商品已有任务 #${conflict.id} 处于“${conflict.platform_status}”；先核对该商品，避免重复发布。`
    return
  }
  if (!window.confirm(
    `将立即把“${row.product_name}”的计划时间调整为现在，并按${row.account_name}今日配额重新校验；随后上传视频、关联商品并提交抖音发布。同账号其他商品的待核查任务会保持隔离，不阻止本条。确认马上发布？`
  )) return

  busyId.value = row.id
  notice.value = ''
  publishProgress.value = {
    state: 'running',
    queueId: row.id,
    accountId: row.account_id,
    productName: row.product_name,
    stage: '校验马上发布条件',
    message: '正在检查账号状态、发布边界、待核查任务和今日配额',
    indeterminate: true,
    updatedAt: new Date().toISOString()
  }
  try {
    const result = await window.api.publish.runImmediately(row.id)
    if (result.state === 'failed') {
      throw new Error(result.error ?? result.message ?? '马上发布未完成')
    }
    noticeError.value = false
    notice.value = result.state === 'warning'
      ? ''
      : result.state === 'succeeded'
        ? `“${row.product_name}”已完成平台提交：${result.message ?? result.stage}`
        : `“${row.product_name}”马上发布已启动，正在处理。`
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    publishProgress.value = {
      ...publishProgress.value,
      state: 'failed',
      stage: '马上发布未完成',
      message,
      error: message,
      indeterminate: false,
      updatedAt: new Date().toISOString()
    }
    noticeError.value = true
    notice.value = `马上发布失败：${message}`
  } finally {
    busyId.value = null
    await refresh()
  }
}

function canRetryManually(row: QueueRow): boolean {
  return row.status === 'scheduled' && [
    'needs_review', 'uploading', 'uploaded_waiting_publish',
    'submitting', 'submission_uncertain', 'isolated'
  ].includes(row.platform_status)
}

async function retryManually(row: QueueRow, clearFailure = false): Promise<void> {
  if (!canRetryManually(row)) {
    noticeError.value = true
    notice.value = '该任务当前不可手动重发；已发布、审核中、取消或尚未上传的任务不会重复提交。'
    return
  }
  const account = accounts.value.find((item) => item.id === row.account_id)
  if (!account || account.status !== 'active') {
    noticeError.value = true
    notice.value = '请先启用此任务所属账号，再手动重发。'
    return
  }

  busyId.value = row.id
  try {
    // 先读取本条的最新检查点，确认后仍由主进程重新校验，防止页面上的旧状态触发重复投稿。
    const plan = await runCli(['manual-retry-plan', '--id', String(row.id)])
    if (plan.ok !== true) throw new Error(String(plan.error ?? '无法读取本条重发检查点'))
    const needsConfirmNotSubmitted = plan.needsConfirmNotSubmitted === true
    const needsConfirmNoDraft = manualRetryNoDraftIds.value[row.id] === true
    const confirmation: string[] = []
    if (needsConfirmNotSubmitted) {
      confirmation.push('请先核对该账号创作者中心，确认这条作品没有发布或进入审核。')
    }
    if (needsConfirmNoDraft) {
      confirmation.push('上次未找到可恢复的现有投稿。请确认创作者中心也没有这条作品的草稿；确认后本次才会重新上传视频。')
    }
    if (row.auto_publish && !manualReviewActive.value) {
      confirmation.push('原任务开启了自动发布，本次恢复成功后可能点击最终“发布”。')
    } else {
      confirmation.push('本次会停在最终发布前，由你检查后手动点击本条“发布”。')
    }
    if (clearFailure) confirmation.push('当前失败提示会清除；原失败原因和截图仍保留在过程日志中。')
    if (!window.confirm(`仅重试“${row.product_name}”（任务 #${row.id}），不启动其他隔离或待核查任务。\n\n${confirmation.join('\n')}\n\n确认继续？`)) return

    if (clearFailure) {
      const cleared = await runCli(['clear-failure-log', '--id', String(row.id)])
      if (cleared.ok !== true) throw new Error(String(cleared.error ?? '失败提示未能清除'))
    }

    notice.value = ''
    publishProgress.value = {
      state: 'running',
      queueId: row.id,
      accountId: row.account_id,
      productName: row.product_name,
      stage: '读取手动重发检查点',
      message: '优先恢复已有投稿；仅在核实没有草稿后才允许重新上传',
      indeterminate: true,
      updatedAt: new Date().toISOString()
    }
    const result = await window.api.publish.retryManually(row.id, {
      confirmedNotSubmitted: needsConfirmNotSubmitted,
      confirmedNoDraft: needsConfirmNoDraft
    })
    publishProgress.value = result
    if (result.state === 'failed' && result.classification === 'MANUAL_RETRY_EXISTING_POST_NOT_FOUND' && result.canConfirmNoDraft === true) {
      manualRetryNoDraftIds.value = { ...manualRetryNoDraftIds.value, [row.id]: true }
      noticeError.value = false
      notice.value = `“${row.product_name}”未找到可恢复的现有投稿；未重新上传。请先在创作者中心核实没有同一作品草稿，再点击本条“手动重新发布”确认重新上传。`
      return
    }
    if (result.state === 'failed') throw new Error(result.error ?? result.message ?? '手动重发未完成')
    manualRetryNoDraftIds.value = { ...manualRetryNoDraftIds.value, [row.id]: false }
    noticeError.value = false
    notice.value = result.stage === '待人工发布'
      ? `“${row.product_name}”已恢复到待人工发布；请核对创作者中心后点击本条“发布”。`
      : result.message || `“${row.product_name}”手动重发已完成当前阶段，请查看本条的平台状态。`
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    publishProgress.value = {
      ...publishProgress.value,
      state: 'failed',
      queueId: row.id,
      accountId: row.account_id,
      productName: row.product_name,
      stage: '手动重发未完成',
      message,
      error: message,
      indeterminate: false,
      updatedAt: new Date().toISOString()
    }
    noticeError.value = true
    notice.value = `“${row.product_name}”手动重发未完成：${message}`
  } finally {
    busyId.value = null
    const retryNotice = notice.value
    const retryNoticeError = noticeError.value
    await refresh()
    if (notice.value === retryNotice) noticeError.value = retryNoticeError
  }
}

async function retrySelected(): Promise<void> {
  const selected = accountRows.value.filter((row) => selectedRetryIds.value.includes(row.id) && canRetryManually(row))
  if (!selected.length) return
  if (busyId.value !== null) return
  try {
    const plans: Array<{ row: QueueRow; plan: Record<string, unknown> }> = []
    for (const row of selected) {
      const plan = await runCli(['manual-retry-plan', '--id', String(row.id)])
      if (plan.ok !== true) throw new Error(`#${row.id} 检查点不可恢复：${String(plan.error ?? '')}`)
      plans.push({ row, plan })
    }
    const needSubmissionCheck = plans.filter(({ plan }) => plan.needsConfirmNotSubmitted === true)
    const summary = plans.map(({ row }) => `#${row.id} ${row.product_name}`).join('\n')
    const warning = needSubmissionCheck.length
      ? `其中 ${needSubmissionCheck.map(({ row }) => `#${row.id}`).join('、')} 必须先确认创作者中心没有已提交或审核中的同一作品。\n`
      : ''
    const publishWarning = plans.some(({ row }) => row.auto_publish) && !manualReviewActive.value
      ? '其中自动发布任务在恢复成功后可能点击最终“发布”。\n' : ''
    if (!window.confirm(`将顺序重试以下 ${plans.length} 条任务；每条优先恢复原投稿，任何一条失败即停止后续。\n${warning}${publishWarning}\n${summary}\n\n确认开始？`)) return
    let completed = 0
    for (const { row, plan } of plans) {
      busyId.value = row.id
      const result = await window.api.publish.retryManually(row.id, {
        confirmedNotSubmitted: plan.needsConfirmNotSubmitted === true,
        confirmedNoDraft: false
      })
      if (result.state === 'failed') throw new Error(`#${row.id}：${result.error ?? result.message ?? '重试未完成'}`)
      completed += 1
      await refresh()
    }
    selectedRetryIds.value = []
    noticeError.value = false
    notice.value = `已顺序处理 ${completed} 条；请按各条平台状态核对回执。`
  } catch (error) {
    noticeError.value = true
    notice.value = `批量重试已停止：${String(error)}`
  } finally {
    busyId.value = null
    await refresh()
  }
}

async function exportReceipts(): Promise<void> {
  const result = await window.api.publish.exportReceipts()
  if (result.cancelled) return
  noticeError.value = !result.ok
  notice.value = result.ok ? `已导出 ${result.count ?? 0} 条发布回执：${result.path ?? ''}` : `导出失败：${result.error ?? '未知错误'}`
}

async function openReceiptScreenshot(row: QueueRow): Promise<void> {
  const result = await window.api.publish.openScreenshot(row.screenshot_path)
  if (!result.ok) {
    noticeError.value = true
    notice.value = result.error ?? '无法打开截图'
  }
}

async function viewReceiptEvents(row: QueueRow): Promise<void> {
  try {
    const result = await runCli(['events', '--id', String(row.id)])
    if (result.ok !== true) throw new Error(String(result.error ?? '读取过程日志失败'))
    receiptEvents.value = (result.events ?? []) as PublishEvent[]
    eventQueueId.value = row.id
  } catch (error) {
    noticeError.value = true
    notice.value = `读取任务 #${row.id} 过程日志失败：${String(error)}`
  }
}

async function prepareCreatorUpload(row: QueueRow): Promise<void> {
  if (row.status !== 'scheduled') {
    noticeError.value = true
    notice.value = '请先确认本地排期，再准备上传。'
    return
  }
  busyId.value = row.id
  notice.value = ''
  publishProgress.value = {
    state: 'running',
    queueId: row.id,
    accountId: row.account_id,
    productName: row.product_name,
    stage: '读取账号发布配置',
    message: '正在校验账号浏览器和视频任务',
    indeterminate: true,
    updatedAt: new Date().toISOString()
  }
  let uploadStarted = false
  let copiedProductUrl = ''
  let screenshotPath = ''
  try {
    const profilePath = await getAccountProfilePath(row.account_id)
    publishProgress.value = { ...publishProgress.value, stage: '校验账号专属浏览器', message: '确认使用本账号的登录 profile', updatedAt: new Date().toISOString() }
    const validation = await window.api.browser.validateProfilePath({
      profileKey: `account-${row.account_id}`,
      profilePath
    })
    if (!validation.ok) throw new Error(validation.error ?? '账号浏览器目录校验失败')

    const task = await runCli(['upload-start', '--id', String(row.id)])
    if (task.ok !== true) throw new Error(String(task.error ?? '无法开始上传准备'))
    uploadStarted = true
    publishProgress.value = { ...publishProgress.value, stage: '连接创作者中心', message: '任务已锁定，开始准备视频上传', updatedAt: new Date().toISOString() }
    const prepared = await window.api.browser.prepareDouyinPublish({
      accountId: row.account_id,
      queueId: row.id,
      profilePath,
      productId: String(task.productId ?? row.product_id),
      productName: String(task.productName ?? row.product_name),
      productUrl: String(task.productUrl ?? row.product_url),
      videoPath: String(task.videoPath ?? row.video_path),
      description: String(task.description ?? row.description ?? row.product_name)
    })
    screenshotPath = typeof prepared.screenshotPath === 'string' ? prepared.screenshotPath : ''
    if (
      prepared.isolated === true &&
      prepared.classification === 'PUBLISH_VERIFY_OTHER_ROW_ISOLATED' &&
      String(prepared.productId ?? '') === String(task.productId ?? row.product_id)
    ) {
      const warning = String(prepared.warning ?? prepared.error ?? '百应商品核验重查耗尽，本任务已隔离；未上传视频。')
      const isolated = await runCli(['upload-isolate', '--id', String(row.id), '--error', warning,
        ...(screenshotPath ? ['--screenshot-path', screenshotPath] : [])])
      if (isolated.ok !== true || isolated.isolated !== true) {
        throw new Error(String(isolated.error ?? '商品核验隔离检查点未能保存'))
      }
      publishProgress.value = {
        ...publishProgress.value,
        state: 'warning',
        stage: '商品核验已隔离',
        message: warning,
        percent: 100,
        indeterminate: false,
        updatedAt: new Date().toISOString()
      }
      noticeError.value = false
      notice.value = ''
      notifyGlobal(warning, 'warning')
      return
    }
    if (prepared.ok !== true) throw new Error(String(prepared.error ?? '创作者中心上传准备未完成'))
    copiedProductUrl = String(prepared.productUrl ?? '').trim()
    if (!copiedProductUrl) throw new Error('百应复制的商品链接没有回传；请检查当前投稿，不要重复上传')
    const platformStatus = String(prepared.platformStatus ?? '')
    if (platformStatus !== 'uploaded_waiting_publish' || prepared.manualPublishRequired !== true) {
      throw new Error('创作者中心没有停在“已上传待人工发布”状态；请检查浏览器，不要重复提交')
    }
    const finished = await runCli([
      'upload-finish', '--id', String(row.id), '--status', platformStatus,
      '--product-url', copiedProductUrl
    ])
    if (finished.ok !== true) throw new Error(String(finished.error ?? '平台已处理，但无法保存发布检查点'))
    publishProgress.value = {
      ...publishProgress.value,
      state: 'succeeded',
      stage: '视频和商品已准备完成',
      message: '流程停在最终发布前；核对无误后，可点击队列中的“发布”提交到抖音',
      percent: 100,
      indeterminate: false,
      updatedAt: new Date().toISOString()
    }
    noticeError.value = false
    notice.value = `${row.account_name}已准备“${row.product_name}”：视频和商品已上传并关联。核对无误后可点击队列中的“发布”；如你已在创作者中心手动发布，可用“仅记录”更新本地状态。`
  } catch (error) {
    const message = String(error)
    publishProgress.value = {
      ...publishProgress.value,
      state: 'failed',
      stage: uploadStarted ? '上传结果需要核对' : '发布准备失败',
      message,
      indeterminate: false,
      error: message,
      updatedAt: new Date().toISOString()
    }
    if (uploadStarted) {
      try {
        const finishArgs = [
          'upload-finish', '--id', String(row.id), '--status', 'needs_review', '--error', message
        ]
        if (copiedProductUrl) finishArgs.push('--product-url', copiedProductUrl)
        if (screenshotPath) finishArgs.push('--screenshot-path', screenshotPath)
        await runCli(finishArgs)
      } catch {
        // 若本地检查点写入也失败，队列仍保留 uploading；下次启动时提示人工先检查浏览器。
      }
    }
    noticeError.value = true
    notice.value = uploadStarted
      ? `上传结果需要核对：${message} 请先检查该账号的创作者中心页面，确认是否已有视频；不要直接重复上传。`
      : `上传准备失败：${message}`
  } finally {
    busyId.value = null
    await refresh()
  }
}

async function markPublished(row: QueueRow): Promise<void> {
  if (!window.confirm(`请在创作者中心确认“${row.product_name}”已经发布。此操作只记录你的核查结果，不会向抖音提交发布。`)) return
  await updateTask(row.id, 'mark-published', '已记录你确认在抖音发布。')
}

async function resetUncertainUpload(row: QueueRow): Promise<void> {
  if (!window.confirm(`请先查看${row.account_name}的创作者中心，确认“${row.product_name}”没有上传成功或生成草稿。确认后才会允许重试上传。`)) return
  await updateTask(row.id, 'reset-upload', '上传状态已重置；可以在确认无草稿后再次准备上传。')
}

async function submitPublish(row: QueueRow): Promise<void> {
  if (row.status !== 'scheduled' || row.platform_status !== 'uploaded_waiting_publish') {
    noticeError.value = true
    notice.value = '该任务当前不处于“已上传待发布”状态，请刷新队列后再试。'
    return
  }
  const account = accounts.value.find((item) => item.id === row.account_id)
  if (!account || account.status !== 'active') {
    noticeError.value = true
    notice.value = '请先启用此任务所属账号，再提交发布。'
    return
  }
  if (!window.confirm(
    `即将通过${row.account_name}的创作者中心提交“${row.product_name}”发布。客户端会核对当前投稿，不会重新上传视频或重复关联商品。确认提交？`
  )) return

  busyId.value = row.id
  notice.value = ''
  publishProgress.value = {
    state: 'running',
    queueId: row.id,
    accountId: row.account_id,
    productName: row.product_name,
    stage: '校验待发布作品',
    message: '准备提交抖音最终发布，不会重新上传视频',
    indeterminate: true,
    updatedAt: new Date().toISOString()
  }
  let submitLockAcquired = false
  let submissionMayHaveBeenAttempted = false
  let screenshotPath = ''
  try {
    const profilePath = await getAccountProfilePath(row.account_id)
    const validation = await window.api.browser.validateProfilePath({
      profileKey: `account-${row.account_id}`,
      profilePath
    })
    if (!validation.ok) throw new Error(validation.error ?? '账号浏览器目录校验失败')

    const task = await runCli(['submit-start', '--id', String(row.id)])
    if (task.ok !== true) throw new Error(String(task.error ?? '无法锁定发布任务'))
    submitLockAcquired = true
    publishProgress.value = {
      ...publishProgress.value,
      stage: '核对创作者中心投稿',
      message: '已锁定任务，核对账号、商品、视频和简介',
      updatedAt: new Date().toISOString()
    }

    // 发出浏览器命令后如果连接超时，无法证明平台没有收到点击，因此按结果不确定处理。
    submissionMayHaveBeenAttempted = true
    const submitted = await window.api.browser.submitDouyinPublish({
      accountId: row.account_id,
      queueId: row.id,
      profilePath,
      productId: String(task.productId ?? row.product_id),
      productName: String(task.productName ?? row.product_name),
      productUrl: String(task.productUrl ?? row.product_url),
      videoPath: String(task.videoPath ?? row.video_path),
      description: String(task.description ?? row.description)
    })
    screenshotPath = typeof submitted.screenshotPath === 'string' ? submitted.screenshotPath : ''
    if (
      submitted.isolated === true &&
      submitted.classification === 'CREATOR_COVER_REQUIRED_ISOLATED' &&
      Number(submitted.queueId) === row.id &&
      String(submitted.productId ?? '') === row.product_id
    ) {
      const warning = String(submitted.warning ?? submitted.error ?? '平台要求封面，当前任务已隔离；未记录为发布成功。')
      const isolated = await runCli(['submit-isolate', '--id', String(row.id), '--error', warning,
        ...(screenshotPath ? ['--screenshot-path', screenshotPath] : [])])
      if (isolated.ok !== true || isolated.isolated !== true) {
        throw new Error(String(isolated.error ?? '平台封面隔离检查点未能保存'))
      }
      submitLockAcquired = false
      publishProgress.value = {
        ...publishProgress.value,
        state: 'warning',
        stage: '平台封面要求已隔离',
        message: warning,
        percent: 100,
        indeterminate: false,
        updatedAt: new Date().toISOString()
      }
      noticeError.value = false
      notice.value = ''
      notifyGlobal(warning, 'warning')
      return
    }
    if (submitted.ok !== true) {
      submissionMayHaveBeenAttempted = submitted.submissionAttempted !== false
      const failedStatus = submissionMayHaveBeenAttempted ? 'submission_uncertain' : 'uploaded_waiting_publish'
      const message = String(submitted.error ?? '抖音未返回发布确认')
      const saved = await runCli([
        'submit-finish', '--id', String(row.id), '--status', failedStatus, '--error', message,
        ...(screenshotPath ? ['--screenshot-path', screenshotPath] : [])
      ])
      if (saved.ok !== true) throw new Error(`${message}；发布检查点保存失败：${String(saved.error ?? '')}`)
      submitLockAcquired = false
      publishProgress.value = {
        ...publishProgress.value,
        state: 'failed',
        stage: submissionMayHaveBeenAttempted ? '发布结果待核对' : '尚未提交发布',
        message,
        error: message,
        indeterminate: false,
        updatedAt: new Date().toISOString()
      }
      noticeError.value = true
      notice.value = submissionMayHaveBeenAttempted
        ? `发布结果不确定：${message} 请先检查创作者中心，确认结果前不要再次提交。`
        : `尚未提交发布：${message} 检查投稿后可以再次点击“发布”。`
      return
    }

    submissionMayHaveBeenAttempted = true
    const platformStatus = String(submitted.platformStatus ?? '')
    if (
      Number(submitted.accountId) !== row.account_id ||
      Number(submitted.queueId) !== row.id ||
      String(submitted.productId ?? '') !== row.product_id ||
      !['published', 'submitted_review'].includes(platformStatus)
    ) {
      throw new Error('抖音提交结果与当前队列任务无法匹配；请检查创作者中心，不要再次提交。')
    }
    const saved = await runCli([
      'submit-finish', '--id', String(row.id), '--status', platformStatus
    ])
    if (saved.ok !== true) throw new Error(String(saved.error ?? '抖音已处理，但本地无法保存发布结果'))
    if (platformStatus === 'published') await window.api.publish.notifyPostPublish(row.id, platformStatus)
    submitLockAcquired = false
    const accepted = platformStatus === 'submitted_review'
    publishProgress.value = {
      ...publishProgress.value,
      state: 'succeeded',
      stage: accepted ? '抖音已接收作品' : '抖音已确认发布',
      message: accepted ? '平台已返回提交/审核回执' : '平台已返回发布成功回执',
      percent: 100,
      indeterminate: false,
      updatedAt: new Date().toISOString()
    }
    noticeError.value = false
    notice.value = accepted
      ? `“${row.product_name}”已提交到抖音，当前等待平台审核。`
      : `“${row.product_name}”已收到抖音发布成功回执。`
  } catch (error) {
    const message = String(error)
    if (submitLockAcquired) {
      const uncertainStatus = submissionMayHaveBeenAttempted ? 'submission_uncertain' : 'uploaded_waiting_publish'
      try {
        const saved = await runCli([
          'submit-finish', '--id', String(row.id), '--status', uncertainStatus, '--error', message,
          ...(screenshotPath ? ['--screenshot-path', screenshotPath] : [])
        ])
        if (saved.ok === true) submitLockAcquired = false
      } catch {
        // 若检查点也无法写入，队列保留 submitting，刷新后仍明确显示需人工核对。
      }
    }
    publishProgress.value = {
      ...publishProgress.value,
      state: 'failed',
      stage: submissionMayHaveBeenAttempted ? '发布结果待核对' : '提交前校验失败',
      message,
      error: message,
      indeterminate: false,
      updatedAt: new Date().toISOString()
    }
    noticeError.value = true
    notice.value = submissionMayHaveBeenAttempted
      ? `发布状态需要核对：${message} 请检查创作者中心，确认前不要重复提交。`
      : `提交前校验失败：${message}`
  } finally {
    busyId.value = null
    await refresh()
    const resumed = await resumeAutoPublishIfClear()
    if (resumed && !noticeError.value) notice.value = `${notice.value} ${resumed}`
  }
}

async function resetUncertainSubmission(row: QueueRow): Promise<void> {
  if (!window.confirm(
    `请先在${row.account_name}的创作者中心检查作品列表、审核列表和草稿，确认“${row.product_name}”没有提交成功。只有确认未提交后，才恢复“发布”按钮；不会重新上传视频。`
  )) return
  await updateTask(row.id, 'reset-submission', '已恢复待发布状态；视频仍保留在创作者中心。')
}

async function addTask(): Promise<void> {
  const accountId = activeAccountId.value
  const targetForm = forms[accountId]
  if (!activeAccount.value || activeAccount.value.status !== 'active') {
    noticeError.value = true
    notice.value = '请先在账号管理中启用此账号，才能加入本地发布队列。'
    return
  }
  const product = products.value.find((item) => item.account_id === accountId && item.product_id === targetForm.productId)
  if (!targetForm.productId || !product) {
    noticeError.value = true
    notice.value = `请先选择${activeAccount.value?.name ?? '当前账号'}已完成选品并加车的商品。`
    return
  }
  if (!targetForm.videoPath) {
    noticeError.value = true
    notice.value = '请先选择已生成的 MP4 视频。'
    return
  }
  saving.value = true
  notice.value = ''
  try {
    const result = await runCli([
      'add', '--account-id', String(accountId), '--product-id', targetForm.productId,
      '--video-path', targetForm.videoPath, '--title', targetForm.title, '--description', targetForm.description,
      '--planned-at', targetForm.plannedAt,
      '--mode', targetForm.mode
    ])
    if (result.ok !== true) throw new Error(String(result.error ?? '无法添加本地计划'))
    const scheduledAt = String(result.scheduleAt ?? targetForm.plannedAt)
    const rolledDays = Number(result.rolledDays ?? 0)
    noticeError.value = false
    notice.value = rolledDays > 0
      ? `每日配额已满，计划顺延 ${rolledDays} 天至 ${formatSchedule(scheduledAt)}。`
      : `已加入本地发布队列：${formatSchedule(scheduledAt)}。`
    targetForm.productId = ''
    targetForm.videoPath = ''
    targetForm.title = ''
    targetForm.description = ''
    targetForm.plannedAt = localDateTimeAfter(10)
    await refresh()
    createFormOpen.value = false
  } catch (error) {
    noticeError.value = true
    notice.value = `添加失败：${String(error)}`
  } finally {
    saving.value = false
  }
}

async function confirmSchedule(row: QueueRow): Promise<void> {
  if (!window.confirm(`确认将“${row.title}”安排在 ${formatSchedule(row.schedule_at)}？\n此操作只确认本地计划，不会发布到抖音。`)) return
  await updateTask(row.id, 'confirm', '已确认本地排期；尚未提交至抖音。')
}

async function cancelTask(row: QueueRow): Promise<void> {
  if (!window.confirm(`取消“${row.title}”的本地计划？取消后会释放该日期的配额。`)) return
  await updateTask(row.id, 'cancel', '已取消本地计划。')
}

async function deleteTask(row: QueueRow): Promise<void> {
  if (row.platform_status !== 'not_uploaded' || !['awaiting_confirmation', 'scheduled', 'cancelled'].includes(row.status)) {
    noticeError.value = true
    notice.value = '只有尚未上传的平台计划可以删除；请先核查已进入平台流程的任务。'
    return
  }
  if (progressForRow(row)?.state === 'running') {
    noticeError.value = true
    notice.value = '该计划当前正在处理中，暂不能删除。'
    return
  }
  if (!window.confirm(`确定永久删除“${row.title}”的本地发布计划？只删除队列记录，不删除视频文件，也不会操作抖音；配额和商品占用会释放，删除后不可恢复。`)) return
  await updateTask(row.id, 'delete', '本地发布计划已删除，视频文件已保留。')
}

async function updateTask(id: number, action: 'confirm' | 'cancel' | 'delete' | 'mark-published' | 'reset-upload' | 'reset-submission', successMessage: string): Promise<void> {
  busyId.value = id
  try {
    const result = action === 'mark-published'
      ? await window.api.publish.markPublished(id)
      : await runCli([action, '--id', String(id)])
    if (result.ok !== true) throw new Error(String(result.error ?? '更新失败'))
    noticeError.value = false
    notice.value = successMessage
    await refresh()
    if (['mark-published', 'reset-upload', 'reset-submission'].includes(action)) {
      const resumed = await resumeAutoPublishIfClear()
      if (resumed) notice.value = `${notice.value} ${resumed}`
    }
  } catch (error) {
    noticeError.value = true
    notice.value = `更新失败：${String(error)}`
  } finally {
    busyId.value = null
  }
}

function formatSchedule(value: string): string {
  return value.replace('T', ' ')
}

function statusLabel(status: QueueRow['status']): string {
  if (status === 'awaiting_confirmation') return '待确认'
  if (status === 'scheduled') return '已排期'
  return '已取消'
}

function platformStatusLabel(status: QueueRow['platform_status']): string {
  if (status === 'uploading') return '上传处理中'
  if (status === 'submitting') return '发布提交中'
  if (status === 'uploaded_waiting_publish') return '已上传，待你发布'
  if (status === 'submitted_review') return '已提交，平台审核中'
  if (status === 'needs_review') return '上传结果待核对'
  if (status === 'submission_uncertain') return '发布结果待核对'
  if (status === 'published') return '已确认发布'
  if (status === 'isolated') return '任务已隔离'
  return '未上传'
}

onMounted(() => {
  stopAutoProgress = window.api.publish.onProgress(receivePublishProgress)
  stopBrowserProgress = window.api.browser.onPublishProgress(receivePublishProgress)
  const receivedBeforeSnapshot = progressEventsReceived
  void window.api.publish.getProgress().then((progress) => {
    if (progressEventsReceived === receivedBeforeSnapshot) publishProgress.value = progress
  }).catch(() => undefined)
  void refresh()
  refreshTimer = window.setInterval(() => {
    if (!loading.value && document.visibilityState === 'visible') void refresh()
  }, 30_000)
})

onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
  stopAutoProgress?.()
  stopBrowserProgress?.()
})
</script>

<template>
  <section class="publish-page">
    <header class="page-head">
      <div>
        <h1>发布进度</h1>
        <p>查看商品发布状态，需要处理的任务会显示在记录中</p>
      </div>
      <div class="head-actions">
        <label class="account-picker">账号
          <select :value="activeAccountId" :disabled="!accountTabs.length" @change="selectAccount(Number(($event.target as HTMLSelectElement).value))">
            <option v-for="account in accountTabs" :key="account.id" :value="account.id">{{ account.name }} · {{ accountPlanCount(account.id) }} 条</option>
          </select>
        </label>
        <button class="button" :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '刷新队列' }}</button>
        <details class="header-more">
          <summary>更多操作</summary>
          <div class="header-more-actions">
            <button class="button primary" :disabled="!activeAccount || activeAccount.status !== 'active'" @click="openCreateForm">新建计划</button>
            <button class="button review-toggle" :class="{ enabled: manualReviewActive }" :disabled="schedulerSettingsSaving" :aria-pressed="manualReviewActive" @click="toggleManualReview">人工复核：{{ manualReviewActive ? '已开' : '已关' }}</button>
            <button class="button" @click="openSchedulerSettings">调度设置</button>
            <button class="button" @click="exportReceipts">导出回执</button>
          </div>
        </details>
      </div>
    </header>

    <div class="auto-publish-state" :class="{ paused: !autoPublishEnabled }">
      <strong>{{ autoPublishEnabled ? '全自动发布已开启' : '全自动发布当前已暂停' }}</strong>
      <span v-if="quarantinedAutoTasks.length > 0">
        {{ quarantinedAutoTasks.length }} 条待核查任务已隔离；{{ autoPublishEnabled ? '其他可执行任务继续排队' : '可恢复全自动继续处理其他任务' }}
      </span>
      <span v-if="manualReviewActive">自动任务停在最终发布前</span>
      <span>自动选品任务默认进入全自动队列；手动计划与人工发布入口保留。</span>
      <button class="button auto-publish-toggle" :disabled="autoPublishToggleSaving || loading"
        @click="toggleAutoPublish">
        {{ autoPublishToggleSaving ? '保存中…' : autoPublishEnabled ? '暂停全自动' : '恢复全自动' }}
      </button>
    </div>

    <details ref="schedulerDetails" class="scheduler-settings">
      <summary>调度细项 · 随机错峰与逾期补发</summary>
      <div class="scheduler-settings-body">
        <label>
          最短随机错峰（秒）
          <input v-model.number="schedulerSettings.jitterMinSeconds" type="number" min="0" max="3600" step="1">
        </label>
        <label>
          最长随机错峰（秒）
          <input v-model.number="schedulerSettings.jitterMaxSeconds" type="number" min="0" max="3600" step="1">
        </label>
        <label>
          逾期计划最长补发期限（小时）
          <input v-model.number="schedulerSettings.catchupMaxLateHours" type="number" min="0" max="8760" step="1">
        </label>
        <button class="button primary" :disabled="schedulerSettingsSaving" @click="saveSchedulerSettings">
          {{ schedulerSettingsSaving ? '保存中…' : '保存调度设置' }}
        </button>
        <small>错峰单位为秒；逾期期限填 0 表示不限。</small>
      </div>
    </details>

    <details class="workflow-help"><summary>查看发布流程说明</summary><p>自动任务到期后按账号逐个处理，失败任务隔离；人工计划依次确认排期、上传关联商品，再由你点击最终“发布”。</p></details>

    <div v-if="notice" class="notice result" :class="{ error: noticeError }">{{ notice }}</div>
    <section v-if="publishProgress && publishProgress.state !== 'idle'" class="publish-progress-card" :class="publishProgress.state">
      <div class="publish-progress-head">
        <strong>{{ publishProgress.state === 'running' ? '发布处理中' : publishProgress.state === 'succeeded' ? '本次处理完成' : '发布需要检查' }}</strong>
        <span v-if="publishProgress.queueId">任务 #{{ publishProgress.queueId }} · {{ publishProgress.productName || rows.find((row) => row.id === publishProgress?.queueId)?.product_name || '商品任务' }}</span>
      </div>
      <div class="publish-progress-stage">{{ publishProgress.stage }}</div>
      <div v-if="publishProgress.state === 'running'" class="progress-track" role="progressbar" :aria-valuenow="publishProgress.percent" :aria-valuemin="0" :aria-valuemax="100">
        <span v-if="typeof publishProgress.percent === 'number'" class="progress-fill" :style="{ width: `${publishProgress.percent}%` }"></span>
        <span v-else class="progress-indeterminate"></span>
      </div>
      <small v-if="publishProgress.state === 'running' && typeof publishProgress.percent === 'number'">当前阶段 {{ publishProgress.percent }}%</small>
      <div v-else-if="publishProgress.state === 'succeeded'" class="progress-track" role="progressbar" aria-valuenow="100" aria-valuemin="0" aria-valuemax="100"><span class="progress-fill" style="width: 100%"></span></div>
      <p v-if="publishProgress.message || publishProgress.error" class="publish-progress-message">{{ publishProgress.error || publishProgress.message }}</p>
      <small v-if="publishProgress.state === 'running' && typeof publishProgress.percent !== 'number'">平台没有提供可验证的上传百分比，当前显示实际处理阶段。</small>
    </section>

    <section v-if="activeAccount" class="account-workspace">
      <header class="account-head">
        <div>
          <h2>{{ activeAccount.name }}</h2>
          <p>有效计划 {{ activeCount }} · 待确认 {{ pendingCount }} · 共 {{ accountRows.length }} 条记录</p>
        </div>
        <div class="account-actions">
          <button class="button" :disabled="activeAccount.status !== 'active' || busyId === -activeAccount.id" @click="openCreatorCenter(activeAccount.id)">{{ busyId === -activeAccount.id ? '正在打开…' : '打开创作者中心' }}</button>
          <button v-if="createFormOpen" class="button" @click="createFormOpen = false">收起新建计划</button>
        </div>
      </header>
      <p v-if="activeAccount.status !== 'active'" class="hint">此账号已停用；队列记录仍可查看，启用后才能创建或处理平台任务。</p>

      <div v-if="createFormOpen" ref="formCardRef" class="card form-card">
        <div class="card-title">
          <h2>添加 {{ activeAccount.name }} 视频计划</h2>
          <span>选择本账号商品和 MP4 视频</span>
        </div>
        <div class="form-grid">
        <label>
          <span>{{ activeAccount.name }}的已加车商品</span>
          <select v-model="form.productId" :disabled="accountProducts.length === 0" @change="syncDescriptionWithProduct">
            <option value="">{{ accountProducts.length ? '请选择本账号已加车商品' : '本账号暂无已加车商品记录' }}</option>
            <option v-for="product in accountProducts" :key="product.product_id" :value="product.product_id">
              {{ product.product_name }}（{{ product.product_id }}）
            </option>
          </select>
          <small v-if="selectedProduct" class="product-link">商品链接：{{ selectedProduct.product_url }}</small>
        </label>
        <label class="wide">
          <span>本地视频</span>
          <div class="file-pick">
            <div class="file-name" :title="form.videoPath">{{ videoName || '尚未选择 MP4 文件' }}</div>
            <button class="button" type="button" @click="chooseVideo">选择文件</button>
          </div>
        </label>
        <label class="wide">
          <span>视频简介（默认取所选商品名称，可按需修改）</span>
          <textarea v-model.trim="form.description" rows="3" maxlength="500" placeholder="选择商品后自动填入商品名称" />
        </label>
        <label class="wide">
          <span>本地计划备注（可留空，默认使用文件名）</span>
          <input v-model.trim="form.title" placeholder="例如：商品主图视频 9:16" />
        </label>
        <label>
          <span>计划时间（本机时间）</span>
          <input v-model="form.plannedAt" type="datetime-local" />
        </label>
        <label>
          <span>确认方式</span>
          <select v-model="form.mode">
            <option value="manual">先保存为待确认（仅本地）</option>
            <option value="scheduled">保存并确认本地排期（仍未上传）</option>
          </select>
        </label>
      </div>
      <div class="form-actions">
        <span>这里只保存本地计划，不会上传或发布。到队列中先确认排期，再上传视频；最后点击“发布”才会提交到抖音。</span>
        <button class="button primary" :disabled="saving || loading || activeAccount.status !== 'active'" @click="addTask">
          {{ saving ? '保存中…' : '保存本地计划' }}
        </button>
      </div>
      </div>

      <div class="card list-card">
        <div class="card-title">
          <h2>{{ activeAccount.name }}发布记录</h2>
          <span>{{ accountRows.length }} 条记录</span>
          <button v-if="selectedRetryIds.length" class="button" :disabled="busyId !== null" @click="retrySelected">批量重试已勾选（{{ selectedRetryIds.length }}）</button>
        </div>
        <nav v-if="accountPageCount > 1" class="queue-pagination" aria-label="发布记录分页">
          <span>第 {{ accountPage }} / {{ accountPageCount }} 页 · 每页 {{ accountPageSize }} 条</span>
          <button class="button" :disabled="accountPage <= 1" @click="setAccountPage(accountPage - 1)">上一页</button>
          <button class="button" :disabled="accountPage >= accountPageCount" @click="setAccountPage(accountPage + 1)">下一页</button>
        </nav>
        <div v-if="accountRows.length === 0" class="empty">暂无发布记录。点击上方“新建计划”添加。</div>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr><th>重试</th><th>计划时间</th><th>商品与视频</th><th>状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in visibleAccountRows" :key="row.id" :class="{ cancelled: row.status === 'cancelled' }">
                <td><input v-if="canRetryManually(row)" v-model="selectedRetryIds" type="checkbox" :value="row.id" :aria-label="`选择重试任务 #${row.id}`"></td>
                <td class="date-cell">{{ formatSchedule(row.schedule_at) }}</td>
                <td class="id-cell" :title="`${row.product_id} · ${row.video_path}`"><b>{{ row.product_name || '商品名称未保存' }}</b><small>{{ row.video_path.split(/[\\/]/).pop() }}</small></td>
                <td class="platform-cell">
                  <span class="badge" :class="row.platform_status">{{ platformStatusLabel(row.platform_status) }}</span>
                  <span v-if="row.status !== 'scheduled'" class="badge" :class="row.status">{{ statusLabel(row.status) }}</span>
                  <div v-if="progressForRow(row)" class="row-publish-progress">
                    <small>{{ progressForRow(row)?.stage }}</small>
                    <div v-if="progressForRow(row)?.state === 'running'" class="progress-track" role="progressbar" :aria-valuenow="progressForRow(row)?.percent" :aria-valuemin="0" :aria-valuemax="100">
                      <span v-if="typeof progressForRow(row)?.percent === 'number'" class="progress-fill" :style="{ width: `${progressForRow(row)?.percent}%` }"></span>
                      <span v-else class="progress-indeterminate"></span>
                    </div>
                    <small v-if="typeof progressForRow(row)?.percent === 'number'">当前阶段 {{ progressForRow(row)?.percent }}%</small>
                    <small v-if="progressForRow(row)?.message">{{ progressForRow(row)?.message }}</small>
                  </div>
                  <small v-if="row.platform_error" class="error-preview" :title="row.platform_error">{{ row.platform_error }}</small>
                </td>
                <td class="actions">
                  <button v-if="row.status === 'awaiting_confirmation'" class="text-button" :disabled="busyId === row.id" @click="confirmSchedule(row)">确认排期</button>
                  <button v-if="row.status === 'scheduled' && row.platform_status === 'not_uploaded' && !row.auto_publish" class="text-button" :disabled="busyId === row.id" @click="prepareCreatorUpload(row)">上传视频并关联商品（之后发布）</button>
                  <div v-else-if="row.status === 'scheduled' && row.platform_status === 'not_uploaded' && row.auto_publish" class="immediate-actions">
                    <button
                      class="text-button publish-action"
                      :disabled="busyId === row.id || busyId === -row.account_id || Boolean(conflictingPublishTask(row)) || accounts.find((item) => item.id === row.account_id)?.status !== 'active'"
                      :title="conflictingPublishTask(row) ? `同账号同商品存在任务 #${conflictingPublishTask(row)?.id} 待核查` : ''"
                      @click="publishImmediately(row)"
                    >{{ busyId === row.id ? '发布中…' : '马上发布' }}</button>
                  </div>
                  <button v-if="row.platform_status === 'uploaded_waiting_publish'" class="text-button publish-action" :disabled="busyId === row.id || busyId === -row.account_id" @click="submitPublish(row)">{{ busyId === row.id ? '提交中…' : '发布' }}</button>
                  <button v-if="canRetryManually(row) && row.platform_status !== 'uploaded_waiting_publish'" class="text-button publish-action" :disabled="busyId !== null || progressForRow(row)?.state === 'running' || accounts.find((item) => item.id === row.account_id)?.status !== 'active'" @click="retryManually(row)">{{ busyId === row.id ? '重试中…' : '检查并重试' }}</button>
                  <details class="row-more-actions">
                    <summary>详情与更多</summary>
                    <div class="row-more-content">
                      <small>任务 #{{ row.id }} · 商品 ID {{ row.product_id }}</small>
                      <small>视频备注：{{ row.title }} · 文件：{{ row.video_path }}</small>
                      <small v-if="row.execution_started_at">实际开始：{{ formatSchedule(row.execution_started_at) }}</small>
                      <small v-if="row.description">简介：{{ row.description }}</small>
                      <small v-if="row.platform_error">原因：{{ row.platform_error }}</small>
                      <button v-if="row.status === 'scheduled'" class="text-button" :disabled="busyId === -row.account_id" @click="openCreatorCenter(row.account_id)">打开创作者中心</button>
                      <button v-if="row.platform_status === 'uploaded_waiting_publish'" class="text-button" :disabled="busyId === row.id" @click="markPublished(row)">我已在抖音发布（仅记录）</button>
                      <button v-if="row.platform_status === 'needs_review' || row.platform_status === 'uploading'" class="text-button danger" :disabled="busyId === row.id" @click="resetUncertainUpload(row)">核实未上传后重置</button>
                      <button v-if="row.platform_status === 'submission_uncertain' || row.platform_status === 'submitting'" class="text-button" :disabled="busyId === row.id" @click="markPublished(row)">我已确认发布（仅记录）</button>
                      <button v-if="row.platform_status === 'submission_uncertain' || row.platform_status === 'submitting'" class="text-button danger" :disabled="busyId === row.id" @click="resetUncertainSubmission(row)">核实未发布后重置</button>
                      <button v-if="row.platform_status === 'uploaded_waiting_publish' && canRetryManually(row)" class="text-button" :disabled="busyId !== null" @click="retryManually(row)">恢复原投稿</button>
                      <button v-if="canRetryManually(row) && row.platform_error" class="text-button" :disabled="busyId !== null" @click="retryManually(row, true)">清除失败提示并重试</button>
                      <button v-if="row.screenshot_path" class="text-button" @click="openReceiptScreenshot(row)">查看失败截图</button>
                      <button class="text-button" @click="viewReceiptEvents(row)">过程日志</button>
                      <button v-if="row.status !== 'cancelled' && (row.platform_status === 'not_uploaded' || row.platform_status === 'isolated')" class="text-button danger" :disabled="busyId === row.id" @click="cancelTask(row)">取消计划</button>
                      <button v-if="row.platform_status === 'not_uploaded' && ['awaiting_confirmation', 'scheduled', 'cancelled'].includes(row.status)" class="text-button danger" :disabled="busyId === row.id || progressForRow(row)?.state === 'running'" @click="deleteTask(row)">删除计划</button>
                    </div>
                  </details>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <div v-else class="empty account-empty">当前没有账号记录。请先在账号管理中添加并启用账号。</div>
    <Teleport to="body">
      <section v-if="eventQueueId !== null" class="receipt-events-mask" @click.self="eventQueueId = null" @keydown.esc.window="eventQueueId = null">
        <div class="receipt-events" role="dialog" aria-modal="true" :aria-label="`任务 #${eventQueueId} 过程日志`">
          <div class="card-title"><h3>任务 #{{ eventQueueId }} 过程日志</h3><button class="button" @click="eventQueueId = null">关闭</button></div>
          <p v-if="!receiptEvents.length">暂无上传或发布事件。</p>
          <div v-for="event in receiptEvents" :key="event.id" class="receipt-event">
            <b>{{ event.stage }} · {{ event.outcome }}</b><span>{{ formatSchedule(event.created_at) }}</span>
            <small v-if="event.reason">{{ event.reason }}{{ event.cleared_at ? '（失败提示已清除，历史留存）' : '' }}</small>
            <small v-if="event.screenshot_path">截图：{{ event.screenshot_path }}</small>
          </div>
        </div>
      </section>
    </Teleport>
  </section>
</template>

<style scoped>
.publish-page { flex: 1; min-width: 0; min-height: 0; overflow: auto; overscroll-behavior-y: contain; overflow-anchor: none; scrollbar-gutter: stable; padding: 2px 3px 14px; color: var(--text); }
.page-head { position: sticky; top: 0; z-index: 12; display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; padding: 8px 4px; border-bottom: 1px solid var(--border); background: var(--bg-app); }
.head-actions { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.header-more { position: relative; font-size: 12px; }
.header-more > summary { cursor: pointer; padding: 7px 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.header-more-actions { position: absolute; right: 0; top: calc(100% + 5px); z-index: 15; display: flex; flex-direction: column; align-items: stretch; gap: 6px; min-width: 160px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); box-shadow: var(--shadow-card); }
.account-picker { display: flex; flex-direction: row; align-items: center; gap: 5px; white-space: nowrap; }
.account-picker select { width: auto; min-width: 130px; max-width: 180px; }
.review-toggle.enabled { border-color: #86efac; background: #f0fdf4; color: #166534; }
.auto-publish-state { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; margin-bottom: 9px; padding: 9px 12px; border: 1px solid #86efac; border-radius: var(--radius); background: #f0fdf4; color: #166534; font-size: 12px; line-height: 1.5; }
.auto-publish-state.paused { border-color: #fcd34d; background: #fffbeb; color: #92400e; }
.auto-publish-state span { overflow-wrap: anywhere; }
.auto-publish-toggle { margin-left: auto; flex: 0 0 auto; }
.scheduler-settings { margin: 0 0 7px; padding: 7px 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); font-size: 12px; scroll-margin-top: 70px; }
.scheduler-settings summary { cursor: pointer; color: var(--text); font-weight: 600; }
.scheduler-settings-body { display: grid; grid-template-columns: repeat(3, minmax(130px, 1fr)) auto; align-items: end; gap: 10px; margin-top: 10px; }
.scheduler-settings-body small { grid-column: 1 / -1; color: var(--text-muted); line-height: 1.5; }
.workflow-help { margin: 0 0 7px; color: var(--text-muted); font-size: 12px; }
.workflow-help summary { width: fit-content; cursor: pointer; }
.workflow-help p { margin: 6px 0 0; }
.receipt-events-mask { position: fixed; inset: 0; z-index: 1000; display: grid; place-items: center; padding: 20px; background: rgb(15 23 42 / 46%); }
.receipt-events { width: min(760px, 95vw); max-height: 80vh; overflow: auto; padding: 16px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg-panel); box-shadow: 0 18px 56px rgb(15 23 42 / 22%); }
.receipt-events .card-title { align-items: center; justify-content: space-between; }
.receipt-event { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 7px 0; border-top: 1px solid var(--border); font-size: 12px; }
.receipt-event small { flex-basis: 100%; color: var(--text-muted); overflow-wrap: anywhere; }
.page-head h1 { margin: 0; font-size: 21px; }
.page-head p, .card-title span, .summary-note, .form-actions > span { color: var(--text-muted); font-size: 12px; }
.page-head p { margin: 5px 0 0; }
.notice { display: flex; flex-wrap: wrap; gap: 7px; padding: 10px 12px; border: 1px solid #bfdbfe; border-radius: var(--radius); background: #eff6ff; color: #1d4ed8; font-size: 12px; line-height: 1.5; }
.workflow-note { display: grid; grid-template-columns: 96px minmax(0, 1fr); align-items: start; gap: 5px 10px; }
.notice.error { border-color: #fecaca; background: #fef2f2; color: #b91c1c; }
.notice.result { margin-top: 9px; }
.publish-progress-card { margin: 9px 0; padding: 11px 13px; border: 1px solid #93c5fd; border-radius: var(--radius); background: #eff6ff; color: #1e3a8a; }
.publish-progress-card.succeeded { border-color: #86efac; background: #f0fdf4; color: #166534; }
.publish-progress-card.failed { border-color: #fecaca; background: #fef2f2; color: #991b1b; }
.publish-progress-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; font-size: 12px; }
.publish-progress-head span, .publish-progress-card small, .publish-progress-message { color: var(--text-muted); font-size: 11px; }
.publish-progress-stage { margin-top: 6px; font-size: 13px; font-weight: 600; }
.publish-progress-message { margin: 5px 0 0; line-height: 1.45; overflow-wrap: anywhere; }
.progress-track { position: relative; height: 6px; margin-top: 7px; overflow: hidden; border-radius: 5px; background: rgba(100, 116, 139, .2); }
.progress-fill { display: block; height: 100%; border-radius: inherit; background: var(--brand); transition: width .25s ease; }
.succeeded .progress-fill { background: #16a34a; }
.progress-indeterminate { position: absolute; top: 0; bottom: 0; width: 36%; border-radius: inherit; background: var(--brand); animation: publish-progress-slide 1.4s ease-in-out infinite; }
.row-publish-progress { display: grid; gap: 4px; min-width: 120px; margin-top: 6px; }
.row-publish-progress small { color: var(--text-muted); font-size: 10px; line-height: 1.35; overflow-wrap: anywhere; }
@keyframes publish-progress-slide { from { left: -38%; } to { left: 105%; } }
.account-workspace { min-width: 0; }
.account-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 12px; padding: 13px 15px; border: 1px solid var(--border); border-radius: var(--radius); background: linear-gradient(110deg, #f6f9ff, #ffffff); box-shadow: var(--shadow-card); }
.account-head h2 { margin: 0; font-size: 16px; }
.account-head p { margin: 5px 0 0; color: var(--text-muted); font-size: 12px; }
.account-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.account-empty { margin-top: 14px; border: 1px dashed var(--border); border-radius: var(--radius); }
.summary { display: grid; grid-template-columns: 130px 150px 1fr; align-items: center; gap: 14px; margin: 10px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.summary > div:not(.summary-note) { display: flex; align-items: baseline; gap: 8px; }
.summary b { color: var(--brand); font-size: 21px; }
.summary span { color: var(--text-muted); font-size: 12px; }
.card { margin-top: 10px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); box-shadow: var(--shadow-card); }
.form-card { scroll-margin-top: 90px; }
.card-title { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.card-title h2 { margin: 0; font-size: 16px; }
.queue-pagination { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 6px; margin: 0 0 9px; color: var(--text-muted); font-size: 12px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 16px; }
label { display: flex; flex-direction: column; gap: 5px; color: var(--text-muted); font-size: 12px; }
label.wide { grid-column: 1 / -1; }
input, select, textarea { width: 100%; min-width: 0; padding: 0 9px; border: 1px solid var(--border); border-radius: 5px; background: var(--bg-panel); color: var(--text); }
input, select { height: 34px; }
textarea { min-height: 66px; padding: 8px 9px; resize: vertical; font: inherit; }
.file-pick { display: flex; align-items: center; gap: 9px; }
.file-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 9px; border: 1px solid var(--border); border-radius: 5px; color: var(--text); }
.product-link { color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }
.button { padding: 7px 12px; border: 1px solid var(--border); border-radius: 5px; background: var(--bg-panel); color: var(--text); cursor: pointer; white-space: nowrap; }
.button:disabled { opacity: .55; cursor: default; }
.button.primary { border-color: var(--brand); background: var(--brand); color: white; }
.form-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.empty { padding: 30px 12px; text-align: center; color: var(--text-muted); font-size: 13px; }
.table-wrap { overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 9px 8px; border-top: 1px solid var(--border); text-align: left; vertical-align: middle; }
th { color: var(--text-muted); font-weight: 500; white-space: nowrap; }
.date-cell { white-space: nowrap; }
.id-cell { min-width: 190px; max-width: 340px; }
.id-cell b, .id-cell small, .platform-cell small { display: block; }
.id-cell b, .id-cell small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.id-cell small, .platform-cell small { margin-top: 3px; color: var(--text-muted); }
.platform-cell { min-width: 170px; max-width: 280px; }
.platform-cell small { max-width: 260px; overflow-wrap: anywhere; }
.platform-cell .badge + .badge { margin-left: 5px; }
.error-preview { display: -webkit-box !important; overflow: hidden; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.badge { display: inline-block; padding: 3px 7px; border-radius: 10px; background: var(--brand-soft); color: var(--brand); white-space: nowrap; }
.badge.awaiting_confirmation { background: #fff7ed; color: #c2410c; }
.badge.cancelled { background: #f3f4f6; color: var(--text-muted); }
.badge.uploading, .badge.needs_review { background: #fff7ed; color: #c2410c; }
.badge.submitting, .badge.submission_uncertain { background: #fff7ed; color: #c2410c; }
.badge.isolated { background: #fff7ed; color: #9a6700; }
.badge.uploaded_waiting_publish, .badge.submitted_review { background: #eff6ff; color: #1d4ed8; }
.badge.published { background: #ecfdf5; color: #047857; }
.publish-action { color: #1d4ed8; font-weight: 700; }
.immediate-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 5px; }
.actions { min-width: 150px; white-space: normal; }
.row-more-actions { margin-top: 5px; font-size: 11px; }
.row-more-actions summary { width: fit-content; min-height: 26px; color: var(--brand); cursor: pointer; }
.row-more-content { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; max-width: 360px; padding: 7px; border: 1px solid var(--border); border-radius: 5px; background: var(--bg-panel); }
.row-more-content small { max-width: 100%; overflow-wrap: anywhere; color: var(--text-muted); }
.text-button { padding: 3px 5px; border: 0; background: transparent; color: var(--brand); cursor: pointer; }
.text-button:disabled { opacity: .5; cursor: default; }
.text-button.danger { color: #dc2626; }
.muted { color: var(--text-muted); }
tr.cancelled { color: var(--text-muted); }
@media (max-width: 900px) { .page-head { align-items: flex-start; flex-direction: column; } .account-head { align-items: flex-start; flex-direction: column; } }
@media (max-width: 700px) { .form-grid { grid-template-columns: 1fr; } label.wide { grid-column: auto; } .form-actions { align-items: flex-start; flex-direction: column; } .scheduler-settings-body { grid-template-columns: 1fr; } }
</style>
