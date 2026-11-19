<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useAppRefresh } from '../utils/app_refresh'

interface Target {
  account_id: number
  account_name: string
  account_status: string
  qianchuan_account_id: number | null
  qc_name: string | null
  qc_id: string | null
}
interface PlanCandidate {
  publishQueueId: number
  accountId: number
  qianchuanAccountId: number
  advertiserId: string
  productId: string
  productName: string
  publishedVideoTitle: string
  dailyBudgetYuan: number
  commissionRate: number
  roiTarget: number
  videoSource: string
  materialCount: number
  remoteMaterialIds: string[]
  existingPlanId: number
  existingPlanStatus: string
  canCreate: boolean
  canSchedule: boolean
  scheduleState?: string
  scheduledAt?: string
  reason: string
}
interface PlanJob {
  id: number
  publish_queue_id: number
  submit_attempted: number
  scheduled_at: string
  schedule_state: string
  account_id: number
  qianchuan_account_id: number
  advertiser_id: string
  product_id: string
  product_name: string
  plan_name: string
  creation_source?: string
  daily_budget_yuan: number
  material_ids_json: string
  selected_material_ids_json: string
  status: string
  remote_plan_id: string
  commission_rate: number
  roi_target: number
  reason: string
  screenshot_path: string
}
interface PlanSupplement {
  id: number
  plan_job_id: number
  account_id: number
  qianchuan_account_id: number
  status: string
  scheduled_at: string
  reason: string
}

const emit = defineEmits<{ (event: 'open-qianchuan-accounts'): void }>()
const targets = ref<Target[]>([])
const accountId = ref(0)
const qianchuanAccountId = ref(0)
const candidates = ref<PlanCandidate[]>([])
const plans = ref<PlanJob[]>([])
const planTotal = ref(0)
const planPage = ref(1)
const planPageSize = 20
const planStatus = ref('')
const supplements = ref<PlanSupplement[]>([])
const loading = ref(false)
const busy = ref(false)
const message = ref('')
const refreshedAt = ref('')
const settingsDirty = ref(false)
interface ScheduleSummary {
  publish_queue_id: number
  qianchuan_account_id: number
  product_id: string
  advertiser_id: string
  daily_budget_yuan: number
  commission_rate: number
  roi_target: number
}
const scheduleDialog = ref<{ snapshot: ScheduleSummary; confirmationJson: string; productName: string; accountName: string; qcName: string } | null>(null)
const scheduleTime = ref('')
const scheduleError = ref('')
const scheduleMinimum = ref('')
const settings = reactive({
  enabled: false,
  dailyBudgetYuan: 0,
  videoSource: 'uploaded',
  longRunning: true,
  smartCoupon: true,
  aigcEnabled: false
})

const activeAccounts = computed(() => [...new Map(targets.value
  .filter(row => row.qianchuan_account_id && row.account_status === 'active')
  .map(row => [row.account_id, { id: row.account_id, name: row.account_name }])).values()])
const linkedTargets = computed(() => targets.value.filter(row =>
  row.account_id === accountId.value && row.qianchuan_account_id && row.account_status === 'active'))
const selectedBinding = computed(() => linkedTargets.value.find(row => row.qianchuan_account_id === qianchuanAccountId.value))
const selectedAccountName = computed(() => activeAccounts.value.find(row => row.id === accountId.value)?.name ?? `账号 ${accountId.value}`)
const selectedQcName = computed(() => selectedBinding.value?.qc_name || selectedBinding.value?.qc_id || `千川账户 ${qianchuanAccountId.value}`)
const visiblePlans = computed(() => plans.value.filter(row =>
  row.account_id === accountId.value && row.qianchuan_account_id === qianchuanAccountId.value))
const planPageCount = computed(() => Math.max(1, Math.ceil(planTotal.value / planPageSize)))
const visibleSupplements = computed(() => supplements.value.filter(row =>
  row.account_id === accountId.value && row.qianchuan_account_id === qianchuanAccountId.value))

function rows<T>(data: Record<string, unknown>): T[] { return Array.isArray(data.rows) ? data.rows as T[] : [] }
function checked(data: Record<string, unknown>): Record<string, unknown> {
  if (data.ok === false) throw new Error(String(data.error ?? '操作失败'))
  return data
}
function statusText(status: string): string {
  return ({ ready: '待发布', submitting: '发布中', published: '已发布', blocked: '发布前停止', needs_review: '待核查' } as Record<string, string>)[status] ?? status
}
function supplementStatusText(status: string): string {
  return ({ scheduled: '等待补入原计划', appending: '正在补入原计划', applied: '已补入原计划', already_present: '原计划已包含', blocked: '补入暂停', needs_review: '保存结果待核查' } as Record<string, string>)[status] ?? status
}
function materialIds(value: string | undefined): string[] {
  try {
    const parsed = JSON.parse(value || '[]')
    return Array.isArray(parsed) ? parsed.map(String) : []
  } catch { return [] }
}
function planMaterialIds(plan: PlanJob): string[] {
  const selected = materialIds(plan.selected_material_ids_json)
  return selected.length ? selected : materialIds(plan.material_ids_json)
}
function planMaterialLabel(plan: PlanJob): string {
  return materialIds(plan.selected_material_ids_json).length ? '千川已选素材 ID' : '待选素材 ID'
}
function displayedBudget(plan: PlanJob): string {
  if (plan.schedule_state === 'scheduled' || plan.schedule_state === 'confirm_required') return `${plan.daily_budget_yuan} 元（定时确认值）`
  if (plan.status === 'ready') return settings.dailyBudgetYuan >= 50 ? `${settings.dailyBudgetYuan} 元` : '待设置'
  const budget = Number(plan.daily_budget_yuan || 0)
  return budget >= 50 ? `${budget} 元` : '历史记录未保存预算'
}

function beijingInput(value = Date.now()): string {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    .format(new Date(value)).replace(' ', 'T')
}
function scheduleLabel(plan: PlanJob): string {
  if (plan.schedule_state === 'confirm_required') return '定时已暂停，需重新确认'
  if (plan.schedule_state === 'cancelled') return '定时已取消，保留手动草稿'
  if (plan.schedule_state === 'completed') return `定时提交已完成 · ${plan.scheduled_at.replace('T', ' ')}（北京时间）`
  return plan.schedule_state === 'scheduled' ? `定时提交 · ${plan.scheduled_at.replace('T', ' ')}（北京时间）` : ''
}
function canEditSchedule(plan: PlanJob): boolean {
  return ['ready', 'blocked'].includes(plan.status) && !plan.submit_attempted && !plan.remote_plan_id && ![38, 39, 42].includes(plan.publish_queue_id)
}
async function openSchedule(queueId: number, qcId: number, existingTime = ''): Promise<void> {
  if (busy.value) return
  if (settingsDirty.value) { message.value = '请先保存计划设置，再确认定时摘要。'; return }
  busy.value = true
  try {
    const data = checked(await window.api.qianchuan.planScheduleContext({ publishQueueId: queueId, qianchuanAccountId: qcId }))
    scheduleDialog.value = {
      snapshot: data.snapshot as ScheduleSummary, confirmationJson: String(data.confirmationJson),
      productName: String(data.productName || ''), accountName: String(data.accountName || ''), qcName: String(data.qcName || '')
    }
    scheduleMinimum.value = beijingInput(Date.now() + 60_000)
    scheduleTime.value = existingTime && Date.parse(existingTime) > Date.now()
      ? beijingInput(Date.parse(existingTime)) : beijingInput(Date.now() + 5 * 60_000)
    scheduleError.value = ''
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}
async function saveSchedule(): Promise<void> {
  const dialog = scheduleDialog.value
  if (!dialog || busy.value) return
  const scheduledAt = `${scheduleTime.value}:00+08:00`
  if (!scheduleTime.value || !Number.isFinite(Date.parse(scheduledAt)) || Date.parse(scheduledAt) <= Date.now()) {
    scheduleError.value = '请选择未来的北京时间。'; return
  }
  busy.value = true
  try {
    checked(await window.api.qianchuan.savePlanSchedule({ publishQueueId: dialog.snapshot.publish_queue_id,
      qianchuanAccountId: dialog.snapshot.qianchuan_account_id, scheduledAt, confirmationJson: dialog.confirmationJson }))
    scheduleDialog.value = null
    message.value = '定时已保存：提前准备素材，到时且前置条件就绪后自动提交一次。'
    await refreshPlanData(false)
  } catch (error) { scheduleError.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}
async function cancelSchedule(plan: PlanJob): Promise<void> {
  if (busy.value || !confirm(`取消商品 ${plan.product_id} 的定时提交？\n草稿和已确认素材将保留，之后需手动发布或重新设置定时。`)) return
  busy.value = true
  try {
    checked(await window.api.qianchuan.cancelPlanSchedule(plan.id))
    message.value = '定时已取消，保留手动草稿。'
    await refreshPlanData(false)
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function loadTargets(): Promise<void> {
  targets.value = rows<Target>(checked(await window.api.qianchuan.uploadTargets()))
  if (!activeAccounts.value.some(row => row.id === accountId.value)) accountId.value = activeAccounts.value[0]?.id ?? 0
}

async function refreshPlanData(updateSettings = true): Promise<void> {
  if (!accountId.value || !qianchuanAccountId.value || !selectedBinding.value) {
    candidates.value = []
    plans.value = []
    supplements.value = []
    planTotal.value = 0
    return
  }
  const [config, history, extra, candidateResult] = await Promise.all([
    window.api.qianchuan.planSettings(qianchuanAccountId.value),
    window.api.qianchuan.planJobs({
      qianchuanAccountId: qianchuanAccountId.value,
      accountId: accountId.value,
      status: planStatus.value,
      offset: (planPage.value - 1) * planPageSize,
      limit: planPageSize
    }),
    window.api.qianchuan.planSupplements(),
    window.api.qianchuan.planCandidates({ accountId: accountId.value, qianchuanAccountId: qianchuanAccountId.value })
  ])
  const saved = checked(config).settings as Record<string, unknown>
  if (updateSettings && !settingsDirty.value) {
    settings.enabled = Number(saved.enabled ?? 0) === 1
    settings.dailyBudgetYuan = Number(saved.daily_budget_yuan ?? 0)
    settings.videoSource = String(saved.video_source ?? 'uploaded')
    settings.longRunning = Number(saved.long_running ?? 1) !== 0
    settings.smartCoupon = Number(saved.smart_coupon ?? 1) !== 0
    settings.aigcEnabled = Number(saved.aigc_enabled ?? 0) === 1
  }
  const historyData = checked(history)
  plans.value = rows<PlanJob>(historyData)
  planTotal.value = Number(historyData.total ?? plans.value.length)
  if (planPage.value > planPageCount.value) {
    planPage.value = planPageCount.value
  }
  supplements.value = rows<PlanSupplement>(checked(extra))
  candidates.value = rows<PlanCandidate>(checked(candidateResult))
}

async function refresh(): Promise<void> {
  loading.value = true
  try {
    await loadTargets()
    if (!linkedTargets.value.some(row => row.qianchuan_account_id === qianchuanAccountId.value)) {
      qianchuanAccountId.value = Number(linkedTargets.value[0]?.qianchuan_account_id ?? 0)
    }
    await refreshPlanData()
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN')
    message.value = ''
  } catch (error) {
    message.value = error instanceof Error ? error.message : String(error)
  } finally { loading.value = false }
}

async function saveSettings(): Promise<void> {
  if (!qianchuanAccountId.value || busy.value) return
  if (settings.enabled && (!Number.isSafeInteger(settings.dailyBudgetYuan) || settings.dailyBudgetYuan < 50)) {
    message.value = '开启自动创建并发布前，请先设置并保存至少 50 元的日预算。'
    return
  }
  if (settings.enabled && !confirm(`开启「${selectedQcName.value}」自动创建并发布后，符合条件的新商品会自动提交千川计划。日预算为 ${settings.dailyBudgetYuan} 元。确认开启？`)) return
  busy.value = true
  try {
    checked(await window.api.qianchuan.savePlanSettings({ qianchuanAccountId: qianchuanAccountId.value, ...settings }))
    settingsDirty.value = false
    message.value = settings.enabled
      ? '自动计划已在千川计划工作区启用；符合条件的后续商品会由独立调度处理。'
      : '后续新商品自动发布已关闭；已确认定时仍按安排执行，可在计划列表逐项取消。'
    await refreshPlanData()
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function createPlan(candidate: PlanCandidate): Promise<void> {
  if (busy.value || !candidate.canCreate) return
  busy.value = true
  try {
    const result = checked(await window.api.qianchuan.createManualPlan({
      publishQueueId: candidate.publishQueueId,
      qianchuanAccountId: qianchuanAccountId.value
    }))
    const job = result.job as Record<string, unknown> | undefined
    message.value = `已创建本地待发布计划任务 #${String(job?.id ?? '')}。尚未提交千川；请在计划列表确认预算后发布。`
    await refreshPlanData()
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function publishPlan(plan: PlanJob): Promise<void> {
  if (busy.value || plan.status !== 'ready') return
  if (['scheduled', 'confirm_required'].includes(plan.schedule_state)) {
    message.value = '该商品已设置定时；如需手动发布，请先取消定时。'
    return
  }
  if (settingsDirty.value) {
    message.value = '计划设置有未保存修改，请先保存，再确认发布。'
    return
  }
  if (plan.account_id !== accountId.value || plan.qianchuan_account_id !== qianchuanAccountId.value) {
    message.value = '当前账号与计划归属不一致，请刷新后再操作。'
    return
  }
  let freshSettings: Record<string, unknown>
  try {
    freshSettings = checked(await window.api.qianchuan.planSettings(plan.qianchuan_account_id))
      .settings as Record<string, unknown>
  } catch (error) {
    message.value = error instanceof Error ? error.message : String(error)
    return
  }
  const budget = Number(freshSettings.daily_budget_yuan ?? 0)
  if (!Number.isSafeInteger(budget) || budget < 50) {
    message.value = '该千川账户的已保存日预算不足 50 元，请先保存预算设置。'
    return
  }
  if (!confirm(`将向「${selectedQcName.value}」提交商品 ${plan.product_id} 的千川计划。\n\n投放方式：控成本投放\n日预算：${budget} 元\n佣金率：${plan.commission_rate}%\n保本 ROI：${Number(plan.roi_target).toFixed(2)}\n\n这会在千川创建并启动付费计划。确认发布？`)) return
  busy.value = true
  try {
    const result = checked(await window.api.qianchuan.runPlan(plan.id, budget))
    const job = result.job as Record<string, unknown> | undefined
    message.value = job?.status === 'published'
      ? `计划 #${plan.id} 已发布，千川计划 ID：${String(job.remote_plan_id ?? '')}`
      : `计划 #${plan.id} 已处理，请查看回执状态。`
    await refreshPlanData()
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
  finally { busy.value = false }
}

async function retryPlan(plan: PlanJob): Promise<void> {
  try {
    checked(await window.api.qianchuan.retryBlockedPlan(plan.id))
    message.value = `计划 #${plan.id} 已重新排队；再次提交前仍需点击发布并确认预算。`
    await refreshPlanData()
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
}

async function resolvePlan(plan: PlanJob, present: boolean): Promise<void> {
  const remotePlanId = present ? prompt('请填写千川页面核实的计划 ID：') : ''
  if (remotePlanId === null) return
  const note = prompt(present ? '请写明已在千川核实此计划的依据（至少 6 字）：' : '请写明已在千川核实不存在此计划的依据（至少 6 字）：')
  if (note === null) return
  try {
    checked(await window.api.qianchuan.resolvePlan({ id: plan.id, present, note, remotePlanId: remotePlanId || undefined }))
    message.value = `计划 #${plan.id} 核查结论已保存。`
    await refreshPlanData()
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
}

async function openScreenshot(path: string): Promise<void> {
  try {
    const result = await window.api.publish.openScreenshot(path)
    if (!result.ok) message.value = result.error ?? '无法打开截图'
  } catch (error) { message.value = error instanceof Error ? error.message : String(error) }
}

watch(accountId, () => {
  settingsDirty.value = false
  planPage.value = 1
  const firstLinked = linkedTargets.value[0]
  qianchuanAccountId.value = Number(firstLinked?.qianchuan_account_id ?? 0)
  if (!firstLinked) void refreshPlanData()
})
watch(qianchuanAccountId, () => {
  settingsDirty.value = false
  planPage.value = 1
  void refreshPlanData().catch(error => { message.value = String(error) })
})
watch(planStatus, () => {
  planPage.value = 1
  void refreshPlanData(false).catch(error => { message.value = String(error) })
})
watch(planPage, () => { void refreshPlanData(false).catch(error => { message.value = String(error) }) })
let refreshTimer: ReturnType<typeof setInterval> | null = null
useAppRefresh(() => refresh())
onMounted(() => {
  void refresh()
  refreshTimer = setInterval(() => {
    if (busy.value || loading.value) return
    void (async () => {
      await loadTargets()
      if (!linkedTargets.value.some(row => row.qianchuan_account_id === qianchuanAccountId.value)) {
        qianchuanAccountId.value = Number(linkedTargets.value[0]?.qianchuan_account_id ?? 0)
      }
      await refreshPlanData(false)
      refreshedAt.value = new Date().toLocaleTimeString('zh-CN')
    })().catch(error => { message.value = String(error) })
  }, 15_000)
})
onBeforeUnmount(() => { if (refreshTimer) clearInterval(refreshTimer) })
</script>

<template>
  <section class="plans-page">
    <header class="page-head">
      <div><h1>千川计划</h1><p>在这里独立创建和发布千川计划；选品及抖音发布不会直接提交付费计划。</p></div>
      <div class="actions"><button class="btn" @click="emit('open-qianchuan-accounts')">账号与登录</button><button class="btn" :disabled="loading" @click="refresh">刷新</button></div>
    </header>

    <div class="toolbar">
      <label>抖音账号
        <select v-model.number="accountId" :disabled="!activeAccounts.length">
          <option v-for="account in activeAccounts" :key="account.id" :value="account.id">{{ account.name }}（{{ account.id }}）</option>
        </select>
      </label>
          <label>千川账户
        <select v-model.number="qianchuanAccountId" :disabled="busy || !linkedTargets.length">
          <option v-for="target in linkedTargets" :key="target.qianchuan_account_id ?? 0" :value="target.qianchuan_account_id ?? 0">{{ target.qc_name || target.qc_id || `账户 ${target.qianchuan_account_id}` }}</option>
        </select>
      </label>
      <span class="binding-status" :class="selectedBinding?.qc_id && /^\d{8,20}$/.test(selectedBinding.qc_id) ? 'ok' : 'error'">
        广告主 ID：{{ selectedBinding?.qc_id || '未填写或未同步到千川档案' }}
      </span>
      <button class="btn" :disabled="loading || busy" @click="refresh">{{ loading ? '刷新中…' : '刷新账号与计划' }}</button>
      <small v-if="refreshedAt" class="refresh-time">更新于 {{ refreshedAt }}</small>
    </div>

    <div v-if="message" class="notice" role="status">{{ message }}</div>
    <div v-if="!linkedTargets.length" class="empty-state">
      <strong>当前抖音账号还没有绑定千川账户</strong>
      <p>请先到账号管理绑定目标千川账户，再回到这里创建计划。</p>
      <button class="btn primary" @click="emit('open-qianchuan-accounts')">去账号管理</button>
    </div>
    <template v-else>
      <section class="card settings-card">
        <div class="section-head"><div><h2>计划设置 · {{ selectedQcName }}</h2><p>预算按千川账户设置；ROI 按所选抖音账号下每个商品的佣金率计算。</p></div><button class="btn primary" :disabled="busy || loading || !qianchuanAccountId" @click="saveSettings">{{ settings.enabled ? '保存自动发布设置' : '开启自动发布' }}</button></div>
        <div class="settings-grid">
          <label class="toggle-row"><input v-model="settings.enabled" type="checkbox" @change="settingsDirty = true" />
            <span><strong>自动创建并发布</strong><small>开启后，新发布商品在千川素材就绪时由独立调度器创建并提交计划；无需再逐条点击发布。</small></span>
          </label>
          <label>日预算（元）<input v-model.number="settings.dailyBudgetYuan" type="number" min="50" max="10000000" step="1" @input="settingsDirty = true" /></label>
          <label>素材来源<select v-model="settings.videoSource" @change="settingsDirty = true"><option value="uploaded">计划页上传的视频</option><option value="my_video">我的视频（已发布作品）</option><option value="both">计划页上传视频并选我的视频</option></select></label>
        </div>
        <p class="formula">投放方式固定为「控成本投放」。保本 ROI = 100 ÷ 佣金率（%）；例如佣金率 20%，ROI 为 5.00。佣金率缺失或账号不匹配时会阻止创建/发布。</p>
        <p class="hint">自动发布开关控制后续新商品；逐商品确认的定时独立保存，需要停止时请在计划列表取消定时。</p>
        <p v-if="settings.enabled && settings.dailyBudgetYuan < 50" class="reason">自动创建并发布已勾选；请先填写至少 50 元日预算，再保存设置。</p>
        <div class="form-actions"><span v-if="settings.enabled" class="warn">自动发布已开启：后续符合条件的商品可能按此预算启动付费计划。</span><span v-else class="hint">勾选后点击上方“开启自动发布”，保存后才会生效。</span></div>
      </section>

      <section class="card">
        <div class="section-head"><div><h2>新建计划</h2><p>只列出本账号已确认抖音发布、素材来源与预算可核对的商品。</p></div></div>
        <div v-if="loading" class="empty-state">正在读取可创建的商品…</div>
        <div v-else-if="!candidates.length" class="empty-state">暂无已发布且绑定当前千川账户的商品。</div>
        <div v-else class="candidate-list">
          <article v-for="candidate in candidates" :key="candidate.publishQueueId" class="candidate">
            <div class="candidate-select">
              <span><strong>{{ candidate.productName || `商品 ${candidate.productId}` }}</strong><small>{{ selectedAccountName }} · 千川广告主 {{ candidate.advertiserId }} · 商品 ID {{ candidate.productId }} · {{ candidate.videoSource === 'my_video' ? '计划页核对我的视频' : `${candidate.materialCount} 条可用已上传视频` }}</small><small v-if="candidate.remoteMaterialIds.length && candidate.videoSource !== 'my_video'">素材 ID：{{ candidate.remoteMaterialIds.join('、') }}</small><small v-if="candidate.canCreate">日预算 {{ candidate.dailyBudgetYuan }} 元 · 佣金率 {{ candidate.commissionRate }}% · 保本 ROI {{ Number(candidate.roiTarget).toFixed(2) }}</small><small v-else class="reason">{{ candidate.reason }}</small></span>
            </div>
            <div class="row-actions">
              <button v-if="candidate.canSchedule" class="btn primary" :disabled="busy" @click="openSchedule(candidate.publishQueueId, candidate.qianchuanAccountId)">定时发布</button>
              <span v-if="candidate.scheduleState === 'scheduled'" class="schedule-tag">已设定时，请在计划列表查看</span>
              <button class="btn" :disabled="busy || !candidate.canCreate" @click="createPlan(candidate)">{{ candidate.canCreate ? '生成待发布计划' : '暂不可创建' }}</button>
            </div>
          </article>
        </div>
        <p class="hint">“生成待发布计划”仅保存手动草稿。“定时发布”确认后会提前准备素材，到设定时间且前置条件就绪时自动提交；软件需保持运行，错过时间会在下次启动且条件就绪后续做。</p>
      </section>

      <section class="card">
        <div class="section-head"><div><h2>计划列表 · {{ selectedAccountName }}</h2><p>已开启账户自动发布时，自动任务会按队列依次处理；手动任务仍需你确认预算。提交结果不明会暂停同一千川账户的后续自动计划，核实前不会重复发布。</p></div><div class="plan-filters"><label>状态<select v-model="planStatus"><option value="">全部状态</option><option value="ready">待发布</option><option value="submitting">发布中</option><option value="published">已发布</option><option value="blocked">发布前停止</option><option value="needs_review">待核查</option></select></label><button class="btn" :disabled="loading || busy" @click="refreshPlanData(false)">刷新计划</button></div></div>
        <div v-if="!visiblePlans.length" class="empty-state">暂无计划任务。</div>
        <div v-else class="plan-list">
          <article v-for="plan in visiblePlans" :key="plan.id" class="plan-row">
            <div class="plan-info">
              <div class="plan-title"><strong>{{ plan.product_name || plan.plan_name }}</strong><span class="status" :class="plan.status">{{ statusText(plan.status) }}</span></div>
              <small>{{ activeAccounts.find(row => row.id === plan.account_id)?.name || `账号 ${plan.account_id}` }} · 千川广告主 {{ plan.advertiser_id }}</small>
              <small>{{ plan.plan_name }} · 控成本投放 · 预算 {{ displayedBudget(plan) }} · {{ plan.schedule_state === 'scheduled' ? '定时任务' : plan.schedule_state === 'confirm_required' ? '定时待确认' : plan.creation_source === 'manual' ? '手动任务' : '自动任务' }}</small>
              <small>商品 {{ plan.product_id }} · 佣金率 {{ plan.commission_rate }}% · 保本 ROI {{ Number(plan.roi_target).toFixed(2) }} · 任务 #{{ plan.id }}<template v-if="plan.remote_plan_id"> · 千川计划 ID {{ plan.remote_plan_id }}</template></small>
              <small v-if="planMaterialIds(plan).length">{{ planMaterialLabel(plan) }}：{{ planMaterialIds(plan).join('、') }}</small>
              <small v-if="plan.reason" class="reason">{{ plan.reason }}</small>
              <small v-if="plan.schedule_state" class="schedule-tag">{{ scheduleLabel(plan) }}</small>
              <small v-for="extra in visibleSupplements.filter(row => row.plan_job_id === plan.id)" :key="extra.id" :class="{ reason: ['blocked', 'needs_review'].includes(extra.status) }">{{ supplementStatusText(extra.status) }}<template v-if="extra.status === 'scheduled'"> · {{ extra.scheduled_at.replace('T', ' ') }}</template><template v-if="extra.reason"> · {{ extra.reason }}</template></small>
            </div>
            <div class="row-actions">
              <button v-if="canEditSchedule(plan)" class="btn" :disabled="busy" @click="openSchedule(plan.publish_queue_id, plan.qianchuan_account_id, plan.scheduled_at)">{{ plan.schedule_state === 'confirm_required' ? '重新确认定时' : plan.schedule_state === 'scheduled' ? '修改时间' : '定时发布' }}</button>
              <button v-if="canEditSchedule(plan) && ['scheduled', 'confirm_required'].includes(plan.schedule_state)" class="btn" :disabled="busy" @click="cancelSchedule(plan)">取消定时</button>
              <button v-if="plan.status === 'ready' && !['scheduled', 'confirm_required'].includes(plan.schedule_state)" class="btn primary" :disabled="busy" @click="publishPlan(plan)">确认预算并发布</button>
              <button v-if="plan.status === 'blocked' && !['scheduled', 'confirm_required'].includes(plan.schedule_state)" class="btn" :disabled="busy" @click="retryPlan(plan)">重新排队</button>
              <template v-if="plan.status === 'needs_review'"><button class="btn" @click="resolvePlan(plan, true)">平台已发布</button><button class="btn" @click="resolvePlan(plan, false)">核实未发布</button></template>
              <button v-if="plan.screenshot_path" class="btn" @click="openScreenshot(plan.screenshot_path)">查看截图</button>
            </div>
          </article>
        </div>
        <div v-if="planTotal" class="pagination"><span>共 {{ planTotal }} 条 · 第 {{ planPage }} / {{ planPageCount }} 页</span><button class="btn" :disabled="planPage <= 1 || busy" @click="planPage--">上一页</button><button class="btn" :disabled="planPage >= planPageCount || busy" @click="planPage++">下一页</button></div>
      </section>
    </template>
    <div v-if="scheduleDialog" class="schedule-mask" @click.self="!busy && (scheduleDialog = null)" @keydown.esc="!busy && (scheduleDialog = null)">
      <section class="schedule-dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-title" tabindex="-1">
        <h2 id="schedule-title">确认千川定时发布</h2>
        <strong>{{ scheduleDialog.productName || scheduleDialog.snapshot.product_id }}</strong>
        <dl>
          <dt>商品 ID</dt><dd>{{ scheduleDialog.snapshot.product_id }}</dd>
          <dt>抖音账号</dt><dd>{{ scheduleDialog.accountName }}</dd>
          <dt>千川账户</dt><dd>{{ scheduleDialog.qcName }} · {{ scheduleDialog.snapshot.advertiser_id }}</dd>
          <dt>日预算</dt><dd>{{ scheduleDialog.snapshot.daily_budget_yuan }} 元</dd>
          <dt>佣金率 / 保本 ROI</dt><dd>{{ scheduleDialog.snapshot.commission_rate }}% / {{ Number(scheduleDialog.snapshot.roi_target).toFixed(2) }}</dd>
        </dl>
        <label class="schedule-time">提交时间（北京时间 UTC+8）<input v-model="scheduleTime" type="datetime-local" :min="scheduleMinimum" :disabled="busy" /></label>
        <p class="hint">控成本投放。确认即授权到时自动提交上述商品的付费计划。素材会提前准备；软件关闭或条件未就绪时保留任务，恢复后续做。预算或 ROI 等设置变化时暂停并要求重新确认。</p>
        <p v-if="scheduleError" class="reason" role="alert">{{ scheduleError }}</p>
        <div class="row-actions"><button class="btn" :disabled="busy" @click="scheduleDialog = null">返回</button><button class="btn primary" :disabled="busy" @click="saveSchedule">{{ busy ? '保存中…' : '确认并保存定时' }}</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.schedule-tag{color:var(--brand)!important;font-size:12px}.schedule-mask{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;padding:18px;background:rgba(15,23,42,.3)}
.schedule-dialog{display:flex;flex-direction:column;gap:12px;width:min(520px,100%);max-height:90vh;overflow:auto;padding:22px;background:var(--surface,#fff);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-card)}
.schedule-dialog h2{margin:0;font-size:18px}.schedule-dialog>strong{overflow-wrap:anywhere}.schedule-dialog dl{display:grid;grid-template-columns:125px minmax(0,1fr);gap:8px;margin:0;font-size:12px}.schedule-dialog dt{color:var(--text-muted)}.schedule-dialog dd{margin:0;overflow-wrap:anywhere}.schedule-time{display:flex;flex-direction:column;gap:7px;font-size:12px}.schedule-time input{min-height:34px;padding:6px 9px;border:1px solid var(--border);border-radius:var(--radius-control);background:#fff;color:var(--text);font:inherit}
.plans-page{display:flex;flex-direction:column;gap:12px;width:100%;min-width:0;min-height:0;overflow:auto;padding:2px 2px 16px;color:var(--text)}
.page-head,.section-head,.actions,.form-actions,.plan-title,.row-actions{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.page-head h1{margin:0;font-size:21px}.page-head p,.section-head p,.formula,.hint{margin:4px 0 0;color:var(--text-muted);font-size:12px;line-height:1.55}
.toolbar,.card{border:1px solid var(--border);border-radius:9px;background:#fff;box-shadow:var(--shadow-card)}
.toolbar{display:flex;gap:14px;flex-wrap:wrap;padding:12px 14px}.toolbar label,.settings-grid>label{display:flex;flex-direction:column;gap:6px;color:var(--text-muted);font-size:12px;font-weight:600}
.binding-status{align-self:end;min-height:34px;display:flex;align-items:center;color:var(--text-muted);font-size:12px}.binding-status.ok{color:#16834d}.binding-status.error{color:#b54743}.refresh-time{align-self:end;color:var(--text-muted);font-size:11px}
.plan-filters,.pagination{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.plan-filters label{display:flex;align-items:center;gap:6px;color:var(--text-muted);font-size:12px}.plan-filters select{min-width:120px;min-height:32px;border:1px solid var(--border);border-radius:6px;padding:4px 8px;background:#fff;color:var(--text);font:inherit}.pagination{justify-content:flex-end;margin-top:12px;color:var(--text-muted);font-size:12px}
select,input[type=number]{min-height:34px;min-width:210px;border:1px solid var(--border);border-radius:6px;padding:5px 9px;background:#fff;color:var(--text);font:inherit}
.card{padding:14px}.section-head h2{margin:0;font-size:16px}.settings-grid{display:grid;grid-template-columns:minmax(260px,1.5fr) minmax(160px,1fr) minmax(170px,1fr);gap:16px;align-items:end;margin-top:14px}
.toggle-row{display:flex!important;flex-direction:row!important;align-items:flex-start!important}.toggle-row input{margin-top:3px}.toggle-row span{display:flex;flex-direction:column;gap:5px}.toggle-row small{font-weight:400;color:#77849a;line-height:1.45}
.formula{padding:9px 11px;border-radius:6px;background:#f3f6fb}.form-actions{margin-top:12px}.warn,.reason{color:#b54743}.warn{font-size:12px}.notice,.empty-state{padding:13px;border-radius:7px;background:var(--brand-soft);color:var(--text-muted);font-size:13px}.empty-state{margin-top:10px;text-align:center}.empty-state strong{color:var(--text)}.empty-state p{margin:5px 0 12px}
.candidate-list,.plan-list{display:flex;flex-direction:column;gap:8px;margin-top:12px}.candidate,.plan-row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:12px;border:1px solid #e5eaf2;border-radius:7px;background:#fff}.candidate.selected{border-color:#6a97ef;background:#f7faff}.candidate-select{display:flex;align-items:flex-start;gap:9px;min-width:0;flex:1}.candidate-select input{margin-top:3px}.candidate-select span,.plan-info{display:flex;flex-direction:column;gap:4px;min-width:0}.candidate-select small,.plan-info small{color:#748198;line-height:1.45;overflow-wrap:anywhere}.reason{color:#b54743!important}.hint{margin-top:9px}.btn{min-height:32px;border:1px solid var(--border);border-radius:6px;padding:5px 11px;background:#fff;color:var(--text);cursor:pointer;font-size:12px;white-space:nowrap}.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}.btn:disabled{opacity:.5;cursor:not-allowed}.status{padding:3px 8px;border-radius:10px;background:#edf1f7;color:#5c6a7c;font-size:11px}.status.published{background:#e6f7ed;color:#16834d}.status.needs_review,.status.blocked{background:#fff0ed;color:#b3483d}.row-actions{justify-content:flex-end}
@media(max-width:900px){.settings-grid{grid-template-columns:1fr}.candidate,.plan-row{flex-direction:column}.row-actions{justify-content:flex-start}}
.plans-page { gap: 14px; padding: 4px 4px 17px; }
.page-head h1 { font-size: 24px; letter-spacing: -.03em; }
.toolbar, .card { border-radius: var(--radius); }
.toolbar, .card { padding: 16px; }
.candidate, .plan-row { padding: 10px; gap: 10px; }
.candidate, .plan-row { border-color: var(--border); border-radius: var(--radius-control); }
.candidate.selected { border-color: var(--brand); background: var(--brand-soft); }
.btn, select, input[type=number], .plan-filters select { min-height: 32px; border-radius: var(--radius-control); }
.notice, .empty-state { border-radius: var(--radius-control); }
</style>
