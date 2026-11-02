<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { notifyGlobal } from '../utils/notifications'

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
  platform_updated_at: string | null
  published_at: string | null
  created_at: string
  updated_at: string
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
const publishProgress = ref<PublishProgressPayload | null>(null)
const manualRetryNoDraftIds = ref<Record<number, boolean>>({})
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
const quarantinedAutoTasks = computed(() => rows.value.filter((row) =>
  Boolean(row.auto_publish) &&
  ['uploading', 'uploaded_waiting_publish', 'needs_review', 'submitting', 'submission_uncertain'].includes(row.platform_status)
))
const activeCount = computed(() => accountRows.value.filter((row) => row.status !== 'cancelled').length)
const pendingCount = computed(() => accountRows.value.filter((row) => row.status === 'awaiting_confirmation').length)
const videoName = computed(() => form.value.videoPath.split(/[\\/]/).filter(Boolean).pop() ?? '')
const accountProducts = computed(() => products.value.filter((product) => product.account_id === activeAccountId.value))
const selectedProduct = computed(() => accountProducts.value.find((product) => product.product_id === form.value.productId) ?? null)

function accountPlanCount(accountId: number): number {
  return rows.value.filter((row) => row.account_id === accountId && row.status !== 'cancelled').length
}

function selectAccount(accountId: number): void {
  if (accountTabs.value.some((account) => account.id === accountId)) {
    activeAccountId.value = accountId
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
    schedulerSettings.jitterMinSeconds = Number(publishSettings.publishJitterMinSeconds ?? 5)
    schedulerSettings.jitterMaxSeconds = Number(publishSettings.publishJitterMaxSeconds ?? 15)
    schedulerSettings.catchupMaxLateHours = Number(publishSettings.publishCatchupMaxLateHours ?? 0)
    for (const account of accounts.value) {
      if (!forms[account.id]) forms[account.id] = createForm()
    }
    if (!accountTabs.value.some((account) => account.id === activeAccountId.value)) {
      activeAccountId.value = accountTabs.value.find((account) => account.status === 'active')?.id ?? accountTabs.value[0]?.id ?? 0
    }
    for (const account of accounts.value) {
      const accountId = account.id
      const accountForm = forms[accountId]
      const selected = products.value.find((product) => product.account_id === accountId && product.product_id === accountForm.productId)
      if (selected && !accountForm.description.trim()) accountForm.description = selected.product_name
    }
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
  if (autoPublishEnabled.value || quarantinedAutoTasks.value.length > 0) return ''
  try {
    const result = await runCli(['auto-settings', 'set', '--enabled', 'true'])
    if (result.ok !== true) {
      return `自动发布暂未恢复：${String(result.error ?? '仍有任务需要核查')}`
    }
    autoPublishEnabled.value = result.autoPublishEnabled === true
    return autoPublishEnabled.value ? '全自动发布已恢复，新任务将继续按队列执行。' : ''
  } catch (error) {
    return `自动发布暂未恢复：${String(error)}`
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
    noticeError.value = false
    notice.value = '已保存本机调度设置；0 小时表示所有逾期计划都按顺延规则补发。'
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
  if (quarantinedAutoTasks.value.length > 0) {
    noticeError.value = true
    notice.value = '还有自动发布任务处于待核查或提交中状态；先处理待核查任务，避免重复发布。'
    return
  }
  if (!window.confirm(
    `将立即把“${row.product_name}”的计划时间调整为现在，并按${row.account_name}今日配额重新校验；随后上传视频、关联商品并提交抖音发布。若存在待核查任务，系统会拒绝执行。确认马上发布？`
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

async function retryManually(row: QueueRow): Promise<void> {
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
    if (row.auto_publish) {
      confirmation.push('原任务开启了自动发布，本次恢复成功后可能点击最终“发布”。')
    } else {
      confirmation.push('原任务未开启自动发布，本次会停在发布前，由你手动点击本条“发布”。')
    }
    if (!window.confirm(`仅重试“${row.product_name}”（任务 #${row.id}），不启动其他隔离或待核查任务。\n\n${confirmation.join('\n')}\n\n确认继续？`)) return

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
    if (
      prepared.isolated === true &&
      prepared.classification === 'PUBLISH_VERIFY_OTHER_ROW_ISOLATED' &&
      String(prepared.productId ?? '') === String(task.productId ?? row.product_id)
    ) {
      const warning = String(prepared.warning ?? prepared.error ?? '百应商品核验重查耗尽，本任务已隔离；未上传视频。')
      const isolated = await runCli(['upload-isolate', '--id', String(row.id), '--error', warning])
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
    if (
      submitted.isolated === true &&
      submitted.classification === 'CREATOR_COVER_REQUIRED_ISOLATED' &&
      Number(submitted.queueId) === row.id &&
      String(submitted.productId ?? '') === row.product_id
    ) {
      const warning = String(submitted.warning ?? submitted.error ?? '平台要求封面，当前任务已隔离；未记录为发布成功。')
      const isolated = await runCli(['submit-isolate', '--id', String(row.id), '--error', warning])
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
        'submit-finish', '--id', String(row.id), '--status', failedStatus, '--error', message
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
          'submit-finish', '--id', String(row.id), '--status', uncertainStatus, '--error', message
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
    const result = await runCli([action, '--id', String(id)])
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
  refreshTimer = window.setInterval(() => { if (!loading.value) void refresh() }, 15_000)
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
        <h1>抖音发布管理</h1>
        <p>选品生成的视频默认全自动发布；此页面保留人工发布入口，供手动计划和异常核查使用。</p>
      </div>
      <div class="head-actions">
        <button class="button" :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '刷新队列' }}</button>
      </div>
    </header>

    <div class="auto-publish-state" :class="{ paused: !autoPublishEnabled }">
      <strong>{{ autoPublishEnabled ? '全自动发布已开启' : '全自动发布当前已暂停' }}</strong>
      <span v-if="quarantinedAutoTasks.length > 0">
        待核查任务 {{ quarantinedAutoTasks.length }} 条：{{ quarantinedAutoTasks.map((row) => `#${row.id} ${row.product_name}`).join('、') }}。这些任务不会自动重试或标记成功，已与后续任务隔离。
      </span>
      <span v-if="autoPublishEnabled">客户端运行期间，其他符合条件的到期任务会继续按队列执行；单项失败只隔离该任务。</span>
      <span v-else>全自动发布当前为暂停状态；待核查任务已隔离，恢复自动发布后不会重试这些任务。</span>
    </div>

    <details class="scheduler-settings">
      <summary>定时调度与逾期补发设置</summary>
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
        <small>调度器由本机主进程运行：每轮检查约间隔 10 秒，并额外等待设定范围内的随机时间。逾期期限填 0 表示不限；超过期限的计划会标记为已取消并保留原因，不会自动触发平台操作。</small>
      </div>
    </details>

    <div class="notice workflow-note">
      <strong>全自动发布</strong>
      <span>选品完成后，每个视频完成图转视频并入队即按商品逐个上传、关联商品并提交。客户端关闭期间错过的任务会在下次启动时补查；配额不足时按顺延时间执行；单项异常会隔离并记录，后续合格任务继续执行，待核查任务不会自动重试或标记成功。</span>
      <strong>人工发布</strong>
      <span>保存本地计划 → 确认排期 → 上传视频并关联商品（此时还未发布） → 检查后点击“发布”，客户端才会向抖音提交。</span>
    </div>

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

    <nav v-if="accountTabs.length" class="account-tabs" role="tablist" aria-label="发布账号">
      <button
        v-for="account in accountTabs"
        :key="account.id"
        class="account-tab"
        :class="{ active: activeAccountId === account.id }"
        role="tab"
        :aria-selected="activeAccountId === account.id"
        @click="selectAccount(account.id)"
      >
        <span>{{ account.name }}</span>
        <small>{{ account.status === 'active' ? '已启用' : '已停用' }} · {{ accountPlanCount(account.id) }} 条有效计划</small>
      </button>
    </nav>

    <section v-if="activeAccount" class="account-workspace" role="tabpanel">
      <header class="account-head">
        <div>
          <h2>{{ activeAccount.name }}</h2>
          <p>当前工作区只显示本账号商品和发布记录，上传使用本账号专属 Chrome profile。</p>
        </div>
        <button class="button primary" :disabled="activeAccount.status !== 'active' || busyId === -activeAccount.id" @click="openCreatorCenter(activeAccount.id)">
          {{ busyId === -activeAccount.id ? '正在打开…' : '打开此账号创作者中心' }}
        </button>
      </header>
      <p v-if="activeAccount.status !== 'active'" class="hint">此账号已停用；队列记录仍可查看，启用后才能创建或处理平台任务。</p>

      <div class="summary">
        <div><b>{{ activeCount }}</b><span>本账号有效计划</span></div>
        <div><b>{{ pendingCount }}</b><span>待确认本地计划</span></div>
        <div class="summary-note">每日配额按本账号设置或通用默认值校验；待确认和已排期任务都会占用该账号的计划日期配额。</div>
      </div>

      <div class="card form-card">
        <div class="card-title">
          <h2>添加 {{ activeAccount.name }} 视频计划</h2>
          <span>仅使用该账号已加车商品；选择图转视频生成的 MP4 文件</span>
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
        </div>
        <div v-if="accountRows.length === 0" class="empty">{{ activeAccount.name }}还没有本地发布计划。请在上方选择该账号已加车商品和视频。</div>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr><th>计划时间</th><th>商品</th><th>视频备注 / 文件</th><th>本地计划状态</th><th>抖音平台状态</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in accountRows" :key="row.id" :class="{ cancelled: row.status === 'cancelled' }">
                <td class="date-cell">{{ formatSchedule(row.schedule_at) }}</td>
                <td class="id-cell"><b>{{ row.product_name || '商品名称未保存' }}</b><small>{{ row.product_id }}</small></td>
                <td class="file-cell" :title="row.video_path"><b>{{ row.title }}</b><small>{{ row.video_path.split(/[\\/]/).pop() }}</small></td>
                <td><span class="badge" :class="row.status">{{ statusLabel(row.status) }}</span></td>
                <td class="platform-cell">
                  <span class="badge" :class="row.platform_status">{{ platformStatusLabel(row.platform_status) }}</span>
                  <div v-if="progressForRow(row)" class="row-publish-progress">
                    <small>{{ progressForRow(row)?.stage }}</small>
                    <div v-if="progressForRow(row)?.state === 'running'" class="progress-track" role="progressbar" :aria-valuenow="progressForRow(row)?.percent" :aria-valuemin="0" :aria-valuemax="100">
                      <span v-if="typeof progressForRow(row)?.percent === 'number'" class="progress-fill" :style="{ width: `${progressForRow(row)?.percent}%` }"></span>
                      <span v-else class="progress-indeterminate"></span>
                    </div>
                    <small v-if="typeof progressForRow(row)?.percent === 'number'">当前阶段 {{ progressForRow(row)?.percent }}%</small>
                    <small v-if="progressForRow(row)?.message">{{ progressForRow(row)?.message }}</small>
                  </div>
                  <small v-if="row.platform_error">{{ row.platform_error }}</small>
                <small v-else-if="row.platform_status === 'uploaded_waiting_publish'">简介：{{ row.description }}</small>
                <small v-else-if="row.platform_status === 'submitted_review'">平台已收件，审核结果由抖音创作者中心更新。</small>
                </td>
                <td class="actions">
                  <button v-if="row.status === 'awaiting_confirmation'" class="text-button" :disabled="busyId === row.id" @click="confirmSchedule(row)">确认排期</button>
                  <button v-if="row.status === 'scheduled' && row.platform_status === 'not_uploaded' && !row.auto_publish" class="text-button" :disabled="busyId === row.id" @click="prepareCreatorUpload(row)">上传视频并关联商品（之后发布）</button>
                  <div v-else-if="row.status === 'scheduled' && row.platform_status === 'not_uploaded' && row.auto_publish" class="immediate-actions">
                    <span class="muted">{{ quarantinedAutoTasks.length > 0 ? '待核查任务已隔离' : autoPublishEnabled ? '等待计划时间' : '自动发布暂停中' }}</span>
                    <button
                      class="text-button publish-action"
                      :disabled="busyId === row.id || busyId === -row.account_id || quarantinedAutoTasks.length > 0 || accounts.find((item) => item.id === row.account_id)?.status !== 'active'"
                      :title="quarantinedAutoTasks.length > 0 ? '存在自动发布待核查任务；马上发布仍要求先完成核查' : ''"
                      @click="publishImmediately(row)"
                    >{{ busyId === row.id ? '发布中…' : '马上发布' }}</button>
                  </div>
                  <button v-if="row.platform_status === 'uploaded_waiting_publish'" class="text-button" :disabled="busyId === -row.account_id" @click="openCreatorCenter(row.account_id)">打开创作者中心</button>
                  <button v-if="row.platform_status === 'uploaded_waiting_publish'" class="text-button publish-action" :disabled="busyId === row.id || busyId === -row.account_id" @click="submitPublish(row)">{{ busyId === row.id ? '提交中…' : '发布' }}</button>
                  <button v-if="row.platform_status === 'uploaded_waiting_publish'" class="text-button" :disabled="busyId === row.id" @click="markPublished(row)">我已在抖音发布（仅记录）</button>
                  <button v-if="row.platform_status === 'needs_review' || row.platform_status === 'uploading'" class="text-button" :disabled="busyId === -row.account_id" @click="openCreatorCenter(row.account_id)">检查创作者中心</button>
                  <button v-if="row.platform_status === 'needs_review' || row.platform_status === 'uploading'" class="text-button danger" :disabled="busyId === row.id" @click="resetUncertainUpload(row)">核实未上传后重试</button>
                  <button v-if="row.platform_status === 'submission_uncertain' || row.platform_status === 'submitting'" class="text-button" :disabled="busyId === -row.account_id" @click="openCreatorCenter(row.account_id)">检查创作者中心</button>
                  <button v-if="row.platform_status === 'submission_uncertain' || row.platform_status === 'submitting'" class="text-button" :disabled="busyId === row.id" @click="markPublished(row)">我已确认发布（仅记录）</button>
                  <button v-if="row.platform_status === 'submission_uncertain' || row.platform_status === 'submitting'" class="text-button danger" :disabled="busyId === row.id" @click="resetUncertainSubmission(row)">核实未发布后重试</button>
                  <button v-if="canRetryManually(row)" class="text-button publish-action" :disabled="busyId !== null || progressForRow(row)?.state === 'running' || accounts.find((item) => item.id === row.account_id)?.status !== 'active'" title="仅重试本条；先恢复已有投稿，必要时再核实无草稿后重传" @click="retryManually(row)">{{ busyId === row.id ? '重发中…' : '手动重新发布' }}</button>
                  <button v-if="row.status !== 'cancelled' && (row.platform_status === 'not_uploaded' || row.platform_status === 'isolated')" class="text-button danger" :disabled="busyId === row.id" @click="cancelTask(row)">取消</button>
                  <button v-if="row.platform_status === 'not_uploaded' && ['awaiting_confirmation', 'scheduled', 'cancelled'].includes(row.status)" class="text-button danger" :disabled="busyId === row.id || progressForRow(row)?.state === 'running'" @click="deleteTask(row)">删除计划</button>
                  <span v-if="row.status !== 'cancelled' && row.platform_status === 'uploaded_waiting_publish'" class="muted">视频已上传，点击“发布”才会提交</span>
                  <span v-if="row.status !== 'cancelled' && row.platform_status === 'submitting'" class="muted">发布命令已启动，请勿重复操作</span>
                  <span v-if="row.status !== 'cancelled' && row.platform_status === 'published'" class="muted">已收到抖音发布成功回执</span>
                  <span v-if="row.status !== 'cancelled' && row.platform_status === 'submitted_review'" class="muted">抖音已接收，等待平台审核</span>
                  <span v-if="row.status !== 'cancelled' && row.platform_status === 'submission_uncertain'" class="muted">请核对创作者中心后再处理</span>
                    <span v-if="row.status !== 'cancelled' && row.platform_status === 'isolated'" class="muted">本项已隔离，未记录为发布成功；查看错误说明后再处理。</span>
                  <span v-if="row.status !== 'cancelled' && (row.platform_status === 'needs_review' || row.platform_status === 'uploading')" class="muted">上传状态需要人工核对</span>
                  <span v-if="row.status === 'cancelled'" class="muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <div v-else class="empty account-empty">当前没有账号记录。请先在账号管理中添加并启用账号。</div>
  </section>
</template>

<style scoped>
.publish-page { flex: 1; min-width: 0; min-height: 0; overflow: auto; padding: 2px 3px 14px; color: var(--text); }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.auto-publish-state { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; margin-bottom: 9px; padding: 9px 12px; border: 1px solid #86efac; border-radius: var(--radius); background: #f0fdf4; color: #166534; font-size: 12px; line-height: 1.5; }
.auto-publish-state.paused { border-color: #fcd34d; background: #fffbeb; color: #92400e; }
.auto-publish-state span { overflow-wrap: anywhere; }
.scheduler-settings { margin: 0 0 9px; padding: 9px 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); font-size: 12px; }
.scheduler-settings summary { cursor: pointer; color: var(--text); font-weight: 600; }
.scheduler-settings-body { display: grid; grid-template-columns: repeat(3, minmax(130px, 1fr)) auto; align-items: end; gap: 10px; margin-top: 10px; }
.scheduler-settings-body small { grid-column: 1 / -1; color: var(--text-muted); line-height: 1.5; }
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
.account-tabs { display: flex; gap: 8px; margin-top: 12px; padding: 0 0 8px; border-bottom: 1px solid var(--border); }
.account-tab { display: flex; flex-direction: column; gap: 4px; min-width: 190px; padding: 9px 13px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-panel); color: var(--text); text-align: left; cursor: pointer; }
.account-tab.active { border-color: var(--brand); box-shadow: inset 0 -2px 0 var(--brand); color: var(--brand); }
.account-tab span { font-size: 13px; font-weight: 600; }
.account-tab small { color: var(--text-muted); font-size: 11px; }
.account-workspace { min-width: 0; }
.account-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 12px; padding: 13px 15px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.account-head h2 { margin: 0; font-size: 16px; }
.account-head p { margin: 5px 0 0; color: var(--text-muted); font-size: 12px; }
.account-empty { margin-top: 14px; border: 1px dashed var(--border); border-radius: var(--radius); }
.summary { display: grid; grid-template-columns: 130px 150px 1fr; align-items: center; gap: 14px; margin: 10px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.summary > div:not(.summary-note) { display: flex; align-items: baseline; gap: 8px; }
.summary b { color: var(--brand); font-size: 21px; }
.summary span { color: var(--text-muted); font-size: 12px; }
.card { margin-top: 10px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.card-title { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.card-title h2 { margin: 0; font-size: 16px; }
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
.date-cell, .id-cell { white-space: nowrap; }
.id-cell b, .id-cell small, .platform-cell small { display: block; }
.id-cell small, .platform-cell small { margin-top: 3px; color: var(--text-muted); white-space: normal; }
.platform-cell { min-width: 150px; max-width: 260px; }
.platform-cell small { max-width: 260px; overflow-wrap: anywhere; }
.file-cell { min-width: 230px; max-width: 420px; }
.file-cell b, .file-cell small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-cell small { margin-top: 3px; color: var(--text-muted); }
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
.actions { display: flex; min-width: 220px; flex-wrap: wrap; align-items: center; gap: 3px 5px; white-space: normal; }
.text-button { padding: 3px 5px; border: 0; background: transparent; color: var(--brand); cursor: pointer; }
.text-button:disabled { opacity: .5; cursor: default; }
.text-button.danger { color: #dc2626; }
.muted { color: var(--text-muted); }
tr.cancelled { color: var(--text-muted); }
@media (max-width: 900px) { .summary { grid-template-columns: 120px 130px; } .summary-note { grid-column: 1 / -1; } .account-head { align-items: flex-start; flex-direction: column; } }
@media (max-width: 700px) { .form-grid { grid-template-columns: 1fr; } label.wide { grid-column: auto; } .form-actions { align-items: flex-start; flex-direction: column; } .workflow-note { grid-template-columns: 1fr; } .scheduler-settings-body { grid-template-columns: 1fr; } }
</style>
