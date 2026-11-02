<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import BrowserPane from './BrowserPane.vue'
import SelectionFilterEditor from './SelectionFilterEditor.vue'
import { createSelectionFilterSettings } from '../utils/selection_filters'
import type { SelectionFilterSettings } from '../utils/selection_filters'
import {
  allocateExclusiveCandidates,
  candidateLeafCategoryId,
  filterCandidatesByRegistry,
  rankByRecommendedCategories,
  scoreUnifiedCandidatePools
} from '../utils/selection_candidates'
import { notifyGlobal } from '../utils/notifications'
import { toUserSelectionError, toUserSelectionMessage } from '../utils/selection_error_ux'
import {
  buildSelectionCategoryPlan,
  canAutoResumeSelection,
  compareRecoverySettings,
  createSingleFlight,
  decideRecoverySettingsChange,
  evaluateSelectionCooldownResume,
  filterPendingRecoveryAccounts,
  findAccountResourcePathConflicts,
  getSelectionErrorRecoveryState,
  getSelectionBatchFinalState,
  getSelectionRecoveryMode,
  isUnconfirmedLoginFalseNegative,
  resolveRecoveryCategorySelection,
  retryUnconfirmedLoginCheck,
  runSequentialSelectionBatch,
  selectCandidateIdsForAccount
} from '../utils/selection_batch'

const props = withDefaults(defineProps<{
  initialAccountId?: number | null
  dialogOnly?: boolean
}>(), {
  initialAccountId: null,
  dialogOnly: false
})
const emit = defineEmits<{
  (event: 'close'): void
  (event: 'saved', accountId: number): void
  (event: 'navigate', value: string): void
}>()

interface CategoryNode {
  id: string
  name: string
  children: CategoryNode[]
}

interface AccountRow {
  id: number
  name: string
  status: string
  douyin_id?: string | null
}

type LoginCheckState = 'checking' | 'logged_in' | 'login_required' | 'verification_required' | 'unknown'

interface LoginPlatformResult {
  state: LoginCheckState
  message: string
}

interface LoginPreflightTarget {
  accountId: number
  accountName: string
  profilePath?: string
  resolvedProfilePath?: string
  contextError?: string
}

interface LoginPreflightAccount extends LoginPreflightTarget {
  baiying: LoginPlatformResult
  creatorCenter: LoginPlatformResult
  manualBaiyingConfirmed?: boolean
  manualCreatorCenterConfirmed?: boolean
}

interface LoginPreflightDialog {
  batchId: string | null
  scopeFingerprint: string
  accounts: LoginPreflightAccount[]
  checking: boolean
}

interface AccountRunContext {
  profileKey: string
  profilePath?: string
  resolvedProfilePath: string
  downloadPath: string
  settings: Record<string, unknown>
}

interface SelectionRecoveryAccount {
  batchId: string
  accountId: number
  accountName: string
  sequence: number
  state: string
  stage: string
  error: string
  candidates: SelectionCandidate[] | null
  selectedIndexes: number[] | null
  completed: Array<Record<string, unknown>> | null
  updatedAt?: string
}

type SelectionLogLevel = 'success' | 'info' | 'warning' | 'error'

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
  legacy?: boolean
}

interface SelectionRunLogInput {
  stage: string
  code: string
  message: string
  detail?: string
  productId?: string
  productTitle?: string
}

interface PrioritySourceReadResult {
  sourceKey: string
  label: string
  status: 'read' | 'empty' | 'skipped'
  productCount: number
  candidateNodeCount?: number
  productNodeCount?: number
  parsedCandidateCount?: number
  matchedCandidateCount?: number
  passedCandidateCount?: number
  weightedHitCount?: number
  categoryFilteredCount?: number
  mutualExclusionSkippedCount?: number
  firstProductIds?: string[]
  sourceOutcome?: string
  pageListDiagnosis?: string
  detail?: string
}

function visibleSourceNoCandidateMessage(
  results: PrioritySourceReadResult[],
  fallback: string
): string {
  if (!results.length) return '当前没有启用的百应来源，本轮未检查商品。'
  const failed = results.filter((source) => source.status === 'skipped')
  if (failed.length) {
    const labels = failed.map((source) => source.label || source.sourceKey).join('、')
    return `${labels}读取未成功，本轮货源检查不完整，不能据此判断百应没有商品。请查看来源日志详情，确认页面和登录状态后恢复批次。`
  }
  if (results.every((source) => source.status === 'empty')) {
    return '已成功检查本轮启用的百应来源，但当前可见列表均显示为空。'
  }
  const unparsed = results.filter((source) =>
    source.status === 'read' && source.productCount > 0 && source.parsedCandidateCount === 0
  )
  if (unparsed.length) {
    return `${unparsed.map((source) => source.label || source.sourceKey).join('、')}读到了商品 ID，但没有解析出可评分商品行；本次不能判断是否有符合条件的商品，请查看来源日志详情。`
  }
  return fallback
}

interface SelectionRecoveryBatch {
  batchId: string
  state: string
  payload: Record<string, unknown>
  accounts: SelectionRecoveryAccount[]
  runLogs?: SelectionRunLog[]
  /** 兼容旧版恢复引擎的返回字段。 */
  failureLogs?: SelectionRunLog[]
  updatedAt?: string
}

interface SelectionRecoveryHooks {
  batchId: string
  state: SelectionRecoveryAccount
  persist: (patch: Record<string, unknown>) => Promise<void>
  /** 旧失败分支兼容名称；新成功/提示事件统一使用 logEvent。 */
  logFailure: (value: SelectionRunLogInput) => Promise<void>
  logEvent: (value: SelectionRunLogInput) => Promise<void>
}

interface AccountCandidatePlan {
  candidates: SelectionCandidate[]
  registryExcluded: Array<{ candidate: SelectionCandidate; reason: string }>
  mutualExclusionSkipped: SelectionCandidate[]
  rejectedCandidates: SelectionCandidate[]
  prioritySourceResults: PrioritySourceReadResult[]
  warnings: string[]
  recommendedCategories: Array<{ categoryId: string; highScoreCount: number; averageScore: number }>
  categoryEnrichment?: { summary?: string }
}

interface FilterField {
  label: string
  options: string[]
  multiple: boolean
}

interface SelectionCandidate {
  cardIndex: number
  productId: string
  publicationLock?: {
    productId: string
    productName?: string
    taskIds: number[]
    queueStatuses: string[]
    platformStatuses: string[]
  }
  cartAttempted?: boolean
  commodityId: string
  title: string
  price: number | null
  monthSales: number | null
  saleAmount: number | null
  conversionRate: number | null
  views: number | null
  commissionRate: number | null
  shopScore: number | null
  trendRising: boolean
  poolRank: number
  sources?: CandidateSource[]
  dataMetrics?: Record<string, number | null>
  metricSources?: Record<string, string>
  analysisScore?: number | null
  unifiedScore?: number | null
  hotOrRankBoost?: number
  analysisRawScore?: number | null
  analysisCoverage?: number
  analysisMetricCount?: number
  detailCapturedAt?: string
  analysisMissingMetrics?: string[]
  estimatedSalesAmount?: number | null
  analysisReasons?: string[]
  analysisStatus?: string
  analysisError?: string
  analysisRejected?: boolean
  thresholdReasons?: string[]
  status?: string
  selected: boolean
  sourceCategorySelection?: { level1: string[]; level2: string[]; level3: string[] }
  categoryIds?: string[]
  leafCategoryId?: string
  sourceKey?: string
  sourceUrl?: string
  selectionMode?: 'general' | 'category'
  categoryMode?: 'auto' | 'manual'
  categoryStatus?: string
  categoryError?: string
  mutualExclusionReason?: string
  mutualExclusionWinnerAccountId?: number
}

type CategoryLevel = 'level1' | 'level2' | 'level3'
type CandidateSource = 'hotSpot' | 'allianceRank' | 'selectionSquare'
const DB_PATH = 'D:\\software\\投广客户端\\data\\ad_client.db'
const COMPLIANCE_DEFAULT_BANNED_WORDS = [
  '高仿', '仿牌', '假货', '假冒', '伪劣', '三无产品', '无中文标签', '无生产日期', '无保质期', '无生产厂家',
  '盗版', '侵权', '山寨', '套牌', '假证', '假检测报告', '虚假宣传', '夸大宣传', '包治', '根治',
  '特效药', '处方药', '禁售', '违禁品', '毒品', '枪支', '弹药', '管制刀具', '赌博', '博彩',
  '六合彩', '色情', '成人用品', '成人视频', '裸聊', '刷单', '刷粉', '刷量', '代考', '代写',
  '作弊器', '窃听器', '针孔摄像', '破解软件', '盗号', '外挂', '个人信息', '银行卡信息', '假币', '人民币'
]
const PROTOTYPE_BANNED_WORDS = [
  '罗永浩', '郝劭文', '正能量小明', '小明', '贾乃亮', '木森', '森弟', '大漠叔叔', '别叫我马大哈', '查文娟',
  '娟砸的废话bot', '笑笑姐', '小蝴蝶', '心动方程式', '与辉', '董与辉', 'K总', '达乐', '老罗', '大舞台',
  '朱朱', '乡妹朱朱', '朱婷', '农村涛涛励志百货', '小熊励志百货', '励志娃聪聪百货', '乐观励志阿文百货', '励志阿文百货', '农村励志小聪百货', '周周百货园',
  '毛毛和爷爷的小店', '小勇在努力', '黄家敏', '匡赐有好物', '与辉同行', '汉森', '罗总', '郝邵文', '交个朋友', '博学罗交个朋友',
  '亮哥', '罗罗', '亮亮', '安安', '唐梦琪', '三公主', '唐三妹', '一路向前', '一颗小小草'
]
const OPTIONAL_MALL_TITLE_BANNED_WORDS = ['编码', '过号', '集合', '随机', '测试', '勿拍', '链接', '补差价']
const DEFAULT_BANNED_WORDS = Array.from(new Set([...COMPLIANCE_DEFAULT_BANNED_WORDS, ...PROTOTYPE_BANNED_WORDS]))

const DEFAULTS = {
  candidateLimit: 5,
  selectionMode: 'general' as 'general' | 'category',
  categoryMode: 'auto' as 'auto' | 'manual',
  categorySelection: { level1: [] as string[], level2: [] as string[], level3: [] as string[] },
  filters: { delivery: {} as Record<string, unknown>, product: {} as Record<string, unknown>, features: [] as string[] },
  poolPriority: ['流量扶持', '小二严选', '低价好货', '福袋奖品'],
  poolFallback: true,
  sources: { hotSpotEnabled: true, allianceRankEnabled: true, priority: ['hotSpot', 'allianceRank'] as Array<'hotSpot' | 'allianceRank'> },
  thresholds: { salesAmount: null as number | null, salesCount: null as number | null, conversionRate: null as number | null, views: null as number | null, commissionRate: null as number | null, experienceScore: null as number | null },
  trendPreference: '上升优先',
  bannedWords: [...DEFAULT_BANNED_WORDS],
  bannedWordsSeedVersion: 1,
  execution: { publishMode: '自动', pinTime: '', randomIntervalMin: 1, randomIntervalMax: 5 },
  quota: { perProductVideos: 10, perAccountProducts: 5, perAccountVideos: 50 }
}

const busy = ref(false)
const error = ref('')
const notice = ref('')
const runLogWarning = ref('')
const selectionErrorPresentation = computed(() => toUserSelectionError(error.value))
const selectionNoticePresentation = computed(() => toUserSelectionError(notice.value))
const selectionProgressPresentation = computed(() =>
  toUserSelectionError(selectionProgress.value?.message ?? '')
)
const selectionCooldownArmed = ref(false)
const selectionCooldownRemaining = ref(0)
let selectionCooldownTimer: number | null = null
let selectionCooldownDeadline: number | null = null
let selectionAutoResumeTimer: number | null = null
let selectionViewUnmounted = false
let stopSelectionCooldownExpired: (() => void) | undefined
let automaticResumeBatchId: string | null = null
let automaticResumeAccountId: number | null = null
const autoResumeAccountId = ref<number | null>(null)
interface CooldownAutoResumeIntent {
  batchId: string
  accountId: number
  deadline: number
  retryAt?: number
  awaitingExpiryAck?: boolean
  attempts: number
  phase: 'pending' | 'starting' | 'blocked'
}
const cooldownAutoResumeIntent = ref<CooldownAutoResumeIntent | null>(null)
let completedCooldownAutoResume: { batchId: string; accountId: number; deadline: number } | null = null
const MAX_COOLDOWN_AUTO_RESUME_START_RETRIES = 3
interface SelectionProgressState {
  state: 'running' | 'finished' | 'failed'
  mode: 'batch' | 'preview'
  accountId: number | null
  accountName: string
  accountIndex: number
  accountTotal: number
  completedAccounts: number
  stage: string
  message: string
  stepIndex?: number | null
  stepTotal?: number | null
  stepUnit?: string
  itemIndex: number | null
  itemTotal: number | null
  percent: number | null
  indeterminate?: boolean
}
const selectionProgress = ref<SelectionProgressState | null>(null)
let stopSelectionProgress: (() => void) | undefined
let stopAutoPublishProgress: (() => void) | undefined
let activeAutoPublishQueueId: number | null = null

interface SelectionWorkflowStep {
  key: string
  label: string
  weight: number
}

const BATCH_SELECTION_WORKFLOW: SelectionWorkflowStep[] = [
  { key: 'preflight', label: '检查账号、浏览器与登录状态', weight: 8 },
  { key: 'collect', label: '采集百应当前可见商品', weight: 20 },
  { key: 'quota_check', label: '检查账号剩余商品配额', weight: 5 },
  { key: 'filter', label: '统一筛选、评分、类目推荐与 ID 去重', weight: 12 },
  { key: 'cart', label: '逐个核验候选并加入选品车', weight: 20 },
  { key: 'quota_record', label: '登记商品配额', weight: 3 },
  { key: 'video', label: '处理详情图并生成视频', weight: 20 },
  { key: 'queue', label: '写入发布队列（全自动时继续发布）', weight: 12 }
]

const PREVIEW_SELECTION_WORKFLOW: SelectionWorkflowStep[] = [
  { key: 'preflight', label: '检查账号设置与本机目录', weight: 15 },
  { key: 'collect', label: '采集百应当前可见商品', weight: 60 },
  { key: 'filter', label: '统一筛选、商品 ID 去重与类目推荐', weight: 20 },
  { key: 'result', label: '整理并显示候选预览', weight: 5 }
]

function workflowIndexForStage(stageValue: string, mode: SelectionProgressState['mode']): number {
  const stage = stageValue.replace(/^(失败于：|账号处理失败：)/, '').trim()
  const steps = mode === 'preview' ? PREVIEW_SELECTION_WORKFLOW : BATCH_SELECTION_WORKFLOW
  if (/video_pending/.test(stage)) return 6
  if (/cart_pending/.test(stage)) return 4
  if (/quota_cart/.test(stage)) return 5
  if (/quota_check/.test(stage)) return 2
  if (/candidate_fetch/.test(stage)) return 1
  if (mode === 'preview') {
    if (/候选预览完成|预览结果/.test(stage)) return 3
    if (/评分|去重|筛选与互斥|类目推荐|推荐类目|统一分配/.test(stage)) return 2
    if (/准备候选预览|准备账号选品任务|检查账号设置|读取账号设置/.test(stage)) return 0
    if (/采集|候选|百应筛选|热点|联盟|榜单|来源|列表/.test(stage)) return 1
    return 0
  }
  if (/准备选品批次|读取启用账号|检查百应与创作者中心登录态|复核登录状态|账号资源校验|登录状态预检/.test(stage)) return 0
  if (/详情图|图转视频|生成视频|视频生成|恢复详情图/.test(stage)) return 6
  if (/发布|投稿|队列|上传视频|创作者中心|封面|关联百应|发布选项/.test(stage)) return 7
  if (/登记加车配额|记录(?:加车|商品|选品车)?配额|配额记录/.test(stage)) return 5
  if (/加车|核验并处理|处理选中商品|商品数据分析|选品车|候选商品处理/.test(stage)) return 4
  if (/评分|筛选与互斥|类目推荐|推荐类目|互斥分配|候选分配|ID 去重|ID去重|本机登记|候选计划已就绪|候选统一/.test(stage)) return 3
  if (/统一采集|百应筛选|获取候选|候选预览|查询百应候选|应用选品条件|material_list|来源|热点|联盟|榜单|商品列表/.test(stage)) return 1
  if (/账号配额预检/.test(stage)) return 0
  if (/每日配额|剩余配额|账号配额|quota_check/.test(stage)) return 2
  // 批次候选计划已在账号执行前统一采集；账号执行入口的准备/配额检查属于候选整理阶段。
  if (/准备账号选品任务|账号选品执行中|开始顺序处理账号|等待账号执行/.test(stage)) return 3
  if (/完成|处理结束/.test(stage)) return steps.length - 1
  return 0
}

const selectionWorkflowSteps = computed(() => {
  const progress = selectionProgress.value
  return progress?.mode === 'preview' ? PREVIEW_SELECTION_WORKFLOW : BATCH_SELECTION_WORKFLOW
})

const selectionWorkflowStageIndex = computed(() => {
  const progress = selectionProgress.value
  if (!progress) return 0
  return workflowIndexForStage(progress.stage, progress.mode)
})

const selectionWorkflowStagePercent = computed(() => {
  const progress = selectionProgress.value
  if (!progress) return null
  if (typeof progress.percent === 'number' && Number.isFinite(progress.percent)) {
    return Math.max(0, Math.min(100, Math.round(progress.percent)))
  }
  if (progress.itemTotal !== null && progress.itemTotal > 0 && progress.itemIndex !== null) {
    const completedItems = Math.max(0, Math.min(progress.itemTotal, progress.itemIndex - 1))
    return Math.floor(completedItems * 100 / progress.itemTotal)
  }
  if (typeof progress.stepTotal === 'number' && progress.stepTotal > 0 && typeof progress.stepIndex === 'number') {
    return Math.floor(Math.max(0, Math.min(progress.stepTotal, progress.stepIndex)) * 100 / progress.stepTotal)
  }
  return null
})

const selectionWorkflowPercent = computed(() => {
  const progress = selectionProgress.value
  if (!progress) return 0
  if (progress.state === 'finished' && progress.percent === 100) return 100
  const steps = selectionWorkflowSteps.value
  const stageIndex = Math.max(0, Math.min(steps.length - 1, selectionWorkflowStageIndex.value))
  const priorWeight = steps.slice(0, stageIndex).reduce((sum, step) => sum + step.weight, 0)
  const stageWeight = steps[stageIndex]?.weight ?? 0
  const stagePercent = selectionWorkflowStagePercent.value ?? 0

  if (progress.mode === 'batch' && stageIndex >= 6) {
    // 视频与队列按商品串行处理；商品序号参与计算，避免每处理下一个商品时总进度倒退。
    const itemTotal = Math.max(1, progress.itemTotal ?? 1)
    const itemIndex = Math.max(1, Math.min(itemTotal, progress.itemIndex ?? 1))
    const itemPhase = stageIndex === 6
      ? 0.7 * stagePercent / 100
      : 0.7 + 0.3 * stagePercent / 100
    const completedBeforeProducts = steps.slice(0, 6).reduce((sum, step) => sum + step.weight, 0)
    const productPhaseWeight = 100 - completedBeforeProducts
    return Math.max(0, Math.min(100, Math.round(
      completedBeforeProducts + productPhaseWeight * ((itemIndex - 1 + itemPhase) / itemTotal)
    )))
  }
  return Math.max(0, Math.min(100, Math.round(priorWeight + stageWeight * stagePercent / 100)))
})

const selectionWorkflowStepStates = computed(() => selectionWorkflowSteps.value.map((step, index) => {
  const progress = selectionProgress.value
  const currentIndex = selectionWorkflowStageIndex.value
  const isFullyFinished = progress?.state === 'finished' && progress.percent === 100
  const status = isFullyFinished || index < currentIndex
    ? 'completed'
    : index === currentIndex
      ? progress?.state === 'failed' || /失败/.test(progress?.stage ?? '') ? 'failed' : progress?.state === 'finished' ? 'ended' : 'running'
      : 'pending'
  const statusText = status === 'completed' ? '已完成'
    : status === 'running' ? '进行中'
      : status === 'failed' ? '失败'
        : status === 'ended' ? '本轮结束'
          : '待开始'
  return { ...step, status, statusText }
}))

function armSelectionCooldown(seconds: number, deadlineOverride?: number): void {
  selectionCooldownArmed.value = true
  selectionCooldownRemaining.value = Math.max(0, Math.ceil(seconds))
  if (selectionCooldownTimer !== null) window.clearInterval(selectionCooldownTimer)
  selectionCooldownTimer = null
  selectionCooldownDeadline = deadlineOverride ?? Date.now() + selectionCooldownRemaining.value * 1000
  const deadline = selectionCooldownDeadline
  const batchId = recoveryBatch.value?.batchId
  if (batchId && !props.dialogOnly) {
    void window.api.selectionCooldown.schedule({ batchId, deadline }).then((result) => {
      void window.api.selectionCooldown.trace({
        batchId,
        stage: 'renderer_deadline_schedule_result',
        details: { ok: result.ok, deadline }
      }).catch(() => undefined)
      if (!result.ok) notifyGlobal(result.error ?? '冷却自动续选计时未能启动。', 'error')
    }).catch((cause) => {
      notifyGlobal('冷却自动续选计时服务暂不可用：' + String(cause instanceof Error ? cause.message : cause), 'error')
    })
  }
  selectionCooldownRemaining.value = Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
  if (selectionCooldownRemaining.value <= 0) {
    selectionCooldownRemaining.value = 0
    return
  }
  selectionCooldownTimer = window.setInterval(() => {
    selectionCooldownRemaining.value = Math.max(0, Math.ceil(((selectionCooldownDeadline ?? Date.now()) - Date.now()) / 1000))
    if (selectionCooldownRemaining.value === 0 && selectionCooldownTimer !== null) {
      window.clearInterval(selectionCooldownTimer)
      selectionCooldownTimer = null
    }
  }, 1000)
}

function refreshCooldownFromDeadline(): void {
  if (!selectionCooldownArmed.value || selectionCooldownDeadline === null) return
  selectionCooldownRemaining.value = Math.max(0, Math.ceil((selectionCooldownDeadline - Date.now()) / 1000))
  if (selectionCooldownRemaining.value === 0 && selectionCooldownTimer !== null) {
    window.clearInterval(selectionCooldownTimer)
    selectionCooldownTimer = null
  }
}

function traceSelectionCooldown(
  stage: string,
  batchId: string,
  details: Record<string, unknown> = {}
): void {
  void window.api.selectionCooldown.trace({ batchId, stage, details }).catch(() => undefined)
}

function acknowledgeSelectionCooldownEvent(
  event: { batchId: string; deadline: number },
  resumeQueued = false
): void {
  void window.api.selectionCooldown.acknowledge({
    batchId: event.batchId, deadline: event.deadline, resumeQueued
  }).catch(() => undefined)
}

function onSelectionCooldownExpired(event: { batchId: string; deadline: number; sendCount: number }): void {
  if (props.dialogOnly) {
    acknowledgeSelectionCooldownEvent(event)
    return
  }
  if (event.batchId !== recoveryBatch.value?.batchId) {
    traceSelectionCooldown('renderer_expiry_event_ignored', event.batchId, {
      reason: 'batch_id_mismatch', currentBatchId: recoveryBatch.value?.batchId ?? null,
      deadline: event.deadline, sendCount: event.sendCount
    })
    acknowledgeSelectionCooldownEvent(event)
    return
  }
  if (completedCooldownAutoResume?.batchId === event.batchId &&
    completedCooldownAutoResume.deadline === event.deadline) {
    traceSelectionCooldown('renderer_expiry_after_resume', event.batchId, {
      accountId: completedCooldownAutoResume.accountId, deadline: event.deadline,
      sendCount: event.sendCount, resumeAlreadyStarted: true
    })
    acknowledgeSelectionCooldownEvent(event)
    return
  }
  const expectedDeadline = selectionCooldownDeadline ?? cooldownAutoResumeIntent.value?.deadline ?? null
  if (expectedDeadline !== null && event.deadline !== expectedDeadline) {
    traceSelectionCooldown('renderer_expiry_event_ignored', event.batchId, {
      reason: 'deadline_mismatch', expectedDeadline,
      receivedDeadline: event.deadline, sendCount: event.sendCount
    })
    acknowledgeSelectionCooldownEvent(event)
    return
  }
  refreshCooldownFromDeadline()
  selectionCooldownRemaining.value = Math.max(0, Math.ceil((event.deadline - Date.now()) / 1000))
  if (selectionCooldownRemaining.value > 0) {
    // 系统时钟若在排程期间向前回拨，以原 deadline 重新交给主进程等待。
    traceSelectionCooldown('renderer_expiry_before_local_deadline', event.batchId, {
      deadline: event.deadline, localDeadline: selectionCooldownDeadline,
      remainingSeconds: selectionCooldownRemaining.value
    })
    void window.api.selectionCooldown.schedule({ batchId: event.batchId, deadline: event.deadline }).catch((cause) => {
      notifyGlobal('冷却自动续选计时服务暂不可用：' + String(cause instanceof Error ? cause.message : cause), 'error')
    })
    return
  }
  selectionCooldownRemaining.value = 0
  const state = currentAutoResumeState()
  let intent = cooldownAutoResumeIntent.value
  if (!intent && state.batchState === 'needs_retry' && state.rateLimitedAccountId !== null &&
    state.pendingAccountIds.includes(state.rateLimitedAccountId)) {
    const limitedAccount = recoveryBatch.value?.accounts.find((account) =>
      account.accountId === state.rateLimitedAccountId && account.state === 'needs_retry' &&
      toUserSelectionError(account.error).classification === 'BAIYING_RATE_LIMIT'
    )
    if (limitedAccount) {
      intent = {
        batchId: event.batchId, accountId: limitedAccount.accountId,
        deadline: event.deadline, attempts: 0, phase: 'pending'
      }
      cooldownAutoResumeIntent.value = intent
    }
  }
  const decision = intent
    ? evaluateSelectionCooldownResume({
      intent, batchId: recoveryBatch.value?.batchId,
      pendingAccountIds: state.pendingAccountIds,
      busy: state.busy, now: Date.now()
    })
    : null
  if (event.sendCount === 1) {
    traceSelectionCooldown('renderer_expiry_event_received', event.batchId, {
      deadline: event.deadline, sendCount: event.sendCount,
      cooldownArmed: state.cooldownArmed, cooldownRemaining: state.cooldownRemaining,
      busy: state.busy, batchState: state.batchState ?? null,
      rateLimitedAccountId: state.rateLimitedAccountId,
      pendingAccountIds: [...state.pendingAccountIds], eligible: decision === 'start',
      resumeIntentDecision: decision
    })
  }
  if (decision === 'already_starting') {
    traceSelectionCooldown('renderer_auto_resume_already_starting', event.batchId, {
      accountId: intent?.accountId ?? null, pendingAccountIds: [...state.pendingAccountIds]
    })
    acknowledgeSelectionCooldownEvent(event, true)
    return
  }
  if (decision === 'waiting_for_busy_task') {
    if (intent) cooldownAutoResumeIntent.value = { ...intent, awaitingExpiryAck: true }
    if (event.sendCount === 1) traceSelectionCooldown('renderer_expiry_waiting_for_busy_task', event.batchId, {
      batchState: state.batchState ?? null, rateLimitedAccountId: state.rateLimitedAccountId,
      accountId: intent?.accountId ?? null, intentPreserved: Boolean(intent)
    })
    return
  }
  if (decision === 'waiting_for_deadline') {
    traceSelectionCooldown('renderer_expiry_waiting_for_retry_deadline', event.batchId, {
      accountId: intent?.accountId ?? null, deadline: intent?.deadline ?? event.deadline,
      retryAt: intent?.retryAt ?? null
    })
    void window.api.selectionCooldown.schedule({
      batchId: event.batchId, deadline: Math.max(intent?.deadline ?? event.deadline, intent?.retryAt ?? 0)
    }).catch((cause) => {
      notifyGlobal('冷却自动续选计时服务暂不可用：' + String(cause instanceof Error ? cause.message : cause), 'error')
    })
    return
  }
  if (decision === 'start') {
    const queued = scheduleCooldownAutoResume()
    traceSelectionCooldown('renderer_auto_resume_enqueued', event.batchId, {
      queued, rateLimitedAccountId: intent?.accountId ?? null, pendingAccountIds: [...state.pendingAccountIds],
      resumeIntentDecision: decision
    })
    if (queued) acknowledgeSelectionCooldownEvent(event, true)
    return
  }
  if (decision === 'blocked') {
    traceSelectionCooldown('renderer_auto_resume_blocked', event.batchId, {
      accountId: intent?.accountId ?? null, reason: 'login_or_manual_gate'
    })
    acknowledgeSelectionCooldownEvent(event)
    return
  }
  if (decision === 'stale_batch' || decision === 'stale_account') {
    traceSelectionCooldown('renderer_auto_resume_intent_stale', event.batchId, {
      decision, intentBatchId: intent?.batchId ?? null, intentAccountId: intent?.accountId ?? null,
      currentBatchId: recoveryBatch.value?.batchId ?? null, pendingAccountIds: [...state.pendingAccountIds]
    })
    cooldownAutoResumeIntent.value = null
    acknowledgeSelectionCooldownEvent(event)
    return
  }
  traceSelectionCooldown('renderer_auto_resume_ineligible', event.batchId, {
    cooldownArmed: state.cooldownArmed, cooldownRemaining: state.cooldownRemaining,
    batchState: state.batchState ?? null, rateLimitedAccountId: state.rateLimitedAccountId,
    pendingAccountIds: [...state.pendingAccountIds], resumeIntentDecision: decision
  })
  acknowledgeSelectionCooldownEvent(event)
}

function restoreSelectionCooldown(batch: SelectionRecoveryBatch | null): boolean {
  const limitedAccount = batch?.accounts.find((account) =>
    account.state === 'needs_retry' &&
    toUserSelectionError(account.error).classification === 'BAIYING_RATE_LIMIT'
  )
  if (!limitedAccount) {
    const intent = cooldownAutoResumeIntent.value
    const stillPending = Boolean(intent && intent.batchId === batch?.batchId &&
      batch?.accounts.some((account) => account.accountId === intent.accountId && account.state !== 'completed'))
    if (stillPending && intent) {
      autoResumeAccountId.value = intent.accountId
      return false
    }
    if (intent?.batchId !== batch?.batchId || intent?.phase !== 'starting') cooldownAutoResumeIntent.value = null
    autoResumeAccountId.value = null
    return false
  }
  autoResumeAccountId.value = limitedAccount.accountId
  const duration = toUserSelectionError(limitedAccount.error).cooldownSeconds ?? 60
  const updatedAt = limitedAccount.updatedAt ? Date.parse(limitedAccount.updatedAt) : Number.NaN
  const deadline = Number.isFinite(updatedAt) ? updatedAt + duration * 1000 : Date.now() + duration * 1000
  const currentIntent = cooldownAutoResumeIntent.value
  if (!currentIntent || currentIntent.batchId !== batch?.batchId || currentIntent.accountId !== limitedAccount.accountId ||
    (currentIntent.phase === 'starting' && deadline > currentIntent.deadline)) {
    cooldownAutoResumeIntent.value = {
      batchId: batch?.batchId ?? '', accountId: limitedAccount.accountId,
      deadline, attempts: 0, phase: 'pending'
    }
    completedCooldownAutoResume = null
  }
  armSelectionCooldown(Math.max(0, Math.ceil((deadline - Date.now()) / 1000)), deadline)
  return true
}

watch(notice, (message) => {
  if (!message) return
  const presentation = toUserSelectionError(message)
  if (presentation.classification === 'BAIYING_RATE_LIMIT') {
    if (!restoreSelectionCooldown(recoveryBatch.value)) armSelectionCooldown(presentation.cooldownSeconds ?? 60)
  }
  if (selectionProgress.value?.state !== 'running') notifyGlobal(presentation.userMessage)
})
watch(error, (message) => {
  if (!message) return
  const presentation = toUserSelectionError(message)
  if (presentation.classification === 'BAIYING_RATE_LIMIT') {
    if (!restoreSelectionCooldown(recoveryBatch.value)) armSelectionCooldown(presentation.cooldownSeconds ?? 60)
  }
  notifyGlobal(presentation.userMessage, 'error')
})

const recoveryBatch = ref<SelectionRecoveryBatch | null>(null)
const recoverySelectionBatchId = ref<string | null>(null)
const recoverySelectedAccountIds = ref<number[]>([])
const pendingRecoveryAccounts = computed(() =>
  recoveryBatch.value?.accounts.filter((account) => account.state !== 'completed') ?? []
)
const completedRecoveryAccounts = computed(() =>
  recoveryBatch.value?.accounts.filter((account) => account.state === 'completed') ?? []
)
const selectedRecoveryAccounts = computed(() => filterPendingRecoveryAccounts(
  recoveryBatch.value?.accounts ?? [],
  new Set(recoverySelectedAccountIds.value)
))
watch(() => recoveryBatch.value?.batchId, (batchId) => {
  recoverySelectionBatchId.value = batchId ?? null
  recoverySelectedAccountIds.value = batchId
    ? recoveryBatch.value?.accounts.filter((account) => account.state !== 'completed').map((account) => account.accountId) ?? []
    : []
}, { flush: 'sync' })
function selectionLogLevel(entry: SelectionRunLog): SelectionLogLevel {
  const code = String(entry.code ?? '')
  if (code.startsWith('SUCCESS_')) return 'success'
  if (code.startsWith('INFO_')) return 'info'
  if (code.startsWith('WARNING_') || [
    'PRODUCT_ANALYSIS_FILTERED', 'PRODUCT_SKIPPED', 'CATEGORY_QUERY_SKIPPED',
    'PRIORITY_SOURCE_READ_SKIPPED', 'DOUYIN_PRODUCT_ALREADY_QUEUED'
  ].includes(code)) return 'warning'
  return 'error'
}

const visibleRunLogs = computed<SelectionRunLog[]>(() => {
  const batch = recoveryBatch.value
  if (!batch) return []
  const savedLogs = batch.runLogs?.length ? batch.runLogs : batch.failureLogs ?? batch.runLogs ?? []
  const legacyLogs = batch.accounts.flatMap((account) => {
    const productFailures = (account.completed ?? []).filter((item) => {
      const status = String(item.status ?? '')
      return item.cartAdded !== true || Boolean(item.error) || Boolean(item.manualAction) ||
        /失败|无法打开|未能|超时|未确认|不匹配|缺少|没有返回|未捕获|尚未/.test(status)
    })
    if (productFailures.length) {
      return productFailures.map((item, index) => {
        const status = String(item.status ?? item.error ?? '商品处理未完成')
        return {
          id: -(account.accountId * 10000 + index + 1),
          batchId: batch.batchId,
          accountId: account.accountId,
          accountName: account.accountName,
          productId: String(item.productId ?? ''),
          productTitle: String(item.title ?? ''),
          stage: item.analysisRejected === true ? '商品数据分析' : item.cartAdded === true ? '历史商品处理' : '历史加车状态',
          code: item.analysisRejected === true ? 'WARNING_PRODUCT_ANALYSIS_FILTERED' : item.skipped === true ? 'WARNING_PRODUCT_SKIPPED' : 'SAVED_CHECKPOINT',
          message: conciseFailureMessage(status),
          detail: String(item.error ?? status).slice(0, 8000),
          createdAt: account.updatedAt ?? batch.updatedAt ?? '',
          legacy: true
        }
      })
    }
    if (!account.error) return []
    return [{
      id: -(account.accountId * 10000),
      batchId: batch.batchId,
      accountId: account.accountId,
      accountName: account.accountName,
      productId: '',
      productTitle: '',
      stage: account.stage || '账号任务',
      code: 'SAVED_CHECKPOINT',
      message: '此账号检查点已保存错误；新格式日志将在后续失败时追加。',
      detail: account.error.slice(0, 8000),
      createdAt: account.updatedAt ?? batch.updatedAt ?? '',
      legacy: true
    }]
  })
  const unmatchedLegacyLogs = legacyLogs.filter((legacy) => !savedLogs.some((saved) =>
    saved.accountId === legacy.accountId && (!legacy.productId || saved.productId === legacy.productId)
  ))
  return [...savedLogs, ...unmatchedLegacyLogs].sort((left, right) => {
    const timeOrder = left.createdAt.localeCompare(right.createdAt)
    return timeOrder || left.id - right.id
  })
})

const visibleRunLogCounts = computed(() => {
  const counts: Record<SelectionLogLevel, number> = { success: 0, info: 0, warning: 0, error: 0 }
  for (const entry of visibleRunLogs.value) counts[selectionLogLevel(entry)] += 1
  return counts
})
const accounts = ref<AccountRow[]>([])
const selectionScopeAccounts = computed(() => accounts.value)
const accountStatuses = reactive<Record<number, string>>({})
const loginPreflight = ref<LoginPreflightDialog | null>(null)
const openingLoginTarget = ref<string | null>(null)
let verifiedLoginPreflight: { fingerprint: string; expiresAt: number } | null = null
let scopeConfirmationToResume: { fingerprint: string; expiresAt: number } | null = null

function currentAutoResumeState() {
  const batch = recoveryBatch.value
  return {
    cooldownArmed: selectionCooldownArmed.value && !props.dialogOnly,
    cooldownRemaining: selectionCooldownRemaining.value,
    busy: busy.value || loginPreflight.value !== null,
    batchState: batch?.state,
    rateLimitedAccountId: cooldownAutoResumeIntent.value?.accountId ?? autoResumeAccountId.value,
    pendingAccountIds: batch?.accounts.filter((account) => account.state !== 'completed').map((account) => account.accountId) ?? []
  }
}

function clearAutoResumeTimer(): void {
  if (selectionAutoResumeTimer !== null) window.clearTimeout(selectionAutoResumeTimer)
  selectionAutoResumeTimer = null
}

function cooldownAutoResumeDecision(intent: CooldownAutoResumeIntent) {
  const state = currentAutoResumeState()
  return evaluateSelectionCooldownResume({
    intent, batchId: recoveryBatch.value?.batchId,
    pendingAccountIds: state.pendingAccountIds,
    busy: state.busy,
    now: Date.now()
  })
}

function scheduleCooldownAutoResume(): boolean {
  const state = currentAutoResumeState()
  let intent = cooldownAutoResumeIntent.value
  if (!intent && canAutoResumeSelection(state)) {
    const batchId = recoveryBatch.value?.batchId
    const accountId = state.rateLimitedAccountId
    if (batchId && accountId !== null) {
      intent = {
        batchId, accountId, deadline: selectionCooldownDeadline ?? Date.now(),
        attempts: 0, phase: 'pending'
      }
      cooldownAutoResumeIntent.value = intent
    }
  }
  if (!intent) {
    clearAutoResumeTimer()
    return false
  }
  const decision = cooldownAutoResumeDecision(intent)
  if (decision === 'waiting_for_deadline' || decision === 'waiting_for_busy_task' ||
    decision === 'already_starting' || decision === 'blocked') return false
  if (decision === 'stale_batch' || decision === 'stale_account') {
    traceSelectionCooldown('renderer_auto_resume_intent_stale', intent.batchId, {
      decision, intentAccountId: intent.accountId,
      currentBatchId: recoveryBatch.value?.batchId ?? null,
      pendingAccountIds: currentAutoResumeState().pendingAccountIds
    })
    cooldownAutoResumeIntent.value = null
    return false
  }
  if (selectionAutoResumeTimer !== null) return true
  const batchId = intent.batchId
  const resumeAccountId = intent.accountId
  selectionAutoResumeTimer = window.setTimeout(() => {
    selectionAutoResumeTimer = null
    if (selectionViewUnmounted) {
      if (batchId) traceSelectionCooldown('renderer_auto_resume_skipped', batchId, { reason: 'view_unmounted' })
      return
    }
    const latestIntent = cooldownAutoResumeIntent.value
    const latestDecision = latestIntent ? cooldownAutoResumeDecision(latestIntent) : 'stale_account'
    if (!latestIntent || latestDecision !== 'start') {
      if (batchId) traceSelectionCooldown('renderer_auto_resume_skipped', batchId, {
        reason: 'intent_changed_before_callback', decision: latestDecision,
        intent: latestIntent ?? null, ...currentAutoResumeState()
      })
      if (latestIntent && (latestDecision === 'stale_batch' || latestDecision === 'stale_account' || latestDecision === 'blocked')) {
        cooldownAutoResumeIntent.value = null
        acknowledgeSelectionCooldownEvent({ batchId, deadline: latestIntent.deadline })
      }
      return
    }
    const batch = recoveryBatch.value
    if (!batch || batch.batchId !== batchId) {
      if (batchId) traceSelectionCooldown('renderer_auto_resume_skipped', batchId, {
        reason: 'batch_changed_before_callback', currentBatchId: batch?.batchId ?? null
      })
      return
    }
    const pendingIds = batch.accounts.filter((account) => account.state !== 'completed').map((account) => account.accountId)
    if (!pendingIds.includes(resumeAccountId)) {
      traceSelectionCooldown('renderer_auto_resume_skipped', batchId, {
        reason: 'rate_limited_account_not_pending', rateLimitedAccountId: resumeAccountId,
        pendingAccountIds: pendingIds
      })
      cooldownAutoResumeIntent.value = null
      acknowledgeSelectionCooldownEvent({ batchId, deadline: latestIntent.deadline })
      return
    }
    if (latestIntent.awaitingExpiryAck) {
      acknowledgeSelectionCooldownEvent({ batchId, deadline: latestIntent.deadline }, true)
    }
    cooldownAutoResumeIntent.value = { ...latestIntent, phase: 'starting' }
    autoResumeAccountId.value = resumeAccountId
    traceSelectionCooldown('renderer_auto_resume_started', batchId, {
      rateLimitedAccountId: resumeAccountId, pendingAccountIds: pendingIds,
      deadline: latestIntent.deadline, priorBatchState: batch.state,
      cooldownArmed: selectionCooldownArmed.value
    })
    automaticResumeBatchId = batch.batchId
    automaticResumeAccountId = resumeAccountId
    recoverySelectionBatchId.value = batch.batchId
    recoverySelectedAccountIds.value = pendingIds
    void startSelection()
  }, 0)
  return true
}

watch([
  selectionCooldownArmed,
  selectionCooldownRemaining,
  busy,
  autoResumeAccountId,
  cooldownAutoResumeIntent,
  loginPreflight,
  () => recoveryBatch.value?.batchId,
  () => recoveryBatch.value?.state,
  () => recoveryBatch.value?.accounts.map((account) => `${account.accountId}:${account.state}`).join('|')
], scheduleCooldownAutoResume, { flush: 'post' })

function reportSelectionProgress(
  accountId: number,
  stage: string,
  update: Partial<SelectionProgressState> = {}
): void {
  const current = selectionProgress.value
  const accountName = accounts.value.find((account) => account.id === accountId)?.name ?? `账号 ${accountId}`
  selectionProgress.value = {
    ...(current ?? {
      state: 'running' as const,
      mode: 'batch' as const,
      accountId,
      accountName,
      accountIndex: 1,
      accountTotal: 1,
      completedAccounts: 0,
      stage,
      message: '',
      stepIndex: null,
      stepTotal: null,
      stepUnit: '',
      itemIndex: null,
      itemTotal: null,
      percent: null
    }),
    ...update,
    accountId,
    accountName,
    stage,
    message: update.message ?? '',
    stepIndex: update.stepIndex ?? null,
    stepTotal: update.stepTotal ?? null,
    stepUnit: update.stepUnit ?? '',
    itemIndex: update.itemIndex ?? null,
    itemTotal: update.itemTotal ?? null,
    percent: update.percent ?? null,
    indeterminate: update.indeterminate ?? false
  }
}
const candidates = ref<SelectionCandidate[]>([])
const categoryRecommendations = ref<Array<{ categoryId: string; highScoreCount: number; averageScore: number }>>([])
const previewAccountId = ref<number | null>(null)
const candidateSelectionCustomized = ref(false)
const accountId = ref<number | null>(props.initialAccountId)
const activeTab = ref<'通用设置' | '账号个性化'>(props.initialAccountId === null ? '通用设置' : '账号个性化')
const categories = ref<CategoryNode[]>([])
const liveCategoryRoots = ref<CategoryNode[]>([])
const filterGroups = ref<Record<string, FilterField[]>>({})
const featureOptions = ref<string[]>([])
const filterCatalogData = ref<Record<string, unknown>>({})
const filterCatalogUpdatedAt = ref('')
const defaultDownloadRoot = ref('')
const browserVisible = ref(false)
const browserProfileKey = computed(() => accountId.value !== null ? `account-${accountId.value}` : 'general')
const global = reactive(structuredClone(DEFAULTS))
const globalFilterSettings = computed<SelectionFilterSettings>({
  get: () => ({
    candidateLimit: global.candidateLimit,
    filters: global.filters,
    poolPriority: global.poolPriority,
    poolFallback: global.poolFallback,
    sources: global.sources,
    thresholds: global.thresholds,
    trendPreference: global.trendPreference
  }),
  set: (value) => {
    const normalized = createSelectionFilterSettings(value)
    global.candidateLimit = normalized.candidateLimit
    global.filters = normalized.filters
    global.poolPriority = normalized.poolPriority
    global.poolFallback = normalized.poolFallback
    global.sources = normalized.sources
    global.thresholds = normalized.thresholds
    global.trendPreference = normalized.trendPreference
  }
})
const accountDraft = reactive({
  selectionMode: 'inherit' as 'inherit' | 'general' | 'category',
  categoryMode: 'inherit' as 'inherit' | 'auto' | 'manual',
  categorySelection: { level1: [] as string[], level2: [] as string[], level3: [] as string[] },
  browserProfilePath: '',
  downloadPath: '',
  dailyQuotaOverride: { enabled: false, perAccountProducts: 5, perAccountVideos: 50 }
})
const accountFilterOverridesEnabled = ref(false)
const accountFilterDraft = ref<SelectionFilterSettings>(createSelectionFilterSettings())
const savedAccountPaths = reactive({ browserProfilePath: '', downloadPath: '' })
const accountPathsHaveUnsavedChanges = computed(() =>
  accountDraft.browserProfilePath !== savedAccountPaths.browserProfilePath ||
  accountDraft.downloadPath !== savedAccountPaths.downloadPath
)
const bannedWordsText = ref(DEFAULT_BANNED_WORDS.join('\n'))
const bannedWordsDialogOpen = ref(false)

function runEngine(
  script: string,
  args: string[],
  stdinJson?: unknown,
  timeoutMs = 30_000,
  onProgress?: (event: EngineEventPayload) => void
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let settled = false
    let taskId: string | null = null
    const pendingEvents: EngineEventPayload[] = []
    let off: () => void = () => undefined
    const finish = (callback: () => void): void => {
      if (settled) return
      settled = true
      off()
      callback()
    }
    const handleEvent = (event: EngineEventPayload): void => {
      if (event.taskId !== taskId) return
      if (event.type === 'stage') onProgress?.(event)
      if (event.type === 'result') finish(() => resolve((event.data ?? {}) as Record<string, unknown>))
      if (event.type === 'error') finish(() => reject(new Error(event.message ?? '引擎执行失败')))
    }
    off = window.api.engine.onEvent((event) => {
      if (!taskId) pendingEvents.push(event)
      else handleEvent(event)
    })
    window.setTimeout(() => finish(() => reject(new Error(`引擎命令超时（${Math.round(timeoutMs / 1000)} 秒）`))), timeoutMs)
    // Electron IPC 使用结构化克隆；Vue 的 reactive/ref 对象不能直接跨进程传递。
    const plainStdinJson = stdinJson === undefined ? undefined : JSON.parse(JSON.stringify(stdinJson)) as unknown
    void window.api.engine.run({ script, args, stdinJson: plainStdinJson }).then((reply) => {
      if (!reply.taskId) finish(() => reject(new Error(reply.error ?? '引擎任务启动失败')))
      else {
        taskId = reply.taskId
        pendingEvents.splice(0).forEach(handleEvent)
      }
    })
  })
}

interface ProductPublishLock {
  productId: string
  productName?: string
  taskIds: number[]
  queueStatuses: string[]
  platformStatuses: string[]
}

async function loadProductPublishLocks(accountId: number): Promise<Map<string, ProductPublishLock>> {
  const result = await runEngine('publish_queue.py', [
    '--db', DB_PATH, 'product-locks', '--account-id', String(accountId)
  ])
  if (result.ok !== true) throw new Error(String(result.error ?? '读取账号商品发布状态失败，已停止选品'))
  const locks = Array.isArray(result.locks) ? result.locks as ProductPublishLock[] : []
  return new Map(locks
    .filter((lock) => typeof lock.productId === 'string' && lock.productId.length > 0)
    .map((lock) => [lock.productId, lock]))
}

function productPublishLockLabel(lock: ProductPublishLock): string {
  const taskIds = Array.isArray(lock.taskIds) ? lock.taskIds : []
  const platformStatuses = Array.isArray(lock.platformStatuses) ? lock.platformStatuses : []
  const taskText = taskIds.length ? `队列任务 #${taskIds.join('、')}` : '已有发布队列任务'
  const published = platformStatuses.includes('published') ? '已发布' : '正在处理或等待发布'
  return `本账号${published}（${taskText}），本次已跳过`
}

function duplicateProductSkipItems(
  candidates: readonly SelectionCandidate[],
  locks: ReadonlyMap<string, ProductPublishLock>
): Array<Record<string, unknown>> {
  return candidates.flatMap((candidate) => {
    const lock = locks.get(candidate.productId)
    if (!lock) return []
    return [{
      productId: candidate.productId,
      title: candidate.title,
      skipped: true,
      cartAdded: false,
      status: productPublishLockLabel(lock),
      publishQueueTaskIds: lock.taskIds
    }]
  })
}

async function logDuplicateProductSkips(
  accountId: number,
  items: readonly Record<string, unknown>[],
  recovery?: SelectionRecoveryHooks
): Promise<void> {
  for (const item of items) {
    const productId = String(item.productId ?? '')
    const taskIds = Array.isArray(item.publishQueueTaskIds) ? item.publishQueueTaskIds.map(String) : []
    await recovery?.logFailure({
      stage: '选品去重', code: 'DOUYIN_PRODUCT_ALREADY_QUEUED',
      message: conciseFailureMessage(item.status),
      detail: `账号 ${accountId} 的商品 ${productId} 已关联发布任务：${taskIds.join(', ')}`,
      productId,
      productTitle: String(item.title ?? '')
    })
  }
}

/** 在 Vue 渲染上下文中先去掉响应式代理，再跨 contextBridge 调用 preload。 */
function toPlainSelectionPayload<T>(value: T): T {
  const serialized = JSON.stringify(value)
  if (serialized === undefined) throw new Error('百应选品参数无法转换为普通数据。')
  return JSON.parse(serialized) as T
}

function runRecovery(args: string[], stdinJson?: unknown): Promise<Record<string, unknown>> {
  return runEngine('selection_recovery.py', ['--db', DB_PATH, ...args], stdinJson)
}

function conciseFailureMessage(value: unknown): string {
  if (value === null || value === undefined || (typeof value === 'string' && !value.trim())) return ''
  const normalized = toUserSelectionMessage(value).replace(/\s+/g, ' ').trim()
  return normalized.length > 260 ? normalized.slice(0, 257) + '…' : normalized
}

function cloneDefault(): typeof DEFAULTS {
  return structuredClone(DEFAULTS)
}

function normalizeCategories(value: unknown): CategoryNode[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    const id = String(record.id ?? '')
    const name = String(record.name ?? '')
    if (!id || !name) return []
    return [{ id, name, children: normalizeCategories(record.children) }]
  })
}

const CATEGORY_PATH_SEPARATOR = '__RC_CASCADER_SPLIT__'

function sameCategoryId(left: string, right: string): boolean {
  if (left === right) return true
  const leftHasPath = left.includes(CATEGORY_PATH_SEPARATOR)
  const rightHasPath = right.includes(CATEGORY_PATH_SEPARATOR)
  if (leftHasPath === rightHasPath) return false
  return leftHasPath
    ? left.endsWith(`${CATEGORY_PATH_SEPARATOR}${right}`)
    : right.endsWith(`${CATEGORY_PATH_SEPARATOR}${left}`)
}

function mergeCategoryTrees(primary: CategoryNode[], fallback: CategoryNode[]): CategoryNode[] {
  const merged: CategoryNode[] = []
  const addNode = (node: CategoryNode): void => {
    const current = merged.find((item) => sameCategoryId(item.id, node.id))
    if (!current) {
      const copy = { ...node, children: mergeCategoryTrees(node.children, []) }
      merged.push(copy)
      return
    }
    // 实时百应目录使用带父路径 ID；旧缓存可能只有短 ID，保留实时路径以便精准应用筛选。
    if (node.id.includes(CATEGORY_PATH_SEPARATOR) && !current.id.includes(CATEGORY_PATH_SEPARATOR)) {
      current.id = node.id
    }
    current.name = node.name
    current.children = mergeCategoryTrees(current.children, node.children)
  }
  primary.forEach(addNode)
  fallback.forEach(addNode)
  return merged
}

function normalizeCategorySelection(
  selection: { level1: string[]; level2: string[]; level3: string[] },
  tree: CategoryNode[]
): { level1: string[]; level2: string[]; level3: string[] } {
  const resolveIds = (ids: string[], options: CategoryNode[]): string[] => [...new Set(ids.map((id) =>
    options.find((item) => sameCategoryId(item.id, id))?.id ?? id
  ))]
  const level1 = resolveIds(selection.level1, tree)
  const selectedRoots = new Set(level1)
  const secondOptions = tree.filter((node) => selectedRoots.has(node.id)).flatMap((node) => node.children)
  const level2 = resolveIds(selection.level2, secondOptions)
  const selectedSecond = new Set(level2)
  const thirdOptions = secondOptions.filter((node) => selectedSecond.has(node.id)).flatMap((node) => node.children)
  const level3 = resolveIds(selection.level3, thirdOptions)
  return { level1, level2, level3 }
}

function normalizeCurrentCategorySelections(): void {
  global.categorySelection = normalizeCategorySelection(global.categorySelection, categories.value)
  accountDraft.categorySelection = normalizeCategorySelection(accountDraft.categorySelection, categories.value)
}

function pruneCategorySelectionToCatalog(
  selection: { level1: string[]; level2: string[]; level3: string[] },
  tree: CategoryNode[]
): { selection: { level1: string[]; level2: string[]; level3: string[] }; removed: number } {
  const resolveIds = (ids: string[], options: CategoryNode[]): string[] => [...new Set(ids
    .map((id) => options.find((item) => sameCategoryId(item.id, id))?.id)
    .filter((id): id is string => Boolean(id)))]
  const level1 = resolveIds(selection.level1, tree)
  const selectedRoots = new Set(level1)
  const secondOptions = tree.filter((node) => selectedRoots.has(node.id)).flatMap((node) => node.children)
  const level2 = resolveIds(selection.level2, secondOptions)
  const selectedSeconds = new Set(level2)
  const thirdOptions = secondOptions.filter((node) => selectedSeconds.has(node.id)).flatMap((node) => node.children)
  const level3 = resolveIds(selection.level3, thirdOptions)
  const next = { level1, level2, level3 }
  const beforeCount = selection.level1.length + selection.level2.length + selection.level3.length
  const afterCount = level1.length + level2.length + level3.length
  return { selection: next, removed: Math.max(0, beforeCount - afterCount) }
}

function pruneCurrentCategorySelections(): number {
  const globalResult = pruneCategorySelectionToCatalog(global.categorySelection, categories.value)
  const accountResult = pruneCategorySelectionToCatalog(accountDraft.categorySelection, categories.value)
  global.categorySelection = globalResult.selection
  accountDraft.categorySelection = accountResult.selection
  return globalResult.removed + accountResult.removed
}

async function loadGlobal(): Promise<void> {
  const result = await runEngine('selection_settings.py', ['--db', DB_PATH, 'get'])
  const settings = (result.settings ?? {}) as Record<string, unknown>
  Object.assign(global, cloneDefault(), settings)
  global.selectionMode = settings.selectionMode === 'category' ? 'category' : 'general'
  global.categoryMode = settings.categoryMode === 'manual' ? 'manual' : 'auto'
  const cat = (settings.categorySelection ?? {}) as Record<string, string[]>
  global.categorySelection = {
    level1: [...(cat.level1 ?? [])], level2: [...(cat.level2 ?? [])], level3: [...(cat.level3 ?? [])]
  }
  const savedProductFilters = { ...((settings.filters as Record<string, Record<string, unknown>> | undefined)?.product ?? {}) }
  delete savedProductFilters['佣金区间']
  global.filters = {
    delivery: { ...((settings.filters as Record<string, Record<string, unknown>> | undefined)?.delivery ?? {}) },
    product: savedProductFilters,
    features: Array.isArray((settings.filters as Record<string, unknown> | undefined)?.features)
      ? [...((settings.filters as Record<string, unknown[]>).features as string[])]
      : Object.entries(((settings.filters as Record<string, Record<string, unknown>> | undefined)?.features ?? {}))
        .filter(([, selected]) => Boolean(selected)).map(([label]) => label)
  }
  global.thresholds = { ...cloneDefault().thresholds, ...((settings.thresholds as typeof global.thresholds | undefined) ?? {}) }
  global.execution = { ...cloneDefault().execution, ...((settings.execution as typeof global.execution | undefined) ?? {}) }
  global.execution.publishMode = '自动'
  const savedQuota = (settings.quota ?? {}) as Record<string, unknown>
  const defaultQuota = cloneDefault().quota
  const positiveQuota = (value: unknown, fallback: number): number => {
    const parsed = Number(value)
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : fallback
  }
  global.quota = {
    perProductVideos: positiveQuota(savedQuota.perProductVideos, defaultQuota.perProductVideos),
    perAccountProducts: positiveQuota(savedQuota.perAccountProducts, defaultQuota.perAccountProducts),
    perAccountVideos: positiveQuota(savedQuota.perAccountVideos, defaultQuota.perAccountVideos)
  }
  global.poolPriority = [...((settings.poolPriority as string[] | undefined) ?? cloneDefault().poolPriority)]
  global.sources = createSelectionFilterSettings(settings).sources
  global.bannedWords = [...((settings.bannedWords as string[] | undefined) ?? DEFAULT_BANNED_WORDS)]
  bannedWordsText.value = global.bannedWords.join('\n')
  categories.value = mergeCategoryTrees(normalizeCategories(result.categories), [])
  normalizeCurrentCategorySelections()
}

async function loadAccounts(): Promise<void> {
  const result = await runEngine('account_manager.py', ['--db', DB_PATH, 'account-list'])
  accounts.value = (result.accounts ?? []) as AccountRow[]
  for (const account of accounts.value) {
    accountStatuses[account.id] = account.status === 'active' ? '待执行' : '已停用'
  }
  if (accountId.value === null || !isSelectionAccountInScope(accountId.value)) {
    const allowedAccounts = selectionScopeAccounts.value
    accountId.value = allowedAccounts.find((account) => account.status === 'active')?.id ?? allowedAccounts[0]?.id ?? null
  }
}

async function loadAccountSettings(): Promise<void> {
  if (accountId.value === null) return
  const id = accountId.value
  const result = await runEngine('selection_settings.py', ['--db', DB_PATH, 'get', '--account-id', String(id)])
  const settings = result.settings as { account?: Record<string, unknown>; inherited?: boolean } | undefined
  const value = settings?.account ?? {}
  accountDraft.selectionMode = value.selectionMode === 'general' || value.selectionMode === 'category'
    ? value.selectionMode : 'inherit'
  accountDraft.categoryMode = value.categoryMode === 'auto' || value.categoryMode === 'manual'
    ? value.categoryMode
    : 'inherit'
  const selected = (value.categorySelection ?? {}) as Record<string, string[]>
  accountDraft.categorySelection = {
    level1: [...(selected.level1 ?? [])], level2: [...(selected.level2 ?? [])], level3: [...(selected.level3 ?? [])]
  }
  accountDraft.categorySelection = normalizeCategorySelection(accountDraft.categorySelection, categories.value)
  accountDraft.browserProfilePath = String(value.browserProfilePath ?? '')
  accountDraft.downloadPath = String(value.downloadPath ?? '')
  const savedDailyQuota = (value.dailyQuotaOverride ?? {}) as Record<string, unknown>
  accountDraft.dailyQuotaOverride.enabled = savedDailyQuota.enabled === true
  const savedProducts = Number(savedDailyQuota.perAccountProducts)
  const savedVideos = Number(savedDailyQuota.perAccountVideos)
  accountDraft.dailyQuotaOverride.perAccountProducts = Number.isInteger(savedProducts) && savedProducts >= 1
    ? savedProducts : global.quota.perAccountProducts
  accountDraft.dailyQuotaOverride.perAccountVideos = Number.isInteger(savedVideos) && savedVideos >= 1
    ? savedVideos : global.quota.perAccountVideos
  const savedFilterOverrides = value.filterOverrides
  accountFilterOverridesEnabled.value = Boolean(savedFilterOverrides && typeof savedFilterOverrides === 'object' && !Array.isArray(savedFilterOverrides))
  accountFilterDraft.value = createSelectionFilterSettings(
    accountFilterOverridesEnabled.value ? savedFilterOverrides : globalFilterSettings.value,
    globalFilterSettings.value
  )
  savedAccountPaths.browserProfilePath = accountDraft.browserProfilePath
  savedAccountPaths.downloadPath = accountDraft.downloadPath
  if (settings?.inherited) notice.value = `账号「${accounts.value.find((a) => a.id === id)?.name ?? id}」尚未单独配置，将继承通用筛选设置。`
}

async function reloadCatalog(): Promise<void> {
  try {
    const result = await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'get'])
    const cached = normalizeCategories(result.categories)
    // 当前托管页面刚读取出的完整动态树优先；旧缓存的 ID 可能不含父级路径，不能覆盖实时子树。
    categories.value = liveCategoryRoots.value.length
      ? mergeCategoryTrees(liveCategoryRoots.value, cached)
      : mergeCategoryTrees(cached, [])
    normalizeCurrentCategorySelections()
  } catch {
    // 目录尚未被百应页面探针捕获时保留现有状态。
  }
}

async function loadCachedFilterCatalog(): Promise<void> {
  const result = await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'filters-get'])
  const cached = result.filterCatalog as Record<string, unknown> | undefined
  if (!cached || !cached.filters || !cached.filterGroups) return
  const groups = cached.filterGroups as Record<string, FilterField[]>
  if (!Object.values(groups).some((items) => Array.isArray(items) && items.length)) return
  filterCatalogData.value = cached
  filterGroups.value = groups
  featureOptions.value = Array.isArray(cached.features) ? cached.features.map(String) : []
  filterCatalogUpdatedAt.value = String(cached.updatedAt ?? '')
}

async function refreshFilterCatalog(): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const cachedSecondCount = categories.value.reduce((total, root) => total + root.children.length, 0)
    const cachedThirdCount = categories.value.reduce(
      (total, root) => total + root.children.reduce((count, child) => count + child.children.length, 0),
      0
    )
    const includeCategoryTree = categories.value.length === 0 || cachedSecondCount === 0 || cachedThirdCount === 0
    const result = await window.api.browser.inspectBaiyingSelection({
      profileKey: browserProfileKey.value,
      profilePath: accountDraft.browserProfilePath || undefined,
      includeCategoryTree
    })
    if (result.ok !== true) throw new Error(String(result.error ?? '读取百应筛选项失败'))
    const freshGroups = result.filterGroups as Record<string, FilterField[]> | undefined
    if (!freshGroups || !Object.values(freshGroups).some((items) => items.length)) {
      throw new Error('百应选品广场未显示筛选控件，请在托管 Chrome 打开选品广场后刷新。')
    }
    const cachedGroups = filterCatalogData.value.filterGroups as Record<string, FilterField[]> | undefined
    const groups = Object.fromEntries(Object.entries(freshGroups).map(([groupName, fields]) => [
      groupName,
      fields.map((field) => {
        if (field.options.length) return field
        const cached = cachedGroups?.[groupName]?.find((item) => item.label === field.label)
        return cached?.options.length ? { ...field, options: [...cached.options], multiple: cached.multiple } : field
      })
    ])) as Record<string, FilterField[]>
    filterGroups.value = groups
    const freshFeatures = Array.isArray(result.features) ? (result.features as string[]) : []
    const cachedFeatures = Array.isArray(filterCatalogData.value.features) ? filterCatalogData.value.features.map(String) : []
    featureOptions.value = freshFeatures.length ? freshFeatures : cachedFeatures
    const filterSummary = `百应筛选已同步：带货情况 ${groups['带货情况']?.length ?? 0} 项、商品信息 ${groups['商品信息']?.length ?? 0} 项、特色货品 ${featureOptions.value.length} 项。`
    const rootOptions = Array.isArray(result.categoryRoots)
      ? result.categoryRoots as Array<{ id: string; name: string; children?: Array<{ id: string; name: string; children?: Array<{ id: string; name: string }> }> }>
      : []
    const secondLevelCount = rootOptions.reduce((total, root) => total + (Array.isArray(root.children) ? root.children.length : 0), 0)
    const thirdLevelCount = rootOptions.reduce(
      (total, root) => total + (root.children ?? []).reduce((count, child) => count + (child.children ?? []).length, 0), 0
    )
    const hasFullRootSnapshot = rootOptions.length > 0 && secondLevelCount > 0 && thirdLevelCount > 0
    const categoryRootsForCache = includeCategoryTree && hasFullRootSnapshot ? rootOptions : categories.value
    const freshFilters = result.filters && typeof result.filters === 'object'
      ? result.filters as Record<string, unknown>
      : {}
    const cachedFilters = filterCatalogData.value.filters && typeof filterCatalogData.value.filters === 'object'
      ? filterCatalogData.value.filters as Record<string, unknown>
      : {}
    const mergedFilters: Record<string, unknown> = { ...cachedFilters, ...freshFilters }
    for (const [label, options] of Object.entries(freshFilters)) {
      if (Array.isArray(options) && options.length === 0 && Array.isArray(cachedFilters[label]) && cachedFilters[label].length) {
        mergedFilters[label] = cachedFilters[label]
      }
    }
    const savedCatalog = await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'filters-save'], {
      filters: mergedFilters,
      filterGroups: groups,
      features: featureOptions.value,
      categoryRoots: categoryRootsForCache
    })
    filterCatalogData.value = (savedCatalog.filterCatalog as Record<string, unknown> | undefined) ?? {
      filters: mergedFilters, filterGroups: groups, features: featureOptions.value, categoryRoots: categoryRootsForCache
    }
    filterCatalogUpdatedAt.value = String(filterCatalogData.value.updatedAt ?? '')
    if (!includeCategoryTree) {
      notice.value = `${filterSummary}类目沿用本机完整缓存：${categories.value.length} 个一级、${cachedSecondCount} 个二级、${cachedThirdCount} 个三级。`
    } else if (hasFullRootSnapshot) {
      liveCategoryRoots.value = rootOptions.filter((root) => root.id && root.name).map((root) => ({
        id: String(root.id),
        name: String(root.name),
        children: (Array.isArray(root.children) ? root.children : []).map((child) => ({
            id: String(child.id),
            name: String(child.name),
            children: (Array.isArray(child.children) ? child.children : []).map((third) => ({
              id: String(third.id), name: String(third.name), children: []
            }))
          }))
      }))
      categories.value = liveCategoryRoots.value
      const removedSelections = pruneCurrentCategorySelections()
      await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'save', '--mode', 'root-snapshot'], {
        mode: 'root-snapshot', categories: liveCategoryRoots.value
      })
      const pruned = await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'selections-prune'])
      const secondCount = liveCategoryRoots.value.reduce((total, root) => total + root.children.length, 0)
      const thirdCount = liveCategoryRoots.value.reduce((total, root) => total + root.children.reduce((count, child) => count + child.children.length, 0), 0)
      const dbRemoved = Number(pruned.removedSelections ?? 0)
      notice.value = `${filterSummary}另同步百应实时类目 ${rootOptions.length} 个一级、${secondCount} 个二级、${thirdCount} 个三级；清理失效勾选 ${Math.max(removedSelections, dbRemoved)} 项。`
    } else {
      notice.value = `${filterSummary}但百应未返回完整三级类目，本机目录未覆盖；请稍后重新同步。`
    }
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    busy.value = false
  }
}

function hasSelectedPlatformFilters(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  const filters = value as Record<string, unknown>
  return Object.entries(filters).some(([group, configured]) => {
    if (group === 'features') {
      if (Array.isArray(configured)) return configured.some((item) => String(item).trim())
      return Boolean(configured && typeof configured === 'object' && Object.values(configured as Record<string, unknown>).some(Boolean))
    }
    if (!configured || typeof configured !== 'object') return false
    return Object.values(configured as Record<string, unknown>).some((item) =>
      Array.isArray(item) ? item.some((entry) => String(entry).trim()) : Boolean(item)
    )
  })
}

const level1Options = computed(() => categories.value)
const level2Options = computed(() => categories.value.filter((item) => global.categorySelection.level1.includes(item.id)).flatMap((item) => item.children))
const level3Options = computed(() => level2Options.value.filter((item) => global.categorySelection.level2.includes(item.id)).flatMap((item) => item.children))
const accountLevel2Options = computed(() => categories.value.filter((item) => accountDraft.categorySelection.level1.includes(item.id)).flatMap((item) => item.children))
const accountLevel3Options = computed(() => accountLevel2Options.value.filter((item) => accountDraft.categorySelection.level2.includes(item.id)).flatMap((item) => item.children))
function selectedCategoryCount(selection: { level1: string[]; level2: string[]; level3: string[] }): number {
  return selection.level1.length + selection.level2.length + selection.level3.length
}

function hasSelectedCategory(selection: { level1: string[]; level2: string[]; level3: string[] }): boolean {
  return selectedCategoryCount(selection) > 0
}

function updateCategory(level: CategoryLevel, scope: 'global' | 'account'): void {
  const selected = scope === 'global' ? global.categorySelection : accountDraft.categorySelection
  const lv2 = scope === 'global' ? level2Options.value : accountLevel2Options.value
  const lv3 = scope === 'global' ? level3Options.value : accountLevel3Options.value
  if (level === 'level1') {
    const allowed = new Set(lv2.map((item) => item.id))
    selected.level2 = selected.level2.filter((id) => allowed.has(id))
  }
  if (level !== 'level3') {
    const allowed = new Set(lv3.map((item) => item.id))
    selected.level3 = selected.level3.filter((id) => allowed.has(id))
  }
}

function toggleCategory(id: string, level: CategoryLevel, scope: 'global' | 'account', checked: boolean): void {
  const selected = scope === 'global' ? global.categorySelection : accountDraft.categorySelection
  const current = selected[level]
  selected[level] = checked
    ? current.includes(id) ? current : [...current, id]
    : current.filter((item) => item !== id)
  updateCategory(level, scope)
}

function categoryAction(level: CategoryLevel, scope: 'global' | 'account', action: 'all' | 'invert' | 'clear'): void {
  const target = scope === 'global' ? global.categorySelection : accountDraft.categorySelection
  const options = scope === 'global'
    ? level === 'level1' ? level1Options.value : level === 'level2' ? level2Options.value : level3Options.value
    : level === 'level1' ? level1Options.value : level === 'level2' ? accountLevel2Options.value : accountLevel3Options.value
  const current = target[level]
  target[level] = action === 'clear' ? [] : action === 'all' ? options.map((item) => item.id) : options.map((item) => item.id).filter((id) => !current.includes(id))
  updateCategory(level, scope)
}

async function saveGlobal(): Promise<void> {
  error.value = ''
  try {
    if (global.selectionMode === 'category' && global.categoryMode === 'manual' && !hasSelectedCategory(global.categorySelection)) {
      throw new Error('已选择手动选类目，请至少勾选一个类目；或切回自动推荐。')
    }
    validateCommissionRateThreshold(global.thresholds.commissionRate, '通用设置')
    validateExperienceScoreThreshold(global.thresholds.experienceScore, '通用设置')
    for (const [label, value] of Object.entries({
      '每品视频上限': global.quota.perProductVideos,
      '通用每账号商品上限': global.quota.perAccountProducts,
      '通用每账号视频上限': global.quota.perAccountVideos
    })) {
      if (!Number.isInteger(value) || value < 1) throw new Error(`${label}必须是大于 0 的整数。`)
    }
    global.execution.publishMode = '自动'
    global.bannedWords = Array.from(new Set(bannedWordsText.value.split(/\r?\n/).map((word) => word.trim()).filter(Boolean)))
    await runEngine('selection_settings.py', ['--db', DB_PATH, 'save'], JSON.parse(JSON.stringify(global)))
    notice.value = '通用选品设置已保存；账号未单独配置的条件会继承此设置。'
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  }
}

function appendOptionalMallTitleBannedWords(): void {
  const currentWords = bannedWordsText.value.split(/\r?\n/).map((word) => word.trim()).filter(Boolean)
  const existingWords = new Set(currentWords.map((word) => word.toLocaleLowerCase()))
  const additions = OPTIONAL_MALL_TITLE_BANNED_WORDS.filter((word) => !existingWords.has(word.toLocaleLowerCase()))
  if (!additions.length) {
    notice.value = '这些商城标题候选词已在屏蔽词列表中。'
    return
  }
  bannedWordsText.value = [...currentWords, ...additions].join('\n')
  notice.value = `已将 ${additions.length} 个候选词加入编辑框；点击「保存通用设置」后生效。`
}

function validateCommissionRateThreshold(value: number | null | undefined, scope: string): void {
  if (value === null || value === undefined) return
  if (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100) {
    throw new Error(`${scope}佣金率阈值须为 0–100 之间的百分数。`)
  }
}

function validateExperienceScoreThreshold(value: number | null | undefined, scope: string): void {
  if (value === null || value === undefined) return
  if (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100) {
    throw new Error(`${scope}体验分阈值须为 0–100 之间的数值。`)
  }
}

async function chooseProfilePath(): Promise<void> {
  const path = await window.api.dir.chooseProfileDir()
  if (path) accountDraft.browserProfilePath = path
}

async function chooseDownloadPath(): Promise<void> {
  const path = await window.api.dir.chooseAccountDownloadDir()
  if (path) accountDraft.downloadPath = path
}

async function chooseGlobalDownloadPath(): Promise<void> {
  error.value = ''
  try {
    const path = await window.api.dir.chooseDownloadDir()
    if (!path) return
    defaultDownloadRoot.value = path
    notice.value = '通用下载根目录已保存。新选品批次将按账号建立独立子目录；账号自定义路径仍优先使用。'
  } catch (cause) {
    error.value = '保存通用下载目录失败：' + String(cause instanceof Error ? cause.message : cause)
  }
}

function ensureAccountPathsSaved(): boolean {
  if (!accountPathsHaveUnsavedChanges.value) return true
  error.value = '账号路径有未保存的修改。请先点击“保存账号设置”；新批次不会读取未保存的路径。'
  return false
}

async function openAccountBaiying(): Promise<void> {
  if (accountId.value === null || busy.value) return
  if (accountDraft.browserProfilePath !== savedAccountPaths.browserProfilePath) {
    error.value = '浏览器缓存路径已修改，请先保存路径，再打开百应授权入口。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await window.api.account.openJingxuan(accountId.value, accountDraft.browserProfilePath || undefined)
    if (!result.ok) throw new Error(result.error ?? '打开百应失败')
    notice.value = `已在账号「${accounts.value.find((item) => item.id === accountId.value)?.name ?? accountId.value}」的专属 profile 打开百应。请先手动登录，再刷新类目。`
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    busy.value = false
  }
}

async function refreshAccountCategories(): Promise<void> {
  if (accountId.value === null || busy.value) return
  if (accountDraft.browserProfilePath !== savedAccountPaths.browserProfilePath || accountDraft.downloadPath !== savedAccountPaths.downloadPath) {
    error.value = '路径已修改，请先保存路径，再刷新类目或打开授权入口。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const result = await window.api.browser.inspectBaiyingSelection({
      profileKey: browserProfileKey.value,
      profilePath: accountDraft.browserProfilePath || undefined,
      includeCategoryTree: true
    })
    if (result.ok !== true) throw new Error(String(result.error ?? '刷新百应类目失败'))
    const roots = normalizeCategories(result.categoryRoots)
    const secondCount = roots.reduce((total, root) => total + root.children.length, 0)
    const thirdCount = roots.reduce((total, root) => total + root.children.reduce((count, child) => count + child.children.length, 0), 0)
    if (!roots.length || !secondCount || !thirdCount) throw new Error('百应没有返回完整的三级类目；本地类目与账号设置均未改动。')
    liveCategoryRoots.value = roots
    categories.value = liveCategoryRoots.value
    const removedSelections = pruneCurrentCategorySelections()
    await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'save', '--mode', 'root-snapshot'], {
      mode: 'root-snapshot', categories: liveCategoryRoots.value
    })
    const currentFilterCatalog = filterCatalogData.value as Record<string, unknown>
    const savedFilterCatalog = await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'filters-save'], {
      filters: currentFilterCatalog.filters && typeof currentFilterCatalog.filters === 'object' ? currentFilterCatalog.filters : {},
      filterGroups: currentFilterCatalog.filterGroups && typeof currentFilterCatalog.filterGroups === 'object' ? currentFilterCatalog.filterGroups : {},
      features: Array.isArray(currentFilterCatalog.features) ? currentFilterCatalog.features : [],
      categoryRoots: roots
    })
    filterCatalogData.value = (savedFilterCatalog.filterCatalog as Record<string, unknown> | undefined) ?? {
      ...currentFilterCatalog, categoryRoots: roots
    }
    const pruned = await runEngine('selection_settings.py', ['--db', DB_PATH, 'catalog', 'selections-prune'])
    const dbRemoved = Number(pruned.removedSelections ?? 0)
    notice.value = `类目已按百应实时目录刷新：${roots.length} 个一级、${secondCount} 个二级、${thirdCount} 个三级；清理失效勾选 ${Math.max(removedSelections, dbRemoved)} 项。`
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    busy.value = false
  }
}

async function saveAccount(): Promise<void> {
  if (accountId.value === null || busy.value) return
  const targetAccountId = accountId.value
  if (!isSelectionAccountInScope(targetAccountId)) {
    error.value = '所选账号已不存在，账号设置未保存。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    if (accountDraft.browserProfilePath) {
      const directoryValidation = await window.api.dir.validateProfileDir(accountDraft.browserProfilePath)
      if (!directoryValidation.ok) {
        throw new Error(directoryValidation.error ?? '浏览器缓存目录校验失败')
      }
      const profileValidation = await window.api.browser.validateProfilePath({
        profileKey: 'account-' + targetAccountId,
        profilePath: directoryValidation.path ?? accountDraft.browserProfilePath
      })
      if (!profileValidation.ok || !profileValidation.path) {
        throw new Error(profileValidation.error ?? '浏览器缓存目录校验失败')
      }
      accountDraft.browserProfilePath = profileValidation.path
    }

    if (accountDraft.downloadPath.trim()) {
      const downloadValidation = await window.api.dir.canonicalizePath(accountDraft.downloadPath)
      if (!downloadValidation.ok || !downloadValidation.path) {
        throw new Error(downloadValidation.error ?? '账号下载路径无效')
      }
      accountDraft.downloadPath = downloadValidation.path
    }
    if (accountFilterOverridesEnabled.value && (!Number.isInteger(accountFilterDraft.value.candidateLimit) || accountFilterDraft.value.candidateLimit < 1 || accountFilterDraft.value.candidateLimit > 10)) {
      throw new Error('账号候选上限须为 1–10 的整数。')
    }
    if (accountFilterOverridesEnabled.value) {
      validateCommissionRateThreshold(accountFilterDraft.value.thresholds.commissionRate, '账号个性化')
      validateExperienceScoreThreshold(accountFilterDraft.value.thresholds.experienceScore, '账号个性化')
    }
    if (accountDraft.dailyQuotaOverride.enabled && (
      !Number.isInteger(accountDraft.dailyQuotaOverride.perAccountProducts) || accountDraft.dailyQuotaOverride.perAccountProducts < 1 ||
      !Number.isInteger(accountDraft.dailyQuotaOverride.perAccountVideos) || accountDraft.dailyQuotaOverride.perAccountVideos < 1
    )) {
      throw new Error('账号每日商品数和视频数上限必须填写大于 0 的整数。')
    }
    const effectiveSelectionMode = accountDraft.selectionMode === 'inherit' ? global.selectionMode : accountDraft.selectionMode
    if (effectiveSelectionMode === 'category' && accountDraft.categoryMode === 'manual' && !hasSelectedCategory(accountDraft.categorySelection)) {
      throw new Error('该账号已选择手动选类目，请至少勾选一个类目；或改为继承通用设置/自动推荐。')
    }
    const accountSettings = {
      selectionMode: accountDraft.selectionMode,
      categoryMode: accountDraft.categoryMode,
      categorySelection: JSON.parse(JSON.stringify(accountDraft.categorySelection)),
      browserProfilePath: accountDraft.browserProfilePath,
      downloadPath: accountDraft.downloadPath,
      dailyQuotaOverride: {
        enabled: accountDraft.dailyQuotaOverride.enabled,
        perAccountProducts: accountDraft.dailyQuotaOverride.enabled
          ? accountDraft.dailyQuotaOverride.perAccountProducts : global.quota.perAccountProducts,
        perAccountVideos: accountDraft.dailyQuotaOverride.enabled
          ? accountDraft.dailyQuotaOverride.perAccountVideos : global.quota.perAccountVideos
      },
      ...(accountFilterOverridesEnabled.value
        ? { filterOverrides: createSelectionFilterSettings(accountFilterDraft.value, globalFilterSettings.value) }
        : {})
    }
    const snapshot = JSON.parse(JSON.stringify(global)) as Record<string, unknown>
    const prepared = await prepareAccountRunContexts(selectionScopeAccounts.value, snapshot, targetAccountId, accountSettings)
    const resourceError = prepared.errors.get(targetAccountId)
    if (resourceError) throw new Error('账号资源隔离校验失败：' + resourceError)
    await runEngine('selection_settings.py', ['--db', DB_PATH, 'save', '--account-id', String(targetAccountId)], accountSettings)
    savedAccountPaths.browserProfilePath = accountDraft.browserProfilePath
    savedAccountPaths.downloadPath = accountDraft.downloadPath
    notice.value = accountDraft.dailyQuotaOverride.enabled
      ? '账号选品设置已保存；每日商品数和视频数按本账号限额执行。'
      : '账号选品设置已保存；每日限额继承通用默认值。'
    emit('saved', targetAccountId)
    if (props.dialogOnly) emit('close')
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    busy.value = false
  }
}

function setAccountFilterOverrides(enabled: boolean): void {
  if (enabled && !accountFilterOverridesEnabled.value) {
    accountFilterDraft.value = createSelectionFilterSettings(globalFilterSettings.value)
  }
  accountFilterOverridesEnabled.value = enabled
}

function resetAccountPaths(): void {
  accountDraft.browserProfilePath = ''
  accountDraft.downloadPath = ''
}

function closeDialog(): void {
  if (!busy.value) emit('close')
}

async function filterByLocalRegistry(
  accountKey: string,
  rows: SelectionCandidate[],
  targetCount: number,
  selectionMode: 'general' | 'category' = 'general'
): Promise<{
  candidates: SelectionCandidate[]
  excluded: Array<{ candidate: SelectionCandidate; reason: string }>
  recommendations: Array<{ categoryId: string; highScoreCount: number; averageScore: number }>
}> {
  if (!accountKey.trim()) throw new Error('账号本机标识无效，无法读取本机选品历史。')
  const snapshot = await window.api.browser.getSelectionRegistrySnapshot(accountKey, rows.map((candidate) => ({
    productId: candidate.productId,
    leafCategoryId: candidateLeafCategoryId(candidate)
  })))
  const categoryMode = selectionMode === 'category'
  const { eligible, excluded } = filterCandidatesByRegistry(rows, snapshot, !categoryMode, categoryMode)
  if (!categoryMode) {
    const sorted = [...eligible].sort((left, right) =>
      Number(right.unifiedScore ?? right.analysisScore ?? 0) - Number(left.unifiedScore ?? left.analysisScore ?? 0) ||
      left.productId.localeCompare(right.productId)
    ).map((candidate) => ({ ...candidate, selected: true }))
    return { candidates: sorted, excluded, recommendations: [] }
  }
  const ranked = rankByRecommendedCategories(eligible, snapshot.categoryUsage ?? {}, targetCount)
  return { candidates: ranked.candidates, excluded, recommendations: ranked.recommendations }
}

function categoryName(categoryId: string): string {
  const visit = (nodes: CategoryNode[]): string => {
    for (const node of nodes) {
      if (node.id === categoryId) return node.name
      const child = visit(node.children)
      if (child) return child
    }
    return ''
  }
  return visit(categories.value) || categoryId
}

function candidateSourceLabels(candidate: SelectionCandidate): string {
  const labels: Record<'hotSpot' | 'allianceRank' | 'selectionSquare', string> = {
    hotSpot: '热点趋势',
    allianceRank: '联盟榜单',
    selectionSquare: '选品广场'
  }
  return (candidate.sources ?? []).map((source) => labels[source]).filter(Boolean).join('、')
}

async function prepareBatchCandidatePlans(
  targets: AccountRow[],
  contexts: Map<number, AccountRunContext>,
  reviewedAccountId?: number | null,
  reviewedProducts?: ReadonlySet<string>,
  accountSwitchFailures?: Map<number, AccountSwitchFailure>
): Promise<Map<number, AccountCandidatePlan>> {
  const poolRows = new Map<number, SelectionCandidate[]>()
  const sourceIdsByAccount = new Map<number, Map<string, Set<string>>>()
  const collectedSourceIdsByAccount = new Map<number, Map<string, Set<string>>>()
  const registryExcludedByAccount = new Map<number, Array<{ candidate: SelectionCandidate; reason: string }>>()
  const rejectedByAccount = new Map<number, SelectionCandidate[]>()
  const sourcesByAccount = new Map<number, PrioritySourceReadResult[]>()
  const warningsByAccount = new Map<number, string[]>()
  const categoryEnrichmentByAccount = new Map<number, AccountCandidatePlan['categoryEnrichment']>()
  const recommendationsByAccount = new Map<number, AccountCandidatePlan['recommendedCategories']>()
  const capacities = new Map<number, number>()
  const names = new Map(targets.map((account) => [account.id, account.name]))

  const orderedTargets = [...targets].sort((left, right) => left.id - right.id)
  for (const [targetIndex, account] of orderedTargets.entries()) {
    const context = contexts.get(account.id)
    if (!context) throw new Error(`账号「${account.name}」缺少运行设置，未开始批次候选采集。`)
    const settings: Record<string, unknown> = {
      ...context.settings,
      _visibleSourcesOnly: true,
      _candidatePoolLimit: 200
    }
    reportSelectionProgress(account.id, '统一采集候选池', {
      accountIndex: targetIndex + 1,
      accountTotal: orderedTargets.length,
      completedAccounts: targetIndex,
      message: `只读采集三个百应来源并统一筛选（账号 ${account.id}）`
    })
    let preview: Record<string, unknown>
    let quota: Record<string, unknown>
    try {
      ;[preview, quota] = await Promise.all([
        window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
          profileKey: context.profileKey,
          profilePath: context.profilePath,
          settings,
          accountId: account.id,
          downloadPath: context.downloadPath,
          previewOnly: true
        })),
        runEngine('selection_settings.py', [
          '--db', DB_PATH, 'quota', 'get', '--account-id', String(account.id)
        ])
      ])
    } catch (cause) {
      const classification = toUserSelectionError(cause).classification
      if (accountSwitchFailures && (
        classification === 'ACCOUNT_SWITCH_CAPTCHA' || classification === 'ACCOUNT_SWITCH_LOGIN' ||
        classification === 'ACCOUNT_SWITCH_DISCONNECT' || classification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED' ||
        classification === 'SEARCHBOX_ZERO_EXHAUSTED' || classification === 'SEARCHBOX_CONTROL_EXHAUSTED'
      )) {
        accountSwitchFailures.set(account.id, createAccountSwitchFailure(account, cause, classification))
        continue
      }
      throw cause
    }
    if (preview.ok !== true) {
      const cause = new Error(`账号「${account.name}」只读候选采集失败：${String(preview.error ?? '未知错误')}`)
      const classification = toUserSelectionError(cause).classification
      if (accountSwitchFailures && (
        classification === 'ACCOUNT_SWITCH_CAPTCHA' || classification === 'ACCOUNT_SWITCH_LOGIN' ||
        classification === 'ACCOUNT_SWITCH_DISCONNECT' || classification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED' ||
        classification === 'SEARCHBOX_ZERO_EXHAUSTED' || classification === 'SEARCHBOX_CONTROL_EXHAUSTED'
      )) {
        accountSwitchFailures.set(account.id, createAccountSwitchFailure(account, cause, classification))
        continue
      }
      throw cause
    }
    let rows = Array.isArray(preview.candidates) ? preview.candidates as SelectionCandidate[] : []
    collectedSourceIdsByAccount.set(account.id, new Map(
      [...new Set(rows.flatMap((candidate) => candidate.sources ?? []))].map((sourceKey) => [
        sourceKey,
        new Set(rows.filter((candidate) => candidate.sources?.includes(sourceKey)).map((candidate) => candidate.productId))
      ])
    ))
    const rawTarget = Number(settings.candidateLimit ?? 5)
    const targetCount = Number.isFinite(rawTarget) ? Math.max(1, Math.min(10, rawTarget)) : 5
    const registry = await filterByLocalRegistry(
      `local:${account.id}`,
      rows,
      targetCount,
      settings.selectionMode === 'category' ? 'category' : 'general'
    )
    rows = registry.candidates
    if (account.id === reviewedAccountId && reviewedProducts) {
      rows = rows.filter((candidate) => reviewedProducts.has(candidate.productId))
    }
    const excluded = [...registry.excluded]
    const quotaSettings = (settings.quota ?? DEFAULTS.quota) as typeof DEFAULTS.quota
    const usage = (quota.account ?? {}) as { products?: number; videos?: number }
    const remainingCapacity = Math.max(0, Math.min(
      Number(quotaSettings.perAccountProducts) - Number(usage.products ?? 0),
      Number(quotaSettings.perAccountVideos) - Number(usage.videos ?? 0),
      targetCount
    ))
    reportSelectionProgress(account.id, '检查每日商品配额', {
      accountIndex: targetIndex + 1,
      accountTotal: orderedTargets.length,
      completedAccounts: targetIndex,
      percent: 100,
      message: `账号剩余可加车容量 ${remainingCapacity} 个；每商品每日视频配额已读取。`
    })
    const productUsage = (quota.productUsage ?? {}) as Record<string, number>
    const publishLocks = await loadProductPublishLocks(account.id)
    rows = rows.filter((candidate) => {
      const lock = publishLocks.get(candidate.productId)
      const dailyVideos = Number(productUsage[candidate.productId] ?? 0)
      if (lock) {
        excluded.push({ candidate, reason: '该商品已有排队或已发布任务' })
        return false
      }
      if (dailyVideos >= Number(quotaSettings.perProductVideos)) {
        excluded.push({ candidate, reason: '该商品今日已达到每品视频配额' })
        return false
      }
      return true
    })
    poolRows.set(account.id, rows)
    sourceIdsByAccount.set(account.id, new Map(
      [...new Set(rows.flatMap((candidate) => candidate.sources ?? []))].map((sourceKey) => [
        sourceKey,
        new Set(rows.filter((candidate) => candidate.sources?.includes(sourceKey)).map((candidate) => candidate.productId))
      ])
    ))
    registryExcludedByAccount.set(account.id, excluded)
    rejectedByAccount.set(account.id, Array.isArray(preview.rejectedCandidates)
      ? preview.rejectedCandidates as SelectionCandidate[] : [])
    sourcesByAccount.set(account.id, Array.isArray(preview.prioritySourceResults)
      ? preview.prioritySourceResults as PrioritySourceReadResult[] : [])
    warningsByAccount.set(account.id, Array.isArray(preview.warnings)
      ? preview.warnings.map(String) : [])
    categoryEnrichmentByAccount.set(account.id, preview.categoryEnrichment && typeof preview.categoryEnrichment === 'object'
      ? preview.categoryEnrichment as AccountCandidatePlan['categoryEnrichment'] : undefined)
    recommendationsByAccount.set(account.id, Array.isArray(preview.recommendedCategories)
      ? preview.recommendedCategories as AccountCandidatePlan['recommendedCategories'] : registry.recommendations)
    capacities.set(account.id, remainingCapacity)
    reportSelectionProgress(account.id, '统一采集候选池', {
      accountIndex: targetIndex + 1,
      accountTotal: orderedTargets.length,
      completedAccounts: targetIndex + 1,
      message: `账号 ${targetIndex + 1}/${orderedTargets.length}：来源候选 ${Array.isArray(preview.candidates) ? preview.candidates.length : 0} 个；本机/任务/配额排除 ${excluded.length} 个；可分配容量 ${remainingCapacity}`
    })
  }

  const scoredPools = scoreUnifiedCandidatePools(poolRows)
  const successfulTargets = targets.filter((account) => !accountSwitchFailures?.has(account.id))
  for (const account of successfulTargets) {
    const context = contexts.get(account.id)
    const settings = context?.settings ?? {}
    const requestedLimit = Number(settings.candidateLimit ?? 5)
    const targetCount = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(10, requestedLimit)) : 5
    const ranked = rankByRecommendedCategories(scoredPools.get(account.id) ?? [], {}, targetCount)
    recommendationsByAccount.set(account.id, ranked.recommendations)
  }
  const allocations = allocateExclusiveCandidates([...scoredPools.entries()].map(([accountId, candidates]) => {
    const targetCapacity = capacities.get(accountId) ?? 0
    // 预览后仍会在主进程原子登记一次；若候选期间被其他任务占用，已拒绝的 ID 会跳过，
    // 因此多分配一轮同账号备用候选。全局分配器仍保证备用商品不会分给其他账号。
    return {
      accountId,
      accountName: names.get(accountId) ?? `账号 ${accountId}`,
      capacity: Math.min(candidates.length, targetCapacity * 2),
      candidates
    }
  }))
  const plans = new Map<number, AccountCandidatePlan>()
  for (const account of successfulTargets) {
    const allocation = allocations.get(account.id)
    const winners = allocation?.candidates ?? []
    const mutualSkips = allocation?.skipped ?? []
    const recommendationOrder = new Map((recommendationsByAccount.get(account.id) ?? [])
      .map((row, index) => [row.categoryId, index]))
    winners.sort((left, right) =>
      (recommendationOrder.get(candidateLeafCategoryId(left)) ?? Number.MAX_SAFE_INTEGER) -
        (recommendationOrder.get(candidateLeafCategoryId(right)) ?? Number.MAX_SAFE_INTEGER) ||
      Number(right.unifiedScore ?? 0) - Number(left.unifiedScore ?? 0)
    )
    const sources = sourcesByAccount.get(account.id) ?? []
    const sourceIds = sourceIdsByAccount.get(account.id) ?? new Map<string, Set<string>>()
    const collectedSourceIds = collectedSourceIdsByAccount.get(account.id) ?? new Map<string, Set<string>>()
    const registryExcluded = registryExcludedByAccount.get(account.id) ?? []
    const productMutexIds = new Set(registryExcluded
      .filter((item) => /商品 ID.*(?:全账号池|已登记|预留|待核查)/.test(item.reason))
      .map((item) => item.candidate.productId))
    const classFilteredIds = new Set(registryExcluded
      .filter((item) => /末级类目|品类模式|类目/.test(item.reason))
      .map((item) => item.candidate.productId))
    for (const source of sources) {
      const sourceKey = source.sourceKey as CandidateSource
      const ids = sourceIds.get(sourceKey) ?? new Set<string>()
      const collectedIds = collectedSourceIds.get(sourceKey) ?? new Set<string>()
      const eligibleRows = (scoredPools.get(account.id) ?? []).filter((candidate) => ids.has(candidate.productId))
      source.passedCandidateCount = eligibleRows.length
      source.weightedHitCount = eligibleRows.filter((candidate) => (candidate.hotOrRankBoost ?? 0) > 0).length
      source.mutualExclusionSkippedCount = new Set([
        ...mutualSkips.filter((candidate) => collectedIds.has(candidate.productId)).map((candidate) => candidate.productId),
        ...[...productMutexIds].filter((productId) => collectedIds.has(productId))
      ]).size
      source.categoryFilteredCount = Math.max(0, Number(source.categoryFilteredCount) || 0) +
        [...classFilteredIds].filter((productId) => collectedIds.has(productId)).length
    }
    plans.set(account.id, {
      candidates: winners,
      registryExcluded: registryExcludedByAccount.get(account.id) ?? [],
      mutualExclusionSkipped: mutualSkips,
      rejectedCandidates: rejectedByAccount.get(account.id) ?? [],
      prioritySourceResults: sources,
      warnings: warningsByAccount.get(account.id) ?? [],
      recommendedCategories: recommendationsByAccount.get(account.id) ?? [],
      categoryEnrichment: categoryEnrichmentByAccount.get(account.id)
    })
    reportSelectionProgress(account.id, '候选统一评分、ID 去重与账号分配', {
      percent: 100,
      message: `候选计划已整理：保留 ${winners.length} 个可用候选，登记/互斥排除 ${registryExcluded.length + mutualSkips.length} 个。`
    })
  }
  return plans
}

async function prepareAccountRunContext(
  targetAccountId: number,
  globalSnapshot: Record<string, unknown>,
  accountSettingsOverride?: Record<string, unknown>
): Promise<AccountRunContext> {
  const saved = await runEngine('selection_settings.py', [
    '--db', DB_PATH, 'get', '--account-id', String(targetAccountId)
  ])
  const settingsRoot = (saved.settings ?? {}) as { account?: Record<string, unknown> }
  const accountSettings = accountSettingsOverride ?? settingsRoot.account ?? {}
  const categoryTree = JSON.parse(JSON.stringify(categories.value)) as CategoryNode[]
  const globalCategory = (globalSnapshot.categorySelection ?? {}) as Record<string, string[]>
  const storedCategory = (accountSettings.categorySelection ?? {}) as Record<string, string[]>
  const accountCategory = normalizeCategorySelection({
    level1: [...(storedCategory.level1 ?? [])],
    level2: [...(storedCategory.level2 ?? [])],
    level3: [...(storedCategory.level3 ?? [])]
  }, categoryTree)
  const globalSelectionMode = globalSnapshot.selectionMode === 'category' ? 'category' : 'general'
  const storedSelectionMode = accountSettings.selectionMode
  const accountSelectionMode = storedSelectionMode === 'general' || storedSelectionMode === 'category'
    ? storedSelectionMode : 'inherit'
  const effectiveSelectionMode = accountSelectionMode === 'inherit' ? globalSelectionMode : accountSelectionMode
  const globalCategoryMode = globalSelectionMode === 'category' && globalSnapshot.categoryMode === 'manual'
    ? 'manual' : 'auto'
  const storedCategoryMode = accountSettings.categoryMode
  const accountCategoryMode = storedCategoryMode === 'auto' || storedCategoryMode === 'manual'
    ? storedCategoryMode
    : 'inherit'
  const effectiveCategoryMode = accountCategoryMode === 'inherit' ? globalCategoryMode : accountCategoryMode
  const normalizedGlobalCategory = normalizeCategorySelection({
    level1: [...(globalCategory.level1 ?? [])],
    level2: [...(globalCategory.level2 ?? [])],
    level3: [...(globalCategory.level3 ?? [])]
  }, categoryTree)
  const effectiveCategorySelection = accountCategoryMode === 'manual'
    ? accountCategory
    : accountCategoryMode === 'inherit' && globalCategoryMode === 'manual'
      ? normalizedGlobalCategory
      : { level1: [], level2: [], level3: [] }

  const settings = JSON.parse(JSON.stringify(globalSnapshot)) as Record<string, unknown>
  settings.filterCatalog = {
    filters: filterCatalogData.value.filters ?? {},
    features: filterCatalogData.value.features ?? []
  }
  // 类目模式可继承通用设置、使用百应可见商品自动推荐，或只筛选用户手动勾选的类目。
  settings.selectionMode = effectiveSelectionMode
  settings.categoryMode = effectiveCategoryMode
  settings.categorySelection = effectiveCategorySelection
  if (effectiveSelectionMode === 'category' && effectiveCategoryMode === 'manual' && !hasSelectedCategory(effectiveCategorySelection)) {
    const accountName = accounts.value.find((account) => account.id === targetAccountId)?.name ?? `账号 ${targetAccountId}`
    throw new Error(`${accountName}处于手动选类目模式，但没有已选类目。请先选择类目或切回自动推荐。`)
  }
  const dailyQuotaOverride = accountSettings.dailyQuotaOverride
  if (dailyQuotaOverride && typeof dailyQuotaOverride === 'object' && !Array.isArray(dailyQuotaOverride)) {
    const override = dailyQuotaOverride as Record<string, unknown>
    if (override.enabled === true) {
      const quota = (settings.quota ?? {}) as Record<string, unknown>
      const perAccountProducts = Number(override.perAccountProducts ?? quota.perAccountProducts ?? 5)
      const perAccountVideos = Number(override.perAccountVideos ?? quota.perAccountVideos ?? 50)
      if (!Number.isInteger(perAccountProducts) || perAccountProducts < 1 || !Number.isInteger(perAccountVideos) || perAccountVideos < 1) {
        throw new Error('账号每日配额设置无效，请到账号个性化设置中检查。')
      }
      settings.quota = {
        ...quota,
        perAccountProducts,
        perAccountVideos
      }
    }
  }
  const filterOverrides = accountSettings.filterOverrides
  if (filterOverrides && typeof filterOverrides === 'object' && !Array.isArray(filterOverrides)) {
    Object.assign(settings, createSelectionFilterSettings(filterOverrides, createSelectionFilterSettings(globalSnapshot)))
  }
  validateCommissionRateThreshold(
    ((settings.thresholds ?? {}) as Record<string, number | null>).commissionRate,
    '账号运行设置'
  )
  validateExperienceScoreThreshold(
    ((settings.thresholds ?? {}) as Record<string, number | null>).experienceScore,
    '账号运行设置'
  )
  settings.categoryTree = categoryTree

  // 每次解析账号上下文都读取主进程持久化值，避免页面挂载后在其他入口变更目录仍沿用旧缓存。
  const latestDownloadRoot = await window.api.dir.getDownloadDir()
  defaultDownloadRoot.value = latestDownloadRoot
  const defaultRoot = latestDownloadRoot.replace(/[\\/]+$/, '')
  const configuredDownloadPath = String(accountSettings.downloadPath ?? '').trim()
  const configuredProfilePath = String(accountSettings.browserProfilePath ?? '').trim()
  const profileKey = 'account-' + targetAccountId
  const profileValidation = await window.api.browser.validateProfilePath({
    profileKey,
    profilePath: configuredProfilePath || undefined
  })
  if (!profileValidation.ok || !profileValidation.path) {
    throw new Error(profileValidation.error ?? '账号浏览器缓存路径校验失败')
  }
  const rawDownloadPath = configuredDownloadPath || defaultRoot + '\\账号-' + targetAccountId
  const downloadValidation = await window.api.dir.canonicalizePath(rawDownloadPath)
  if (!downloadValidation.ok || !downloadValidation.path) {
    throw new Error(downloadValidation.error ?? '账号下载路径解析失败')
  }
  return {
    profileKey,
    profilePath: configuredProfilePath ? profileValidation.path : undefined,
    resolvedProfilePath: profileValidation.path,
    downloadPath: downloadValidation.path,
    settings
  }
}

async function prepareAccountRunContexts(
  rows: AccountRow[],
  globalSnapshot: Record<string, unknown>,
  overrideAccountId?: number,
  accountSettingsOverride?: Record<string, unknown>
): Promise<{ contexts: Map<number, AccountRunContext>; errors: Map<number, string> }> {
  const contexts = new Map<number, AccountRunContext>()
  const errors = new Map<number, string>()
  for (const account of rows) {
    try {
      contexts.set(account.id, await prepareAccountRunContext(
        account.id,
        globalSnapshot,
        account.id === overrideAccountId ? accountSettingsOverride : undefined
      ))
    } catch (cause) {
      errors.set(account.id, String(cause instanceof Error ? cause.message : cause))
    }
  }
  const conflicts = findAccountResourcePathConflicts([...contexts].map(([id, context]) => ({
    accountId: id,
    accountName: rows.find((account) => account.id === id)?.name ?? String(id),
    browserProfilePath: context.resolvedProfilePath,
    downloadPath: context.downloadPath
  })))
  for (const [id, messages] of conflicts) {
    const previous = errors.get(id)
    errors.set(id, [previous, ...messages].filter(Boolean).join('；'))
  }
  return { contexts, errors }
}

async function executeAccountSelection(
  targetAccountId: number,
  previewOnly: boolean,
  globalSnapshot: Record<string, unknown>,
  reviewedProductIds?: ReadonlySet<string>,
  preparedContext?: AccountRunContext,
  recovery?: SelectionRecoveryHooks,
  reviewedPreviewAccountId?: number | null,
  candidatePlan?: AccountCandidatePlan
): Promise<{ status: '完成' | '部分完成'; message?: string; continueBatch?: boolean }> {
  let stage = '读取账号设置'
  let recoveryStage = recovery?.state.stage ?? 'quota_check'
  let categoryWarnings: string[] = []
  const persistStage = async (nextStage: string, extra: Record<string, unknown> = {}): Promise<void> => {
    recoveryStage = nextStage
    const progressLabels: Record<string, string> = {
      quota_check: '校验每日配额',
      candidate_fetch: '筛选并查询百应候选',
      cart_pending: '逐个核验并加入选品车',
      quota_cart: '记录选品车配额',
      video_pending: '生成商品详情图视频并加入发布队列'
    }
    const reusingCandidatePlan = Boolean(candidatePlan && ['quota_check', 'candidate_fetch'].includes(nextStage))
    const displayStage = reusingCandidatePlan
      ? '候选计划已就绪'
      : nextStage === 'quota_check' ? '账号配额预检' : progressLabels[nextStage] ?? nextStage
    reportSelectionProgress(
      targetAccountId,
      displayStage,
      reusingCandidatePlan ? {
        percent: 100,
        message: '已复用本批统一候选计划；配额复核完成，未重复采集候选。'
      } : nextStage === 'quota_check' ? {
        message: '检查账号剩余配额，配额充足后继续读取百应候选。'
      } : {}
    )
    if (recovery) await recovery.persist({ state: 'in_progress', stage: nextStage, error: '', ...extra })
  }
  try {
    const context = preparedContext ?? await prepareAccountRunContext(targetAccountId, globalSnapshot)
    if (!isSelectionAccountInScope(targetAccountId)) {
      throw new Error('所选账号已不存在，已停止选品操作。')
    }
    let fetched: SelectionCandidate[] = Array.isArray(recovery?.state.candidates)
      ? recovery.state.candidates : []
    let selectedIndexes: number[] = Array.isArray(recovery?.state.selectedIndexes)
      ? recovery.state.selectedIndexes : []
    let completed: Array<Record<string, unknown>> = Array.isArray(recovery?.state.completed)
      ? recovery.state.completed : []
    const savedStage = recovery?.state.stage ?? ''
    const recoveryMode = getSelectionRecoveryMode(
      savedStage,
      fetched.length > 0 && selectedIndexes.length > 0,
      completed
    )
    const accountLabel = accounts.value.find((account) => account.id === targetAccountId)?.name ?? `账号 ${targetAccountId}`
    reportSelectionProgress(targetAccountId, '准备账号选品任务', {
      percent: candidatePlan ? 100 : 0,
      message: candidatePlan ? '已复用统一候选、筛选和账号互斥计划；准备执行配额复核。' : '检查账号设置和本地类目计划'
    })
    let settings = recovery
      ? settingsForRecoveryCheckpoint(context.settings, recovery.state, accountLabel)
      : context.settings
    settings = { ...settings, _visibleSourcesOnly: true }
    notice.value = settings.selectionMode === 'category' && settings.categoryMode === 'manual'
      ? '选品仍只读取百应当前可见商品，并按手动选择的类目范围筛选。'
      : settings.selectionMode === 'category'
        ? '三个百应来源统一筛选；品类模式沿用自动推荐逻辑。'
        : '选品广场、热点趋势和联盟榜单按同一套数据门槛统一筛选；默认全部类目。'
    const cachedFilterOptions = filterCatalogData.value.filters
    if (hasSelectedPlatformFilters(settings.filters) &&
      (!cachedFilterOptions || typeof cachedFilterOptions !== 'object' || Object.keys(cachedFilterOptions as Record<string, unknown>).length === 0)) {
      throw new Error('当前设置包含百应页面筛选项，但本机还没有筛选目录。请点击一次“同步百应筛选项”后再执行。')
    }
    const profile = { profileKey: context.profileKey, profilePath: context.profilePath }

    if (previewOnly) {
      stage = '百应候选预览'
      accountStatuses[targetAccountId] = '获取候选中'
      reportSelectionProgress(targetAccountId, '获取百应候选预览', { message: '使用与批次相同的三源筛选、统一评分和本机互斥规则' })
      const previewPlanMap = await prepareBatchCandidatePlans(
        [{ id: targetAccountId, name: accountLabel, status: 'active' }],
        new Map([[targetAccountId, context]])
      )
      const previewPlan = previewPlanMap.get(targetAccountId)
      if (!previewPlan) throw new Error('百应候选预览没有生成统一筛选计划。')
      const found = previewPlan.candidates
      categoryRecommendations.value = previewPlan.recommendedCategories
      if (accountId.value === targetAccountId) {
        candidates.value = found
        previewAccountId.value = targetAccountId
        candidateSelectionCustomized.value = false
      }
      accountStatuses[targetAccountId] = '候选已就绪'
      const warnings = [...previewPlan.warnings]
      const locallySkipped = previewPlan.registryExcluded.length + previewPlan.mutualExclusionSkipped.length
      if (locallySkipped) {
        const skippedDetails = [
          ...previewPlan.registryExcluded.map((item) => `${item.candidate.productId}：${item.reason}`),
          ...previewPlan.mutualExclusionSkipped.map((item) => `${item.productId}：${item.mutualExclusionReason || '商品 ID 已被占用'}`)
        ].slice(0, 4).join('；')
        warnings.unshift(`本机登记、配额或商品 ID 互斥过滤 ${locallySkipped} 个候选${skippedDetails ? `；示例：${skippedDetails}` : ''}。`)
      }
      const rejected = previewPlan.rejectedCandidates
      const firstRejectedReason = rejected[0]?.thresholdReasons?.[0]
      const rejectionSummary = rejected.length
        ? `；另有 ${rejected.length} 个候选因列表实测数据未达阈值被排除${firstRejectedReason ? `，示例：${firstRejectedReason}` : ''}`
        : ''
      const previewSourceResults = previewPlan.prioritySourceResults
      const categorySummary = previewPlan.recommendedCategories.length
        ? `重点类目推荐依据：${previewPlan.recommendedCategories.slice(0, 8).map((row) =>
            `${categoryName(row.categoryId)}（高分商品 ${row.highScoreCount} 个，平均统一分 ${row.averageScore}）`
          ).join('；')}`
        : '无可推荐类目：当前通过筛选的候选没有可识别完整三级类目 ID。类目未知商品仍按通用数据模式保留。'
      const emptyCandidateMessage = settings._visibleSourcesOnly === true
        ? visibleSourceNoCandidateMessage(
          previewSourceResults,
          '已读取百应当前可见商品，但当前没有可执行候选；请查看筛选条件、已选商品/类目和跳过原因。'
        )
        : '已完成全部启用来源与选品广场兜底查询，仍没有符合条件的商品。'
      const sourceSummary = previewSourceResults.map((source) => {
        const label = String(source.label || source.sourceKey || '百应来源')
        if (source.status === 'read') {
          const nodeCount = Math.max(0, Number(source.productNodeCount ?? source.candidateNodeCount) || 0)
          const productIds = Array.isArray(source.firstProductIds)
            ? source.firstProductIds.slice(0, 3).map((id) => String(id)).join('、') : ''
          return `${label}${source.sourceOutcome || '① 滚动区出现商品'}：采集 ${Math.max(0, Number(source.productCount) || 0)} 个商品 ID，过筛 ${Math.max(0, Number(source.passedCandidateCount) || 0)} 个，加权命中 ${Math.max(0, Number(source.weightedHitCount) || 0)} 个，品类过滤 ${Math.max(0, Number(source.categoryFilteredCount) || 0)} 个，互斥跳过 ${Math.max(0, Number(source.mutualExclusionSkippedCount) || 0)} 个；滚动区节点 ${nodeCount} 个；前 3 个 ID：${productIds || '无'}`
        }
        if (source.status === 'empty') {
          return `${label}${source.sourceOutcome || source.pageListDiagnosis || '页面列表为空'}`
        }
        const detail = String(source.detail || '未返回 DOM 诊断详情').replace(/\s+/g, ' ').slice(0, 420)
        return `${label}未解析出商品 ID；${detail}`
      }).join('；')
      const previewMessage = found.length
        ? '已获取 ' + found.length + ' 个候选' + rejectionSummary + (warnings.length ? '；提示：' + warnings[0] : '')
        : emptyCandidateMessage + rejectionSummary + (warnings.length ? '；' + warnings[0] : '')
      const completePreviewMessage = categorySummary && !previewMessage.includes(categorySummary)
        ? `${previewMessage}；${categorySummary}`
        : previewMessage
      return {
        status: '完成',
        message: sourceSummary ? `${completePreviewMessage}；来源检查：${sourceSummary}` : completePreviewMessage
      }
    }

    const { resumeAfterCart, resumeCart } = recoveryMode

    if (!resumeAfterCart) {
      let eligible: SelectionCandidate[]
      let selectedIndexesToExecute = selectedIndexes
      let maxSelectedProducts: number | undefined
      const requestedLimit = Number(settings.candidateLimit ?? 5)
      const candidateLimit = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(10, requestedLimit)) : 5
      const hasExplicitPreviewSelection =
        reviewedPreviewAccountId === targetAccountId && reviewedProductIds !== undefined
      if (resumeCart) {
        const allSelectedCandidates = fetched.filter((candidate) => selectedIndexes.includes(candidate.cardIndex))
        if (allSelectedCandidates.length !== new Set(selectedIndexes).size || allSelectedCandidates.length !== selectedIndexes.length) {
          throw new Error('恢复任务中的候选快照不完整，已保留任务，请重新核对后再恢复。')
        }
        const resolvedProductIds = new Set(completed
          .filter((item) => item.cartAdded === true || item.skipped === true)
          .map((item) => String(item.productId ?? '')))
        const remainingCandidates = fetched.filter((candidate) =>
          selectedIndexes.includes(candidate.cardIndex) && !resolvedProductIds.has(candidate.productId)
        )
        const productLocks = await loadProductPublishLocks(targetAccountId)
        const duplicateSkips = duplicateProductSkipItems(remainingCandidates, productLocks)
        await logDuplicateProductSkips(targetAccountId, duplicateSkips, recovery)
        completed = [...completed.filter((item) =>
          !duplicateSkips.some((skipped) => String(item.productId ?? '') === String(skipped.productId ?? ''))
        ), ...duplicateSkips]
        const quota = await runEngine('selection_settings.py', [
          '--db', DB_PATH, 'quota', 'get', '--account-id', String(targetAccountId)
        ])
        const accountUsage = (quota.account ?? {}) as { products?: number; videos?: number }
        const quotaSettings = (settings.quota ?? cloneDefault().quota) as typeof DEFAULTS.quota
        const remainingAccountQuota = Math.max(0, Math.min(
          quotaSettings.perAccountProducts - (accountUsage.products ?? 0),
          quotaSettings.perAccountVideos - (accountUsage.videos ?? 0)
        ))
        const alreadyAddedCount = completed.filter((item) => item.cartAdded === true).length
        const remainingBatchTarget = Math.max(0, candidateLimit - alreadyAddedCount)
        if (remainingBatchTarget > 0 && remainingAccountQuota <= 0 && remainingCandidates.length) {
          throw new Error('该账号当前剩余商品或视频配额为 0；恢复检查点已保留，配额恢复后再继续。')
        }
        maxSelectedProducts = Math.min(
          remainingBatchTarget,
          remainingAccountQuota
        )
        const productUsage = (quota.productUsage ?? {}) as Record<string, number>
        eligible = remainingBatchTarget > 0 ? remainingCandidates.filter((candidate) =>
          !productLocks.has(candidate.productId) &&
          (productUsage[candidate.productId] ?? 0) < quotaSettings.perProductVideos
        ) : []
        selectedIndexesToExecute = eligible.map((candidate) => candidate.cardIndex)
        if (!eligible.length && !completed.length) {
          throw new Error('恢复任务中的候选快照不完整，已保留任务，请重新预览后再启动新批次。')
        }
      } else {
        await persistStage('quota_check')
        stage = '校验每日配额'
        accountStatuses[targetAccountId] = '校验每日配额'
        const quota = await runEngine('selection_settings.py', [
          '--db', DB_PATH, 'quota', 'get', '--account-id', String(targetAccountId)
        ])
        const accountUsage = (quota.account ?? {}) as { products?: number; videos?: number }
        const quotaSettings = (settings.quota ?? cloneDefault().quota) as typeof DEFAULTS.quota
        const maxProducts = Math.min(
          quotaSettings.perAccountProducts - (accountUsage.products ?? 0),
          quotaSettings.perAccountVideos - (accountUsage.videos ?? 0)
        )
        if (maxProducts <= 0) throw new Error('该账号今日商品或视频配额已用完；新任务需等待后续可用日期。')

        await persistStage('candidate_fetch')
        stage = '百应筛选并获取候选'
        accountStatuses[targetAccountId] = '获取候选中'
        const candidateQuerySettings = hasExplicitPreviewSelection
          ? settings
          : { ...settings, _candidatePoolLimit: 200 }
        const preview = candidatePlan
          ? {
              ok: true,
              candidates: candidatePlan.candidates,
              rejectedCandidates: candidatePlan.rejectedCandidates,
              warnings: candidatePlan.warnings,
              prioritySourceResults: candidatePlan.prioritySourceResults,
              recommendedCategories: candidatePlan.recommendedCategories
            }
          : await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
              ...profile, settings: candidateQuerySettings, accountId: targetAccountId,
              downloadPath: context.downloadPath, previewOnly: true
            }))
        if (preview.ok !== true) throw new Error(String(preview.error ?? '百应候选获取失败'))
        fetched = Array.isArray(preview.candidates) ? preview.candidates as SelectionCandidate[] : []
        const stableAccountKey = `local:${targetAccountId}`
        const targetCount = Number(settings.candidateLimit ?? 5)
        const registryFiltered = candidatePlan
          ? { candidates: fetched, excluded: [] as Array<{ candidate: SelectionCandidate; reason: string }>, recommendations: candidatePlan.recommendedCategories }
          : await filterByLocalRegistry(
              stableAccountKey,
              scoreUnifiedCandidatePools(new Map([[targetAccountId, fetched]])).get(targetAccountId) ?? fetched,
              Number.isFinite(targetCount) ? Math.max(1, targetCount) : 5,
              settings.selectionMode === 'category' ? 'category' : 'general'
            )
        fetched = registryFiltered.candidates
        categoryRecommendations.value = registryFiltered.recommendations
        const allRegistryExcluded = [
          ...registryFiltered.excluded,
          ...(candidatePlan?.registryExcluded ?? [])
        ]
        for (const { candidate, reason } of allRegistryExcluded) {
          completed.push({
            ...candidate,
            cartAdded: false,
            skipped: true,
            registryExcluded: true,
            status: reason
          })
          await recovery?.logEvent({
            stage: '本机选品登记', code: 'INFO_REGISTRY_CANDIDATE_EXCLUDED',
            message: `${String(candidate.title ?? candidate.productId)}：${reason}`,
            detail: reason, productId: String(candidate.productId ?? ''), productTitle: String(candidate.title ?? '')
          })
        }
        for (const candidate of candidatePlan?.mutualExclusionSkipped ?? []) {
          const reason = String(candidate.mutualExclusionReason || '商品已由统一分更高的账号占用')
          completed.push({ ...candidate, cartAdded: false, skipped: true, mutualExclusionSkipped: true, status: reason })
          await recovery?.logEvent({
            stage: '跨账号商品互斥', code: 'INFO_CROSS_ACCOUNT_PRODUCT_SKIPPED',
            message: `${String(candidate.title ?? candidate.productId)}：${reason}`,
            detail: reason, productId: String(candidate.productId ?? ''), productTitle: String(candidate.title ?? '')
          })
        }
        if (fetched.length && fetched[0]) {
          const topCategory = candidateLeafCategoryId(fetched[0])
          notice.value = topCategory
            ? `本机去重后保留 ${fetched.length} 个候选；系统已按高分商品数量、平均分和跨账号类目错开偏好排序，重点末级类目 ID：${topCategory}。`
            : `本机去重后保留 ${fetched.length} 个候选；类目由系统自动推荐。`
        }
        const prioritySourceResults = Array.isArray(preview.prioritySourceResults)
          ? preview.prioritySourceResults as PrioritySourceReadResult[] : []
        const prioritySourceMatchStatus = String(preview.prioritySourceMatchStatus || 'complete')
        const listRejected = Array.isArray(preview.rejectedCandidates)
          ? preview.rejectedCandidates as SelectionCandidate[] : []
        categoryWarnings = Array.isArray(preview.warnings)
          ? preview.warnings.map(String).filter((warning) =>
            warning.startsWith('跳过失败类目：') ||
            (!prioritySourceResults.length && warning.startsWith('跳过来源读取：')))
          : []
        for (const warning of categoryWarnings) {
          await recovery?.logFailure({
            stage: '百应类目查询',
            code: 'CATEGORY_QUERY_SKIPPED',
            message: conciseFailureMessage(warning), detail: warning
          })
        }
        for (const sourceResult of prioritySourceResults) {
          const productCount = Math.max(0, Number(sourceResult.productCount) || 0)
          const matchedCount = Math.max(0, Number(sourceResult.matchedCandidateCount) || 0)
          const passedCount = Math.max(0, Number(sourceResult.passedCandidateCount) || 0)
          const weightedCount = Math.max(0, Number(sourceResult.weightedHitCount) || 0)
          const categoryFilteredCount = Math.max(0, Number(sourceResult.categoryFilteredCount) || 0)
          const mutexSkippedCount = Math.max(0, Number(sourceResult.mutualExclusionSkippedCount) || 0)
          const hasMatchCount = sourceResult.matchedCandidateCount !== undefined &&
            sourceResult.matchedCandidateCount !== null && Number.isFinite(Number(sourceResult.matchedCandidateCount))
          const label = String(sourceResult.label || sourceResult.sourceKey || '优先来源')
          let code = 'PRIORITY_SOURCE_READ_SKIPPED'
          let message = settings._visibleSourcesOnly === true
            ? `${label}读取未成功，已跳过该来源；不会回退选品广场。`
            : `${label}读取未成功，已跳过该来源并继续选品广场兜底。`
          if (sourceResult.status === 'read' && prioritySourceMatchStatus === 'unavailable') {
            code = 'INFO_PRIORITY_SOURCE_READ'
            message = `${label}已读取到 ${productCount} 个商品 ID；类目查询未完成，是否匹配无法确认。`
          } else if (sourceResult.status === 'read' && prioritySourceMatchStatus === 'partial') {
            code = 'INFO_PRIORITY_SOURCE_READ'
            message = `${label}读取到 ${productCount} 个商品 ID；部分类目查询返回候选匹配 ${matchedCount} 个，完整匹配数无法确认。`
          } else if (sourceResult.status === 'read' && !hasMatchCount) {
            code = 'INFO_PRIORITY_SOURCE_READ'
            message = `${label}已读取到 ${productCount} 个商品 ID；本次类目查询未完成，匹配情况无法确认。`
          } else if (sourceResult.status === 'read' && matchedCount > 0) {
            code = 'SUCCESS_PRIORITY_SOURCE_READ'
            message = settings._visibleSourcesOnly === true
              ? `${label}识别到 ${Number(sourceResult.candidateNodeCount) || 0} 个候选节点 / ${productCount} 个商品 ID，解析出 ${matchedCount} 个候选。`
              : `${label}读取到 ${productCount} 个商品 ID，本次候选匹配 ${matchedCount} 个，已参与优先排序。`
          } else if (sourceResult.status === 'read') {
            code = 'INFO_PRIORITY_SOURCE_NO_MATCH'
            message = settings._visibleSourcesOnly === true
              ? `${label}读取到 ${productCount} 个商品 ID，但没有可用候选；已跳过该来源，不回退选品广场。`
              : `${label}读取到 ${productCount} 个商品 ID，但当前类目候选没有匹配；已跳过来源匹配条件，继续按选品广场筛选。`
          } else if (sourceResult.status === 'empty') {
            code = 'INFO_PRIORITY_SOURCE_EMPTY'
            message = settings._visibleSourcesOnly === true
              ? `${label}${sourceResult.pageListDiagnosis || '页面列表为空'}；已跳过该来源，继续检查其他启用来源。`
              : `${label}当前没有商品，已跳过该来源条件并继续查询选品广场。`
          }
          await recovery?.logEvent({
            stage: '优先来源读取', code, message,
            detail: [
              `采集 ${productCount} 个`,
              `统一筛选后分配 ${passedCount} 个`,
              `热点/榜单加权命中 ${weightedCount} 个`,
              `品类过滤 ${categoryFilteredCount} 个`,
              `跨账号互斥跳过 ${mutexSkippedCount} 个`,
              String(sourceResult.detail ?? '')
            ].filter(Boolean).join('；')
          })
        }
        for (const candidate of listRejected) {
          const productId = String(candidate.productId ?? '')
          if (!productId || completed.some((item) => String(item.productId ?? '') === productId)) continue
          const thresholdReasons = Array.isArray(candidate.thresholdReasons)
            ? candidate.thresholdReasons.map(String) : []
          const reason = thresholdReasons.join('；') || '候选列表实测数据未达到已设置阈值'
          completed.push({
            ...candidate,
            analysisRejected: true,
            analysisStatus: '列表实测数据未通过筛选',
            thresholdReasons,
            cartAdded: false,
            skipped: true,
            status: '选品广场实测数据未通过筛选：' + reason
          })
          await recovery?.logEvent({
            stage: '商品数据分析', code: 'WARNING_PRODUCT_ANALYSIS_FILTERED',
            message: conciseFailureMessage('商品数据未达到当前筛选条件，已跳过：' + reason),
            detail: reason, productId, productTitle: String(candidate.title ?? '')
          })
        }
        const usage = (quota.productUsage ?? {}) as Record<string, number>
        const chosenProducts = selectCandidateIdsForAccount(
          targetAccountId,
          reviewedPreviewAccountId === undefined ? previewAccountId.value : reviewedPreviewAccountId,
          reviewedProductIds,
          fetched
        )
        const productLocks = await loadProductPublishLocks(targetAccountId)
        const lockedCandidates = fetched.filter((candidate) =>
          chosenProducts.has(candidate.productId) && productLocks.has(candidate.productId)
        )
        const duplicateSkips = duplicateProductSkipItems(lockedCandidates, productLocks)
        await logDuplicateProductSkips(targetAccountId, duplicateSkips, recovery)
        completed = [...completed.filter((item) =>
          !duplicateSkips.some((skipped) => String(item.productId ?? '') === skipped.productId)
        ), ...duplicateSkips]
        maxSelectedProducts = Math.min(candidateLimit, maxProducts)
        eligible = fetched.filter((candidate) =>
          chosenProducts.has(candidate.productId) && !productLocks.has(candidate.productId) &&
          (usage[candidate.productId] ?? 0) < quotaSettings.perProductVideos
        )
        if (!eligible.length) {
          if (duplicateSkips.length) {
            const skippedSummary = `本账号已有 ${duplicateSkips.length} 个已排队或已发布商品，已阻止重复选品：${duplicateSkips.map((item) => item.title).join('、')}`
            accountStatuses[targetAccountId] = '已跳过账号内重复商品'
            await recovery?.persist({
              state: 'completed', stage: 'completed', error: '', candidates: fetched,
              selectedIndexes: [], completed
            })
            return { status: '部分完成', message: skippedSummary, continueBatch: true }
          }
          const analysisSkipped = completed.filter((item) => item.analysisRejected === true)
          if (analysisSkipped.length) {
            const skippedSummary = `候选列表数据分析排除 ${analysisSkipped.length} 个商品，未执行加车；原因已记录。`
            accountStatuses[targetAccountId] = '部分完成（筛选未通过）'
            await recovery?.persist({
              state: 'completed', stage: 'completed', error: '', candidates: fetched,
              selectedIndexes: [], completed
            })
            return { status: '部分完成', message: skippedSummary, continueBatch: true }
          }
          const globallySkipped = completed.filter((item) =>
            item.mutualExclusionSkipped === true || item.registryExcluded === true
          )
          if (globallySkipped.length) {
            const skippedSummary = `本机商品 ID 互斥/登记过滤 ${globallySkipped.length} 个候选，未执行加车；具体账号和原因已记录。`
            accountStatuses[targetAccountId] = '部分完成（商品互斥跳过）'
            await recovery?.persist({
              state: 'completed', stage: 'completed', error: '', candidates: fetched,
              selectedIndexes: [], completed
            })
            return { status: '部分完成', message: skippedSummary, continueBatch: true }
          }
          if (categoryWarnings.length) {
            const skippedSummary = `本账号没有可执行候选；已跳过 ${categoryWarnings.length} 个可恢复的类目或来源读取问题，原因已记录。`
            accountStatuses[targetAccountId] = '部分完成（类目/来源已跳过）'
            await recovery?.persist({
              state: 'completed', stage: 'completed', error: '', candidates: fetched,
              selectedIndexes: [], completed
            })
            return { status: '部分完成', message: skippedSummary, continueBatch: true }
          }
          throw new Error(settings._visibleSourcesOnly === true
            ? visibleSourceNoCandidateMessage(
              prioritySourceResults,
              '已读取百应当前可见商品，但当前没有可执行候选；请查看筛选条件、已选商品/类目和跳过原因。'
            )
            : '已查询全部已启用来源与选品广场兜底，仍没有符合条件的商品。')
        }
        selectedIndexes = eligible.map((candidate) => candidate.cardIndex)
        selectedIndexesToExecute = selectedIndexes.slice(0, Math.min(
          selectedIndexes.length,
          Math.max(candidateLimit, candidateLimit * 2)
        ))
        reportSelectionProgress(targetAccountId, '准备逐个处理选中商品', {
          itemIndex: 0,
          itemTotal: selectedIndexesToExecute.length,
          message: `候选池 ${selectedIndexesToExecute.length} 个；本轮最多加入 ${maxSelectedProducts} 个`
        })
        await persistStage('cart_pending', {
          candidates: fetched,
          selectedIndexes,
          completed: completed.length ? completed : null
        })
      }

      await persistStage('cart_pending', { candidates: fetched, selectedIndexes, completed: completed.length ? completed : null })
      if (selectedIndexesToExecute.length) {
        await recovery?.logEvent({
          stage: '候选池准备', code: 'INFO_CANDIDATE_POOL_READY',
          message: `已找到 ${selectedIndexesToExecute.length} 个可核验候选（含 ${Math.max(0, selectedIndexesToExecute.length - (maxSelectedProducts ?? 0))} 个备用）；登记冲突按商品 ID 跳过后继续尝试后续候选，最多加入 ${maxSelectedProducts} 个。`,
          detail: hasExplicitPreviewSelection
            ? '本次只使用人工勾选的候选。'
            : '候选池按全账号商品 ID 互斥分配；正式预留发现商品已登记时跳过该 ID，并尝试后续备用候选。自动补位候选池最多读取 30 个商品。'
        })
        stage = '加入选品车并取回商品 ID'
        accountStatuses[targetAccountId] = '加入选品车中'
        reportSelectionProgress(targetAccountId, '加入选品车并取回商品 ID', {
          itemIndex: 0,
          itemTotal: selectedIndexesToExecute.length,
          message: `逐个处理 ${selectedIndexesToExecute.length} 个已选商品`
        })
        const attemptedButUnconfirmedIds = new Set(completed
          .filter((item) => item.cartAttempted === true && item.cartAdded !== true && item.skipped !== true)
          .map((item) => String(item.productId ?? '')))
        const executionCandidates = fetched.map((candidate) => attemptedButUnconfirmedIds.has(candidate.productId)
          ? { ...candidate, cartAttempted: true }
          : candidate)
        const result = await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
          ...profile, settings, accountId: targetAccountId,
          accountKey: `local:${targetAccountId}`,
          downloadPath: context.downloadPath,
          selectedIndexes: selectedIndexesToExecute,
          candidateSnapshot: executionCandidates,
          refreshCandidatesById: resumeCart,
          analyzeBeforeCart: !resumeAfterCart,
          maxSelectedProducts,
          previewOnly: false
        }))
        if (result.ok !== true) throw new Error(String(result.error ?? '百应选品执行失败'))

        const registryRejected = Array.isArray(result.registryRejected)
          ? result.registryRejected as Array<{ productId: string; reason: string }> : []
        for (const rejected of registryRejected) {
          await recovery?.logEvent({
            stage: '本机选品登记', code: 'WARNING_REGISTRY_CONFLICT',
            message: `商品 ${rejected.productId} 未获本机预留，已跳过：${rejected.reason}`,
            detail: rejected.reason, productId: rejected.productId
          })
        }
        const settlementWarnings = Array.isArray(result.registrySettlementWarnings)
          ? result.registrySettlementWarnings.map(String) : []
        for (const warning of settlementWarnings) {
          await recovery?.logFailure({
            stage: '本机选品登记', code: 'REGISTRY_SETTLEMENT_PENDING',
            message: '百应结果已返回，但本机登记结算尚未确认；相关商品保持锁定待核查。',
            detail: warning
          })
        }
        const registryReleaseEvents = Array.isArray(result.registryReleaseEvents)
          ? result.registryReleaseEvents as Array<{
            productId: string
            outcome: string
            state: string
            reason: string
            diagnosticPath: string
            settled: boolean
          }> : []
        for (const release of registryReleaseEvents) {
          await recovery?.logEvent({
            stage: '本机选品登记',
            code: 'REGISTRY_RELEASE_CONTROL_FAILURE',
            message: `商品 ${release.productId} 预留已${release.settled ? '自动释放' : '尝试释放'}（${release.outcome}/${release.state}）；释放原因=${release.reason}。`,
            detail: release.diagnosticPath || '控件失败诊断路径未返回。',
            productId: release.productId
          })
        }

        const returned = Array.isArray(result.completed) ? result.completed as Array<Record<string, unknown>> : []
        if (!returned.length) throw new Error('百应没有返回已执行的商品结果。')
        const reportedAnalysisCount = Number(result.analysisAttemptedCount)
        const analysisAttemptedCount = Number.isFinite(reportedAnalysisCount)
          ? Math.max(0, reportedAnalysisCount)
          : returned.length
        const addedCount = returned.filter((item) => item.cartAdded === true).length
        reportSelectionProgress(targetAccountId, '已收到加车处理结果', {
          itemIndex: analysisAttemptedCount,
          itemTotal: analysisAttemptedCount,
          percent: 100,
          message: `详情核验 ${analysisAttemptedCount} 个候选；成功加入选品车 ${addedCount} 个`
        })
        for (const item of returned) {
          const status = String(item.status ?? '')
          const analysisRejected = item.analysisRejected === true
          const productId = String(item.productId ?? '')
          const productTitle = String(item.title ?? '')
          if (analysisRejected) {
            const reasons = Array.isArray(item.thresholdReasons) ? item.thresholdReasons.map(String) : []
            await recovery?.logEvent({
              stage: '商品数据分析', code: 'WARNING_PRODUCT_ANALYSIS_FILTERED',
              message: conciseFailureMessage(status || '实测数据未达到当前筛选条件，已跳过。'),
              detail: reasons.join('\n') || String(item.analysisError ?? ''), productId, productTitle
            })
          } else if (item.skipped === true) {
            await recovery?.logEvent({
              stage: '商品处理', code: 'WARNING_PRODUCT_SKIPPED',
              message: conciseFailureMessage(status || '本商品已跳过，批次继续处理其他商品。'),
              detail: typeof item.error === 'string' ? item.error : '', productId, productTitle
            })
          } else if (item.cartAdded === true) {
            await recovery?.logEvent({
              stage: '加入选品车', code: 'SUCCESS_PRODUCT_CART_ADDED',
              message: '已确认加入当前账号选品车。',
              detail: status, productId, productTitle
            })
          }
          const actionableFailure = analysisRejected || item.cartAdded !== true || Boolean(item.error) ||
            Boolean(item.manualAction) || /失败|无法打开|未能|超时|未确认|不匹配|缺少|没有返回/.test(status)
          if (!actionableFailure || analysisRejected || item.skipped === true) continue
          const cartFailed = item.cartAdded !== true
          const detailStage = !cartFailed && /详情|图片|素材/.test(status)
          const diagnosticDetail = typeof item.error === 'string'
              ? item.error
            : Array.isArray(item.imageErrors) ? item.imageErrors.map(String).join('\n') : status
          await recovery?.logFailure({
            stage: cartFailed ? '加入选品车' : detailStage ? '商品详情/素材' : '百应商品处理',
            code: cartFailed ? 'CART_NOT_CONFIRMED' : detailStage ? 'DETAIL_OR_ASSET_FAILED' : 'PRODUCT_STEP_FAILED',
            message: conciseFailureMessage(status || (item.manualAction ? '需要人工处理' : item.error)),
            detail: diagnosticDetail,
            productId,
            productTitle
          })
        }
        const returnedIds = new Set(returned.map((item) => String(item.productId ?? '')))
        const priorById = new Map(completed.map((item) => [String(item.productId ?? ''), item]))
        const mergedReturned = returned.map((item) => ({ ...(priorById.get(String(item.productId ?? '')) ?? {}), ...item }))
        completed = [...completed.filter((item) => !returnedIds.has(String(item.productId ?? ''))), ...mergedReturned]
        const returnedByProductId = new Map<string, Record<string, unknown>>(returned
          .filter((item) => String(item.productId ?? ''))
          .map((item) => [String(item.productId), item] as const))
        fetched = fetched.map((candidate) => {
          const analysis = returnedByProductId.get(candidate.productId)
          return analysis ? { ...candidate, ...analysis } as SelectionCandidate : candidate
        })
        if (accountId.value === targetAccountId) candidates.value = fetched
        const switchItem = returned.find((item) => {
          if (item.manualAction === 'complete_baiying_verification') return true
          const itemError = [item.error, item.status].filter((value) => typeof value === 'string').join('\n')
          const classification = toUserSelectionError(itemError).classification
          return classification === 'ACCOUNT_SWITCH_CAPTCHA' || classification === 'ACCOUNT_SWITCH_LOGIN' ||
            classification === 'ACCOUNT_SWITCH_DISCONNECT' || classification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED' ||
            classification === 'SEARCHBOX_ZERO_EXHAUSTED' || classification === 'SEARCHBOX_CONTROL_EXHAUSTED'
        })
        if (switchItem) {
          const switchClassification = switchItem.manualAction === 'complete_baiying_verification'
            ? 'ACCOUNT_SWITCH_CAPTCHA'
            : toUserSelectionError([switchItem.error, switchItem.status]
              .filter((value) => typeof value === 'string').join('\n')).classification
          const normalizedClassification = switchClassification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED'
            ? 'ACCOUNT_SWITCH_LOGIN' : switchClassification
          if (normalizedClassification === 'ACCOUNT_SWITCH_CAPTCHA' || normalizedClassification === 'ACCOUNT_SWITCH_LOGIN' ||
              normalizedClassification === 'ACCOUNT_SWITCH_DISCONNECT' || normalizedClassification === 'SEARCHBOX_ZERO_EXHAUSTED' ||
              normalizedClassification === 'SEARCHBOX_CONTROL_EXHAUSTED') {
            const detail = [switchItem.error, switchItem.status].filter((value) => typeof value === 'string').join('\n')
            const userError = toUserSelectionError({ classification: normalizedClassification, message: detail })
            const reason = `${normalizedClassification}：${userError.userMessage}`
            const failedStage = switchItem.cartAdded === true ? 'video_pending' : 'cart_pending'
            if (recovery) {
              await recovery.logFailure({
                stage: failedStage === 'video_pending' ? '商品详情/素材' : '加入选品车',
                code: normalizedClassification,
                message: userError.userMessage,
                detail,
                productId: String(switchItem.productId ?? ''),
                productTitle: String(switchItem.title ?? '')
              })
              await recovery.persist({
                state: 'failed', stage: failedStage, error: reason,
                candidates: fetched, selectedIndexes, completed
              })
            }
            throw new Error(reason)
          }
        }
      }
      await persistStage('quota_cart', { candidates: fetched, selectedIndexes, completed })
    }

    if (!completed.length) throw new Error('恢复任务中没有可继续处理的商品结果。')
    const quotaSettings = (settings.quota ?? cloneDefault().quota) as typeof DEFAULTS.quota
    stage = '记录加车配额'
    accountStatuses[targetAccountId] = '记录加车配额'
    const quotaProducts = completed.filter((item) => item.skipped !== true && item.cartAdded === true && String(item.productId ?? ''))
    for (const [quotaIndex, item] of quotaProducts.entries()) {
      const productId = String(item.productId ?? '')
      reportSelectionProgress(targetAccountId, '记录加车配额', {
        itemIndex: quotaIndex + 1,
        itemTotal: quotaProducts.length,
        message: `记录商品 ${quotaIndex + 1}/${quotaProducts.length} 的配额`
      })
      if (!productId) continue
      if (recovery) {
        await runRecovery([
          'quota-record-once', '--batch-id', recovery.batchId, '--account-id', String(targetAccountId),
          '--product-id', productId, '--event', 'cart', '--videos', '0'
        ])
      } else {
        await runEngine('selection_settings.py', [
          '--db', DB_PATH, 'quota', 'record', '--account-id', String(targetAccountId), '--product-id', productId, '--videos', '0'
        ])
      }
    }
    await persistStage('video_pending', { candidates: fetched, selectedIndexes, completed })

    stage = '商品详情图转视频'
    accountStatuses[targetAccountId] = '商品详情图转视频中'
    reportSelectionProgress(targetAccountId, '商品详情图转视频', {
      itemIndex: 0,
      itemTotal: completed.length,
      message: `准备处理 ${completed.length} 个选品结果`
    })
    const videoResults: string[] = []
    let hasCartFailure = false
    let hasQuotaFailure = false
    let hasQueueFailure = false
    const enqueueGeneratedVideo = async (item: Record<string, unknown>, productId: string): Promise<void> => {
      const videoPath = typeof item.video9x16 === 'string' ? item.video9x16 : ''
      if (!productId || !videoPath) throw new Error('缺少商品 ID 或 9:16 视频路径，不能加入发布队列')
      try {
        const queued = await runEngine('publish_queue.py', [
          '--db', DB_PATH, 'add-generated', '--account-id', String(targetAccountId),
          '--product-id', productId, '--video-path', videoPath
        ])
        if (queued.ok !== true) throw new Error(String(queued.error ?? '发布队列未确认接收任务'))
        const queueId = Number(queued.id)
        if (!Number.isInteger(queueId) || queueId < 1) throw new Error('发布队列没有返回有效任务编号')
        const queueStatus = String(queued.status ?? 'awaiting_confirmation')
        const autoPublish = queued.autoPublish === true
        const queueStatusText = queueStatus === 'awaiting_confirmation'
          ? '待确认'
          : queueStatus === 'scheduled' ? (autoPublish ? '全自动发布已排期' : '已确认排期') : queueStatus
        const rolledDays = Number(queued.rolledDays ?? 0)
        const scheduleAt = String(queued.scheduleAt ?? '')
        let publishQueueStatus = queueStatus
        let queueMessage = queued.alreadyExists === true
          ? `队列已有任务 #${queueId}（${queueStatusText}）`
          : rolledDays > 0
            ? `已加入${autoPublish ? '全自动发布' : '待确认'}队列 #${queueId}，配额顺延 ${rolledDays} 天${scheduleAt ? `，计划时间 ${scheduleAt.replace('T', ' ')}` : ''}`
          : autoPublish
            ? `已加入全自动发布队列 #${queueId}，正在启动发布`
            : `已加入待确认队列 #${queueId}`
        let queueError = ''
        const shouldRunAutoPublish = autoPublish && queued.alreadyExists !== true && rolledDays <= 0
        const currentProductProgress = {
          itemIndex: Math.min(completed.indexOf(item) + 1, completed.length),
          itemTotal: completed.length
        }
        reportSelectionProgress(targetAccountId, shouldRunAutoPublish ? '全自动发布当前商品' : '写入发布队列', {
          ...currentProductProgress,
          percent: shouldRunAutoPublish ? 0 : 100,
          message: shouldRunAutoPublish
            ? `商品已加入队列 #${queueId}，准备自动发布`
            : queueMessage
        })
        if (shouldRunAutoPublish) {
          reportSelectionProgress(targetAccountId, '立即发布当前商品', {
            ...currentProductProgress,
            percent: 0,
            message: `商品 ${String(item.title ?? productId)} 已完成图转视频，正在按账号启动抖音发布`
          })
          activeAutoPublishQueueId = queueId
          try {
            const publishProgress = await window.api.publish.runNow(queueId)
            if (publishProgress.state === 'succeeded') {
              queueMessage = `全自动发布完成（队列 #${queueId}：${publishProgress.stage}）`
              item.publishResult = publishProgress.stage
              publishQueueStatus = publishProgress.stage === '发布成功'
                ? 'published'
                : publishProgress.stage === '已提交平台审核' ? 'submitted_review' : queueStatus
            } else {
              const reason = String(publishProgress.error ?? publishProgress.message ?? '自动发布未完成，任务已保留待核查')
              queueMessage = `本商品自动发布未完成（队列 #${queueId}）：${reason}；选品继续处理后续商品`
              queueError = reason
              await recovery?.logFailure({
                stage: '自动发布', code: 'AUTO_PUBLISH_FAILED',
                message: conciseFailureMessage(reason), detail: reason,
                productId, productTitle: String(item.title ?? '')
              })
            }
          } catch (cause) {
            const reason = String(cause instanceof Error ? cause.message : cause)
            queueMessage = `本商品自动发布未启动（队列 #${queueId}）：${reason}；选品继续处理后续商品`
            queueError = reason
            await recovery?.logFailure({
              stage: '自动发布', code: 'AUTO_PUBLISH_NOT_STARTED',
              message: conciseFailureMessage(reason), detail: reason,
              productId, productTitle: String(item.title ?? '')
            })
          } finally {
            if (activeAutoPublishQueueId === queueId) activeAutoPublishQueueId = null
          }
          reportSelectionProgress(targetAccountId, '当前商品自动发布处理结束', {
            ...currentProductProgress,
            percent: 100,
            message: queueMessage
          })
        }
        item.publishQueueTaskId = queueId
        item.publishQueueStatus = publishQueueStatus
        item.autoPublish = autoPublish
        item.publishQueueError = queueError
        item.status = String(item.status ?? '已加车').replace(/；发布队列(?:失败)?[:：].*$/, '') + '；发布队列：' + queueMessage
        videoResults.push(String(item.title ?? productId) + '：' + queueMessage)
        if (recovery) await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
      } catch (cause) {
        const reason = String(cause instanceof Error ? cause.message : cause)
        hasQueueFailure = true
        item.publishQueueError = reason
        item.status = String(item.status ?? '已加车').replace(/；发布队列(?:失败)?[:：].*$/, '') + '；发布队列失败：' + reason
        videoResults.push(String(item.title ?? productId) + '：加入发布队列失败（' + reason + '）')
        await recovery?.logFailure({
          stage: '写入发布队列', code: 'PUBLISH_QUEUE_FAILED',
          message: '已生成视频，但写入本地发布队列失败：' + conciseFailureMessage(reason),
          detail: reason, productId, productTitle: String(item.title ?? '')
        })
        if (recovery) await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
      }
    }
    for (const [productIndex, item] of completed.entries()) {
      const productId = String(item.productId ?? '')
      reportSelectionProgress(targetAccountId, '生成视频并写入发布队列', {
        itemIndex: productIndex + 1,
        itemTotal: completed.length,
        message: `商品 ${productIndex + 1}/${completed.length}：${String(item.title ?? productId)}`
      })
      if (item.skipped === true) {
        videoResults.push(String(item.title ?? productId) + '：已跳过并记录，继续处理其他商品')
        continue
      }
      if (item.cartAdded !== true) {
        hasCartFailure = true
        videoResults.push(String(item.title ?? productId) + '：加车未确认，任务保留待重试')
        continue
      }
      if (typeof item.video9x16 === 'string') {
        if (recovery && productId) {
          await runRecovery([
            'quota-record-once', '--batch-id', recovery.batchId, '--account-id', String(targetAccountId),
            '--product-id', productId, '--event', 'video', '--videos', '1'
          ])
        }
        await enqueueGeneratedVideo(item, productId)
        continue
      }
      if (item.manualAction === 'complete_baiying_verification' && !resumeAfterCart) {
        const reason = 'ACCOUNT_SWITCH_CAPTCHA：百应要求手动完成验证码或安全验证；已停止当前账号，保留已加车检查点并继续后续账号。'
        item.status = String(item.status ?? '已加车') + '；需要手动完成百应验证，已停止当前账号'
        videoResults.push(String(item.title ?? productId) + '：需要手动完成百应验证，已停止当前账号；恢复时不会重复加车')
        if (recovery) await recovery.persist({ state: 'failed', stage: 'video_pending', error: reason, completed })
        throw new Error(reason)
      }
      if (resumeAfterCart && item.manualAction === 'complete_baiying_verification') {
        delete item.manualAction
      }
      let imagePaths = Array.isArray(item.imagePaths) ? item.imagePaths.map(String) : []
      if (!imagePaths.length || typeof item.outputDir !== 'string') {
        const candidate = fetched.find((row) => row.productId === productId)
        try {
          if (!recovery || !candidate) throw new Error('恢复检查点缺少原商品候选，任务已保留')
          const detail = await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
            ...profile,
            settings,
            accountId: targetAccountId,
            accountKey: `local:${targetAccountId}`,
            downloadPath: context.downloadPath,
            selectedIndexes: [candidate.cardIndex],
            candidateSnapshot: fetched,
            previewOnly: false
          }))
          if (detail.ok !== true) throw new Error(String(detail.error ?? '百应商品详情采集失败'))
          const returned = Array.isArray(detail.completed) ? detail.completed as Array<Record<string, unknown>> : []
          const recovered = returned.find((entry) => String(entry.productId ?? '') === productId)
          if (!recovered || recovered.cartAdded !== true) throw new Error('没有按原商品 ID 恢复到已加车商品')
          const recoveredImages = Array.isArray(recovered.imagePaths) ? recovered.imagePaths.map(String) : []
          if (!recoveredImages.length || typeof recovered.outputDir !== 'string') {
            throw new Error(String(recovered.status ?? '百应商品详情没有返回可用图片'))
          }
          Object.assign(item, recovered, {
            productId,
            cartAdded: true,
            cartStatus: String(item.cartStatus ?? recovered.cartStatus ?? '已在选品车')
          })
          imagePaths = recoveredImages
          await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
        } catch (cause) {
          const reason = String(cause instanceof Error ? cause.message : cause)
          const classification = toUserSelectionError(cause).classification
          if (classification === 'ACCOUNT_SWITCH_CAPTCHA') {
            item.manualAction = 'complete_baiying_verification'
          }
          item.status = String(item.status ?? '已加车') + '；详情图恢复失败：' + reason
          videoResults.push(String(item.title ?? productId) + '：详情图恢复失败（' + reason + '）')
          await recovery?.logFailure({
            stage: '恢复详情图', code: 'DETAIL_IMAGE_RECOVERY_FAILED',
            message: '商品已确认加车，但详情图恢复失败：' + conciseFailureMessage(reason),
            detail: reason, productId, productTitle: String(item.title ?? '')
          })
          if (classification === 'ACCOUNT_SWITCH_CAPTCHA' || classification === 'ACCOUNT_SWITCH_LOGIN' ||
              classification === 'ACCOUNT_SWITCH_DISCONNECT' || classification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED') {
            const userError = toUserSelectionError(cause)
            const switchReason = `${userError.classification}：${userError.userMessage}`
            if (recovery) await recovery.persist({ state: 'failed', stage: 'video_pending', error: switchReason, completed })
            throw new Error(switchReason)
          }
          if (recovery) await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
          continue
        }
      }
      if (!imagePaths.length || typeof item.outputDir !== 'string') {
        item.status = String(item.status ?? '已加车') + '；缺少详情图或输出目录，待恢复'
        videoResults.push(String(item.title ?? productId) + '：缺少详情图或输出目录')
        await recovery?.logFailure({
          stage: '详情图校验', code: 'DETAIL_IMAGES_MISSING',
          message: '缺少详情图或输出目录，无法生成视频',
          detail: String(item.status ?? ''), productId, productTitle: String(item.title ?? '')
        })
        if (recovery) await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
        continue
      }
      try {
        const video = await runEngine(
          'image_to_video.py',
          ['--output-dir', item.outputDir],
          { images: imagePaths },
          5 * 60_000,
          (event) => reportSelectionProgress(targetAccountId, event.stage ?? '详情图转视频', {
            itemIndex: productIndex + 1,
            itemTotal: completed.length,
            message: event.message ?? '',
            percent: typeof event.percent === 'number' ? event.percent : null
          })
        )
        if (video.ok !== true) throw new Error(String(video.error ?? '商品详情图转视频失败'))
        const videoResult = (video.data ?? video) as Record<string, unknown>
        item.video9x16 = videoResult.video9x16
        item.video1x1 = videoResult.video1x1
        // Step 17：显式标记详情图生成来源，避免被千川网页下载素材队列误收。
        for (const generatedPath of [videoResult.video9x16, videoResult.video1x1]) {
          if (typeof generatedPath !== 'string' || !generatedPath) continue
          try {
            const sourceRegistration = await runEngine('asset_library.py', [
              '--db', DB_PATH, 'register', '--file-path', generatedPath,
              '--source-type', 'detail_generated', '--account-id', String(targetAccountId),
              '--product-id', productId
            ])
            if (sourceRegistration.ok !== true) throw new Error(String(sourceRegistration.error ?? '素材来源登记失败'))
          } catch (registerError) {
            await recovery?.logFailure({
              stage: '登记生成素材来源', code: 'ASSET_SOURCE_REGISTER_FAILED',
              message: '详情图视频已生成，但素材库来源登记失败',
              detail: String(registerError), productId, productTitle: String(item.title ?? '')
            })
          }
        }
        item.status = String(item.status ?? '已加车') + '；详情图转视频完成'
        videoResults.push(String(item.title ?? productId) + '：9:16 与 1:1 视频已生成')
        if (recovery) await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
        if (productId) {
          if (recovery) {
            await runRecovery([
              'quota-record-once', '--batch-id', recovery.batchId, '--account-id', String(targetAccountId),
              '--product-id', productId, '--event', 'video', '--videos', '1'
            ])
          } else {
            await runEngine('selection_settings.py', [
              '--db', DB_PATH, 'quota', 'record', '--account-id', String(targetAccountId), '--product-id', productId, '--videos', '1'
            ])
          }
        }
        await enqueueGeneratedVideo(item, productId)
      } catch (cause) {
        const reason = String(cause instanceof Error ? cause.message : cause)
        if (typeof item.video9x16 === 'string') hasQuotaFailure = true
        item.status = String(item.status ?? '已加车') + '；图转视频失败：' + reason
        videoResults.push(String(item.title ?? productId) + '：图转视频失败（' + reason + '）')
        const queueFailure = /发布队列|队列任务|队列没有返回/.test(reason)
        await recovery?.logFailure({
          stage: queueFailure ? '写入发布队列' : '详情图转视频',
          code: queueFailure ? 'PUBLISH_QUEUE_FAILED' : 'IMAGE_TO_VIDEO_FAILED',
          message: conciseFailureMessage((queueFailure ? '写入本地发布队列失败：' : '详情图转视频失败：') + reason),
          detail: reason, productId, productTitle: String(item.title ?? '')
        })
        if (recovery) await recovery.persist({ state: 'in_progress', stage: 'video_pending', completed })
      }
    }

    const hasSkippedProduct = completed.some((item) => item.skipped === true)
    const hasIncompleteProduct = hasQuotaFailure || hasQueueFailure || completed.some((item) =>
      item.skipped !== true && (item.cartAdded !== true || typeof item.video9x16 !== 'string')
    )
    const productSummary = completed.map((item) =>
      String(item.title ?? item.productId) + '：' + String(item.status ?? '已完成')
    ).join('；')
    const categorySummary = categoryWarnings.length
      ? `跳过可恢复的类目 / 来源读取问题 ${categoryWarnings.length} 项（原因已记录）`
      : ''
    const summary = [categorySummary, productSummary, videoResults.length ? '处理结果：' + videoResults.join('；') : '']
      .filter(Boolean).join('；')
    if (recovery) {
      if (hasCartFailure) {
        await recovery.persist({ state: 'needs_retry', stage: 'cart_pending', error: summary, candidates: fetched, selectedIndexes, completed })
      } else if (hasIncompleteProduct) {
        await recovery.persist({ state: 'needs_retry', stage: 'video_pending', error: summary, completed })
      } else {
        await recovery.persist({ state: 'completed', stage: 'completed', error: '', completed })
      }
    }
    const partial = hasIncompleteProduct || hasSkippedProduct || categoryWarnings.length > 0
    accountStatuses[targetAccountId] = partial ? '部分完成' : '完成'
    reportSelectionProgress(targetAccountId, partial ? '当前账号处理结束，存在跳过或待恢复商品' : '当前账号选品完成', {
      itemIndex: completed.length,
      itemTotal: completed.length,
      percent: partial ? null : 100,
      message: summary
    })
    return {
      status: partial ? '部分完成' : '完成',
      message: summary,
      continueBatch: !hasIncompleteProduct && (hasSkippedProduct || categoryWarnings.length > 0)
    }
  } catch (cause) {
    const reason = String(cause instanceof Error ? cause.message : cause)
    if (recovery) {
      await recovery.logFailure({
        stage: recoveryStage || stage,
        code: 'ACCOUNT_STAGE_FAILED',
        message: '阶段「' + stage + '」失败：' + conciseFailureMessage(reason),
        detail: reason
      })
      try {
        await recovery.persist({
          state: getSelectionErrorRecoveryState(reason),
          stage: recoveryStage || stage,
          error: reason
        })
      } catch {
        // 原始平台错误仍要返回；若数据库同时不可写，批次的旧检查点会在下次启动时保留。
      }
    }
    throw new Error('阶段「' + stage + '」失败：' + reason)
  }
}

async function previewCandidates(): Promise<void> {
  if (busy.value) return
  if (!ensureAccountPathsSaved()) return
  if (accountId.value === null) {
    error.value = '请先添加并选择一个百应账号。'
    return
  }
  const targetAccountId = accountId.value
  if (!isSelectionAccountInScope(targetAccountId)) {
    error.value = '所选账号已不存在，无法预览或执行选品。'
    return
  }
  if (accounts.value.find((account) => account.id === targetAccountId)?.status !== 'active') {
    error.value = '当前账号已停用，不能预览或执行选品。'
    return
  }
  busy.value = true
  error.value = ''
  notice.value = ''
  selectionProgress.value = {
    state: 'running', mode: 'preview', accountId: targetAccountId,
    accountName: accounts.value.find((item) => item.id === targetAccountId)?.name ?? `账号 ${targetAccountId}`,
    accountIndex: 1, accountTotal: 1, completedAccounts: 0,
    stage: '准备候选预览', message: '检查账号设置和本地类目计划',
    stepIndex: null, stepTotal: null, stepUnit: '',
    itemIndex: null, itemTotal: null, percent: 0
  }
  try {
    const snapshot = JSON.parse(JSON.stringify(global)) as Record<string, unknown>
    const prepared = await prepareAccountRunContexts(selectionScopeAccounts.value, snapshot)
    const resourceError = prepared.errors.get(targetAccountId)
    const context = prepared.contexts.get(targetAccountId)
    if (resourceError) throw new Error('账号资源隔离校验失败：' + resourceError)
    if (!context) throw new Error('账号资源配置未能解析。')
    const result = await executeAccountSelection(targetAccountId, true, snapshot, undefined, context)
    notice.value = '账号「' + (accounts.value.find((item) => item.id === targetAccountId)?.name ?? targetAccountId) +
      '」预览完成：已找到 ' + candidates.value.length + ' 个候选商品。详细来源统计和类目依据已收起，可在下方进度详情中查看。'
    selectionProgress.value = {
      ...selectionProgress.value!, state: 'finished', stage: '候选预览完成',
      message: result.message ?? '候选预览完成', itemIndex: null, itemTotal: null, percent: 100
    }
  } catch (cause) {
    accountStatuses[targetAccountId] = '失败'
    error.value = String(cause instanceof Error ? cause.message : cause)
    const lastProgress = selectionProgress.value
    selectionProgress.value = {
      ...lastProgress!, state: 'failed',
      stage: lastProgress?.stage ? `失败于：${lastProgress.stage}` : '候选预览失败',
      message: error.value,
      stepIndex: lastProgress?.stepIndex ?? null,
      stepTotal: lastProgress?.stepTotal ?? null,
      stepUnit: lastProgress?.stepUnit ?? '',
      itemIndex: lastProgress?.itemIndex ?? null,
      itemTotal: lastProgress?.itemTotal ?? null,
      percent: lastProgress?.percent ?? null
    }
  } finally {
    busy.value = false
  }
}

function normalizedResourcePath(value: string): string {
  return value.trim().replace(/\//g, '\\').replace(/[\\]+$/, '').toLowerCase()
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(stableJson).join(',') + ']'
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    return '{' + Object.keys(record).sort().map((key) => JSON.stringify(key) + ':' + stableJson(record[key])).join(',') + '}'
  }
  return JSON.stringify(value) ?? 'null'
}

function isSelectionAccountInScope(targetAccountId: number): boolean {
  return accounts.value.some((account) => account.id === targetAccountId)
}

function requireValidCategoryPlan(settings: Record<string, unknown>, accountLabel: string): string {
  const selection = (settings.categorySelection ?? {}) as { level1?: string[]; level2?: string[]; level3?: string[] }
  const tree = Array.isArray(settings.categoryTree) ? settings.categoryTree as CategoryNode[] : []
  const plan = buildSelectionCategoryPlan(selection, tree)
  if (plan.conflicts.length) {
    const plannedPaths = plan.paths.join('；') || '没有可用的末级路径'
    throw new Error(
      `${accountLabel}类目层级配置冲突；按旧展开行为会产生 ${plan.queryCount} 条查询，` +
      `末级路径计划：${plannedPaths}。问题：${plan.conflicts.join('；')}。` +
      '已在本地预检时停止，未调用百应，也未改写账号设置。'
    )
  }
  return plan.catalogLoaded
    ? `${accountLabel} ${plan.queryCount} 条类目查询`
    : `${accountLabel}本地类目目录未加载，无法预估查询数`
}

function settingsForRecoveryCheckpoint(
  settings: Record<string, unknown>,
  checkpoint: SelectionRecoveryAccount,
  accountLabel: string
): Record<string, unknown> {
  // A-104 只读取百应当前可见来源；手动类目模式仅在本机筛选这些候选。
  settings = { ...settings, _visibleSourcesOnly: true }
  const candidates = Array.isArray(checkpoint.candidates) ? checkpoint.candidates : []
  const selectedIndexes = Array.isArray(checkpoint.selectedIndexes) ? checkpoint.selectedIndexes : []
  const completed = Array.isArray(checkpoint.completed) ? checkpoint.completed : []
  const recoveryMode = getSelectionRecoveryMode(
    checkpoint.stage,
    candidates.length > 0 && selectedIndexes.length > 0,
    completed
  )
  if (!recoveryMode.resumeCart && !recoveryMode.resumeAfterCart) return settings
  if (settings._visibleSourcesOnly === true) return settings

  const tree = Array.isArray(settings.categoryTree) ? settings.categoryTree as CategoryNode[] : []
  const resolved = resolveRecoveryCategorySelection(candidates, selectedIndexes, tree)
  if (resolved.selection) {
    const scoped = { ...settings, categorySelection: resolved.selection }
    requireValidCategoryPlan(scoped, accountLabel + '恢复商品来源')
    return scoped
  }

  const savedSelection = (settings.categorySelection ?? {}) as {
    level1?: string[]; level2?: string[]; level3?: string[]
  }
  const savedPlan = buildSelectionCategoryPlan(savedSelection, tree)
  if (!savedPlan.conflicts.length) return settings
  throw new Error(
    accountLabel + '无法按原商品来源安全恢复：' +
    (resolved.error ?? '保存的完整类目计划存在冲突')
  )
}

async function validateRecoveryContexts(batch: SelectionRecoveryBatch): Promise<{
  contexts: Map<number, AccountRunContext>
  errors: Map<number, string>
}> {
  const contexts = new Map<number, AccountRunContext>()
  const errors = new Map<number, string>()
  const stored = (batch.payload.accountContexts ?? {}) as Record<string, AccountRunContext>
  const pending = batch.accounts.filter((item) => item.state !== 'completed')
  for (const item of pending) {
    const context = stored[String(item.accountId)]
    if (!context) {
      errors.set(item.accountId, '恢复批次缺少账号资源快照。')
      continue
    }
    try {
      const profile = await window.api.browser.validateProfilePath({
        profileKey: context.profileKey,
        profilePath: context.profilePath || undefined
      })
      if (!profile.ok || !profile.path) throw new Error(profile.error ?? '浏览器 profile 校验失败')
      if (normalizedResourcePath(profile.path) !== normalizedResourcePath(context.resolvedProfilePath)) {
        throw new Error('浏览器 profile 路径与批次快照不一致')
      }
      const download = await window.api.dir.canonicalizePath(context.downloadPath)
      if (!download.ok || !download.path) throw new Error(download.error ?? '下载路径校验失败')
      if (normalizedResourcePath(download.path) !== normalizedResourcePath(context.downloadPath)) {
        throw new Error('下载路径与批次快照不一致')
      }
      contexts.set(item.accountId, { ...context, resolvedProfilePath: profile.path, downloadPath: download.path })
    } catch (cause) {
      errors.set(item.accountId, String(cause instanceof Error ? cause.message : cause))
    }
  }
  const accountNames = new Map(batch.accounts.map((item) => [item.accountId, item.accountName]))
  const conflicts = findAccountResourcePathConflicts([...contexts].map(([id, context]) => ({
    accountId: id,
    accountName: accountNames.get(id) ?? String(id),
    browserProfilePath: context.resolvedProfilePath,
    downloadPath: context.downloadPath
  })))
  for (const [id, messages] of conflicts) {
    errors.set(id, [errors.get(id), ...messages].filter(Boolean).join('；'))
  }
  return { contexts, errors }
}

async function loadRecoveryStatus(): Promise<void> {
  const result = await runRecovery(['batch', 'latest'])
  const activeBatch = result.batch as SelectionRecoveryBatch | null
  const recentResult = activeBatch ? null : await runRecovery(['batch', 'recent'])
  const batch = activeBatch ?? (recentResult?.batch as SelectionRecoveryBatch | null)
  recoveryBatch.value = batch
  restoreSelectionCooldown(batch)
  if (!batch || !['in_progress', 'needs_retry'].includes(batch.state)) return
  const stageNames: Record<string, string> = {
    quota_check: '配额校验', candidate_fetch: '获取候选', cart_pending: '加入选品车',
    quota_cart: '记录加车配额', video_pending: '生成详情视频'
  }
  for (const item of batch.accounts) {
    accountStatuses[item.accountId] = item.state === 'completed'
      ? '完成'
      : item.state === 'failed' || item.state === 'needs_retry'
        ? '待恢复（' + (stageNames[item.stage] ?? item.stage ?? '上次任务') + '）'
        : '待恢复'
  }
  const remaining = batch.accounts.filter((item) => item.state !== 'completed').length
  if (remaining) {
    notice.value = '发现未完成批次：' + remaining + ' 个账号任务已保存在本机。点击“继续选品”将从最近检查点继续。'
  }
}

async function saveCandidateFetchRateLimitCheckpoint(
  batchId: string,
  pendingAccounts: Array<{ accountId: number; accountName: string }>,
  failedAccountId: number,
  reason: string
): Promise<void> {
  for (const account of pendingAccounts) {
    const isFailedAccount = account.accountId === failedAccountId
    await runRecovery([
      'account', 'update', '--batch-id', batchId, '--account-id', String(account.accountId)
    ], {
      state: isFailedAccount ? 'needs_retry' : 'queued',
      stage: 'candidate_fetch',
      error: isFailedAccount ? reason : ''
    })
  }
  await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', String(failedAccountId)], {
    stage: '获取候选',
    code: 'BAIYING_RATE_LIMIT',
    message: toUserSelectionMessage(reason),
    detail: reason
  })
  await runRecovery(['batch', 'update', '--batch-id', batchId, '--state', 'needs_retry'])
  const latest = await runRecovery(['batch', 'latest'])
  recoveryBatch.value = latest.batch as SelectionRecoveryBatch | null
  restoreSelectionCooldown(recoveryBatch.value)
}

function setRecoveryAccountSelected(accountId: number, checked: boolean): void {
  const selected = new Set(recoverySelectedAccountIds.value)
  if (checked) selected.add(accountId)
  else selected.delete(accountId)
  recoverySelectedAccountIds.value = [...selected]
}

function selectAllPendingRecoveryAccounts(): void {
  recoverySelectedAccountIds.value = pendingRecoveryAccounts.value.map((account) => account.accountId)
}

function clearPendingRecoveryAccountSelection(): void {
  recoverySelectedAccountIds.value = []
}

function recoveryAccountStageLabel(account: SelectionRecoveryAccount): string {
  const labels: Record<string, string> = {
    quota_check: '配额校验',
    candidate_fetch: '获取候选',
    cart_pending: '加入选品车',
    quota_cart: '记录加车配额',
    video_pending: '生成详情视频'
  }
  return labels[account.stage] ?? (account.state === 'queued' ? '排队中' : '待恢复')
}

async function abandonStaleBatch(): Promise<void> {
  const batch = recoveryBatch.value
  if (busy.value || !batch || batch.state !== 'needs_retry') return
  const pending = batch.accounts.filter((item) => item.state !== 'completed')
  if (batch.accounts.some((item) => item.state === 'in_progress')) {
    error.value = '批次仍有执行中的账号任务，暂不能结束。'
    return
  }
  const pendingNames = pending.map((item) => item.accountName + '（' + (item.stage || item.state) + '）').join('、')
  const accepted = window.confirm(
    '结束待恢复批次 ' + batch.batchId + '？\n\n' +
    '待恢复账号：' + (pendingNames || '无') + '\n\n' +
    '结束后不会再自动恢复此批次；候选、商品结果及配额事件检查点会保留，批次不会被标记为成功。'
  )
  if (!accepted) return

  busy.value = true
  error.value = ''
  try {
    const result = await runRecovery([
      'batch', 'abandon', '--batch-id', batch.batchId,
      '--reason', '用户在选品设置页面确认结束待恢复批次；保留检查点，不标记成功。'
    ])
    const ended = result.batch as SelectionRecoveryBatch
    recoveryBatch.value = null
    for (const item of ended.accounts) {
      if (item.state !== 'completed') accountStatuses[item.accountId] = '已结束（检查点保留）'
    }
    notice.value = '批次 ' + ended.batchId + ' 已结束；检查点与配额事件保留，未标记成功。现在可以创建新批次。'
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
    await loadRecoveryStatus()
  } finally {
    busy.value = false
  }
}

function loginPreflightFingerprint(batchId: string | null, targets: LoginPreflightTarget[]): string {
  const accountProfiles = [...targets]
    .sort((left, right) => left.accountId - right.accountId)
    .map((item) => ({ id: item.accountId, profile: item.resolvedProfilePath ?? item.profilePath ?? '' }))
  return JSON.stringify({ batchId, accounts: accountProfiles })
}

function decodeLoginPlatformResult(value: unknown, fallback: string): LoginPlatformResult {
  const result = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const state = result.state
  const allowed: LoginCheckState[] = ['logged_in', 'login_required', 'verification_required', 'unknown']
  return {
    state: allowed.includes(state as LoginCheckState) ? state as LoginCheckState : 'unknown',
    message: typeof result.message === 'string' && result.message.trim() ? result.message : fallback
  }
}

async function checkLoginTargets(targets: LoginPreflightTarget[]): Promise<LoginPreflightAccount[]> {
  const results: LoginPreflightAccount[] = []
  for (const [targetIndex, target] of targets.entries()) {
    reportSelectionProgress(target.accountId, '检查百应与创作者中心登录态', {
      accountIndex: targetIndex + 1,
      accountTotal: targets.length,
      completedAccounts: targetIndex,
      stepIndex: targetIndex,
      stepTotal: targets.length,
      stepUnit: '账号',
      percent: targets.length ? Math.floor(targetIndex * 100 / targets.length) : 0,
      message: `正在检查账号「${target.accountName}」的百应和创作者中心登录状态（${targetIndex + 1}/${targets.length}）。`
    })
    if (target.contextError) {
      const message = '账号浏览器配置无法确认：' + target.contextError
      results.push({
        ...target,
        baiying: { state: 'unknown', message },
        creatorCenter: { state: 'unknown', message }
      })
      continue
    }
    const checkOnce = async (): Promise<LoginPreflightAccount> => {
      try {
        const response = await window.api.browser.checkAccountLogins({
          accountId: target.accountId,
          profilePath: target.profilePath
        })
        if (response.ok !== true) {
          const message = typeof response.error === 'string' ? response.error : '登录状态检查失败，无法确认。'
          return {
            ...target,
            baiying: { state: 'unknown', message },
            creatorCenter: { state: 'unknown', message }
          }
        }
        return {
          ...target,
          baiying: decodeLoginPlatformResult(response.baiying, '百应登录状态无法确认。'),
          creatorCenter: decodeLoginPlatformResult(response.creatorCenter, '创作者中心登录状态无法确认。')
        }
      } catch (cause) {
        const message = '登录状态检查失败：' + String(cause instanceof Error ? cause.message : cause)
        return {
          ...target,
          baiying: { state: 'unknown', message },
          creatorCenter: { state: 'unknown', message }
        }
      }
    }
    const checked = await retryUnconfirmedLoginCheck(
      checkOnce,
      isUnconfirmedLoginFalseNegative,
      {
        onRetry: () => reportSelectionProgress(target.accountId, '复核登录状态', {
          message: `账号「${target.accountName}」页面已打开但登录工作台尚未识别，正在短暂复查一次。`
        })
      }
    )
    results.push(checked.value)
    reportSelectionProgress(target.accountId, '检查百应与创作者中心登录态', {
      accountIndex: targetIndex + 1,
      accountTotal: targets.length,
      completedAccounts: targetIndex + 1,
      stepIndex: targetIndex + 1,
      stepTotal: targets.length,
      stepUnit: '账号',
      percent: Math.floor((targetIndex + 1) * 100 / targets.length),
      message: `账号「${target.accountName}」登录状态检查完成（${targetIndex + 1}/${targets.length}）。`
    })
  }
  return results
}

interface AccountSwitchFailure {
  classification: 'ACCOUNT_SWITCH_CAPTCHA' | 'ACCOUNT_SWITCH_LOGIN' | 'ACCOUNT_SWITCH_DISCONNECT' |
    'SEARCHBOX_ZERO_EXHAUSTED' | 'SEARCHBOX_CONTROL_EXHAUSTED'
  reason: string
  message: string
  detail: string
  stage: string
}

function createAccountSwitchFailure(
  account: Pick<AccountRow, 'id' | 'name'>,
  cause: unknown,
  classification: AccountSwitchFailure['classification'] | 'LOGIN_OR_PAGE_STATE_UNCONFIRMED'
): AccountSwitchFailure {
  const normalizedClassification: AccountSwitchFailure['classification'] = classification === 'LOGIN_OR_PAGE_STATE_UNCONFIRMED'
    ? 'ACCOUNT_SWITCH_LOGIN' : classification
  const detail = cause instanceof Error ? cause.message : String(cause)
  const userMessage = toUserSelectionMessage({ classification: normalizedClassification, message: detail })
  return {
    classification: normalizedClassification,
    reason: `${normalizedClassification}：账号「${account.name}」${userMessage}`,
    message: `账号「${account.name}」${userMessage}`,
    detail,
    stage: normalizedClassification === 'SEARCHBOX_ZERO_EXHAUSTED' || normalizedClassification === 'SEARCHBOX_CONTROL_EXHAUSTED'
      ? '百应选品搜索框恢复'
      : normalizedClassification === 'ACCOUNT_SWITCH_CAPTCHA'
        ? '账号安全验证检查'
        : normalizedClassification === 'ACCOUNT_SWITCH_DISCONNECT'
          ? '账号浏览器连接检查'
          : '账号登录状态检查'
  }
}

function getAccountSwitchFailure(result: LoginPreflightAccount[]): AccountSwitchFailure | null {
  const account = result[0]
  if (!account || loginTargetsPassed(result)) return null
  const platforms = [
    { name: '百应', ...account.baiying },
    { name: '创作者中心', ...account.creatorCenter }
  ].filter((item) => item.state !== 'logged_in')
  const detail = platforms.map((item) => item.name + '：' + item.message).join('；')
  const messageText = detail || '无法确认该账号的平台页面和登录状态。'
  const hasVerification = platforms.some((item) => item.state === 'verification_required')
  const messageClassification = toUserSelectionError(messageText).classification
  // 频控仍由候选读取阶段的卡②检查点处理，不能误当登录失效换号。
  if (messageClassification === 'BAIYING_RATE_LIMIT') return null
  const classification: AccountSwitchFailure['classification'] = hasVerification
    ? 'ACCOUNT_SWITCH_CAPTCHA'
    : messageClassification === 'ACCOUNT_SWITCH_CAPTCHA'
      ? 'ACCOUNT_SWITCH_CAPTCHA'
      : messageClassification === 'ACCOUNT_SWITCH_DISCONNECT'
        ? 'ACCOUNT_SWITCH_DISCONNECT'
        : 'ACCOUNT_SWITCH_LOGIN'
  const message = toUserSelectionMessage({ classification, message: messageText })
  return {
    classification,
    reason: `${classification}：账号「${account.accountName}」${message}`,
    message: `账号「${account.accountName}」${message}`,
    detail: messageText,
    stage: classification === 'ACCOUNT_SWITCH_CAPTCHA'
      ? '账号安全验证检查'
      : classification === 'ACCOUNT_SWITCH_DISCONNECT'
        ? '账号浏览器连接检查'
        : '账号登录状态检查'
  }
}

async function collectAccountSwitchFailures(
  targets: LoginPreflightTarget[],
  batchId: string | null,
  scopeFingerprint?: string
): Promise<Map<number, AccountSwitchFailure>> {
  const fingerprint = loginPreflightFingerprint(batchId, targets)
  if (verifiedLoginPreflight?.fingerprint === fingerprint && verifiedLoginPreflight.expiresAt > Date.now()) {
    verifiedLoginPreflight = null
    return new Map()
  }
  verifiedLoginPreflight = null
  const results = await checkLoginTargets(targets)
  if (scopeFingerprint && results.some((item) => isUnconfirmedLoginFalseNegative(item))) {
    loginPreflight.value = {
      batchId,
      scopeFingerprint,
      accounts: results,
      checking: false
    }
    const unconfirmed = results
      .filter((item) => isUnconfirmedLoginFalseNegative(item))
      .map((item) => item.accountName)
      .join('、')
    if (selectionProgress.value) {
      selectionProgress.value = {
        ...selectionProgress.value,
        state: 'finished',
        stage: '等待确认账号登录状态',
        message: `账号「${unconfirmed}」页面已打开但工作台未能确认；请在账号专属浏览器核实后重新检查或手动确认。尚未创建或修改批次。`,
        percent: null
      }
    }
    return new Map()
  }
  const failures = new Map<number, AccountSwitchFailure>()
  for (const result of results) {
    const failure = getAccountSwitchFailure([result])
    if (failure) failures.set(result.accountId, failure)
  }
  if (failures.size > 0) {
    loginPreflight.value = {
      batchId,
      scopeFingerprint: scopeFingerprint ?? loginPreflightFingerprint(batchId, targets),
      accounts: results,
      checking: false
    }
    const blockedAccounts = results
      .filter((item) => failures.has(item.accountId))
      .map((item) => item.accountName)
      .join('、')
    if (selectionProgress.value) {
      selectionProgress.value = {
        ...selectionProgress.value,
        state: 'finished',
        stage: '等待账号登录或验证',
        message: `账号「${blockedAccounts}」的百应或创作者中心需要登录/验证；尚未创建或修改批次。请先处理后重新检查。`,
        percent: null
      }
    }
    return new Map()
  }
  return failures
}

function loginTargetsPassed(results: LoginPreflightAccount[]): boolean {
  return results.length > 0 && results.every((item) =>
    item.baiying.state === 'logged_in' && item.creatorCenter.state === 'logged_in'
  )
}

function loginPreflightReady(results: LoginPreflightAccount[]): boolean {
  return results.length > 0 && results.every((item) =>
    (item.baiying.state === 'logged_in' || (item.baiying.state === 'unknown' && item.manualBaiyingConfirmed === true)) &&
    (item.creatorCenter.state === 'logged_in' || (item.creatorCenter.state === 'unknown' && item.manualCreatorCenterConfirmed === true))
  )
}

function loginStateLabel(state: LoginCheckState): string {
  if (state === 'logged_in') return '已确认登录'
  if (state === 'login_required') return '需要登录'
  if (state === 'verification_required') return '需要人工验证'
  if (state === 'checking') return '检查中'
  return '无法确认'
}

function confirmSelectionScope(fingerprint: string, message: string): boolean {
  const pending = scopeConfirmationToResume
  scopeConfirmationToResume = null
  if (pending && pending.expiresAt > Date.now() && pending.fingerprint === fingerprint) return true
  return window.confirm(message)
}

async function recheckSelectionLogins(): Promise<void> {
  const current = loginPreflight.value
  if (!current || current.checking) return
  current.checking = true
  if (selectionProgress.value) {
    selectionProgress.value = {
      ...selectionProgress.value,
      state: 'running',
      stage: '重新检查平台登录态',
      message: '正在检查所选账号的百应和创作者中心。',
      percent: null
    }
  }
  const targets = current.accounts.map(({ accountId, accountName, profilePath, resolvedProfilePath, contextError }) => ({
    accountId, accountName, profilePath, resolvedProfilePath, contextError
  }))
  try {
    const results = await checkLoginTargets(targets)
    loginPreflight.value = { ...current, accounts: results, checking: false }
    if (loginTargetsPassed(results)) {
      verifiedLoginPreflight = {
        fingerprint: loginPreflightFingerprint(current.batchId, targets),
        expiresAt: Date.now() + 5 * 60_000
      }
      if (selectionProgress.value) {
        selectionProgress.value = {
          ...selectionProgress.value,
          state: 'finished',
          stage: '登录检查完成',
          message: '所选账号的百应和创作者中心均已确认登录。',
          percent: 100
        }
      }
      notice.value = '所选账号的百应和创作者中心均已确认登录。请点击「继续选品」重新开始；客户端会再次展示账号范围确认。'
    } else if (selectionProgress.value) {
      selectionProgress.value = {
        ...selectionProgress.value,
        state: 'finished',
        stage: '登录检查未通过',
        message: '仍有账号未确认完成两站登录；本次不会创建或修改选品批次。',
        percent: null
      }
    }
  } catch (cause) {
    loginPreflight.value = { ...current, checking: false }
    if (selectionProgress.value) {
      selectionProgress.value = {
        ...selectionProgress.value,
        state: 'failed',
        stage: '登录检查失败',
        message: '重新检查过程中发生错误。',
        percent: null
      }
    }
    error.value = '重新检查登录状态失败：' + String(cause instanceof Error ? cause.message : cause)
  }
}

async function openPreflightLogin(account: LoginPreflightAccount, platform: 'baiying' | 'creatorCenter'): Promise<void> {
  const key = `${account.accountId}:${platform}`
  if (openingLoginTarget.value) return
  openingLoginTarget.value = key
  try {
    const result = platform === 'baiying'
      ? await window.api.account.openJingxuan(account.accountId, account.profilePath)
      : await window.api.browser.openDouyinCreator({ accountId: account.accountId, profilePath: account.profilePath })
    if (result.ok !== true) throw new Error(typeof result.error === 'string' ? result.error : '无法打开登录页面')
    notice.value = `已打开账号「${account.accountName}」的${platform === 'baiying' ? '百应' : '抖音创作者中心'}页面；完成登录后返回此处重新检查。`
  } catch (cause) {
    error.value = `打开账号「${account.accountName}」登录页面失败：${String(cause instanceof Error ? cause.message : cause)}`
  } finally {
    openingLoginTarget.value = null
  }
}

function continueAfterLoginPreflight(): void {
  const current = loginPreflight.value
  if (!current || current.checking || !loginPreflightReady(current.accounts)) return
  const targets = current.accounts.map(({ accountId, accountName, profilePath, resolvedProfilePath, contextError }) => ({
    accountId, accountName, profilePath, resolvedProfilePath, contextError
  }))
  // 自动检测通过或用户逐个平台确认后，只跳过本次重启选品流程中的自动检查。
  verifiedLoginPreflight = {
    fingerprint: loginPreflightFingerprint(current.batchId, targets),
    expiresAt: Date.now() + 2 * 60_000
  }
  scopeConfirmationToResume = {
    fingerprint: current.scopeFingerprint,
    expiresAt: Date.now() + 2 * 60_000
  }
  loginPreflight.value = null
  startSelection()
}

function toggleManualLoginConfirmation(account: LoginPreflightAccount, platform: 'baiying' | 'creatorCenter'): void {
  if (!loginPreflight.value || loginPreflight.value.checking) return
  const platformState = platform === 'baiying' ? account.baiying.state : account.creatorCenter.state
  if (platformState !== 'unknown') {
    error.value = '检测结果为需要登录或人工验证时，必须先在专属浏览器完成处理并重新检查，不能手动跳过。'
    return
  }
  if (account.contextError || !(account.resolvedProfilePath || account.profilePath)) {
    error.value = `账号「${account.accountName}」的专属浏览器配置无法确认，不能跳过登录检测。`
    return
  }
  if (platform === 'baiying') {
    account.manualBaiyingConfirmed = !account.manualBaiyingConfirmed
  } else {
    account.manualCreatorCenterConfirmed = !account.manualCreatorCenterConfirmed
  }
  verifiedLoginPreflight = null
  error.value = ''
}

async function runSelectionBatch(): Promise<void> {
  const requestedAutoResumeBatchId = automaticResumeBatchId
  const requestedAutoResumeAccountId = automaticResumeAccountId
  automaticResumeBatchId = null
  automaticResumeAccountId = null
  if (busy.value) {
    const intent = cooldownAutoResumeIntent.value
    if (requestedAutoResumeBatchId && intent?.batchId === requestedAutoResumeBatchId &&
      intent.accountId === requestedAutoResumeAccountId && intent.phase === 'starting') {
      cooldownAutoResumeIntent.value = { ...intent, phase: 'pending' }
      traceSelectionCooldown('renderer_auto_resume_deferred', requestedAutoResumeBatchId, {
        accountId: requestedAutoResumeAccountId, reason: 'busy_at_batch_entry'
      })
    }
    return
  }
  if (!requestedAutoResumeBatchId && !ensureAccountPathsSaved()) return
  if (requestedAutoResumeBatchId && accountPathsHaveUnsavedChanges.value) {
    traceSelectionCooldown('automatic_recovery_unsaved_path_drift_ignored', requestedAutoResumeBatchId, {
      source: 'selection_settings_database', accountId: requestedAutoResumeAccountId
    })
  }
  busy.value = true
  error.value = ''
  notice.value = ''
  runLogWarning.value = ''
  selectionProgress.value = {
    state: 'running', mode: 'batch', accountId: null, accountName: '',
    accountIndex: 0, accountTotal: 0, completedAccounts: 0,
    stage: '准备选品批次', message: '读取启用账号和待恢复检查点',
    itemIndex: null, itemTotal: null, percent: null
  }
  try {
    // 每次创建或恢复正式批次前都从数据库刷新账号，避免沿用组件挂载时的旧范围。
    await loadAccounts()
    const latestResult = await runRecovery(['batch', 'latest'])
    let batch = latestResult.batch as SelectionRecoveryBatch | null
    if (requestedAutoResumeBatchId) {
      const limitedAccount = batch?.accounts.find((item) =>
        item.accountId === requestedAutoResumeAccountId && item.state === 'needs_retry' &&
        toUserSelectionError(item.error).classification === 'BAIYING_RATE_LIMIT'
      )
      const activeIntent = cooldownAutoResumeIntent.value
      const matchingIntent = Boolean(batch && activeIntent &&
        activeIntent.batchId === requestedAutoResumeBatchId && activeIntent.batchId === batch.batchId &&
        activeIntent.accountId === requestedAutoResumeAccountId && activeIntent.phase === 'starting')
      const pendingTarget = batch?.accounts.find((item) =>
        item.accountId === requestedAutoResumeAccountId && item.state !== 'completed'
      )
      const checkpointStillResumable = Boolean(batch && batch.state === 'needs_retry' && limitedAccount)
      const inFlightCheckpointStillResumable = Boolean(matchingIntent && pendingTarget)
      if (!batch || batch.batchId !== requestedAutoResumeBatchId ||
        (!checkpointStillResumable && !inFlightCheckpointStillResumable)) {
        traceSelectionCooldown('renderer_auto_resume_checkpoint_rejected', requestedAutoResumeBatchId, {
          currentBatchId: batch?.batchId ?? null, batchState: batch?.state ?? null,
          accountId: requestedAutoResumeAccountId, targetState: pendingTarget?.state ?? null,
          matchingIntent, checkpointStillResumable, inFlightCheckpointStillResumable
        })
        cooldownAutoResumeIntent.value = null
        autoResumeAccountId.value = null
        acknowledgeSelectionCooldownEvent({
          batchId: requestedAutoResumeBatchId,
          deadline: activeIntent?.deadline ?? selectionCooldownDeadline ?? Date.now()
        })
        notice.value = '冷却结束，但原限流检查点已变化或不再待恢复；未启动新批次，请检查当前批次后再继续。'
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '自动恢复检查点已变化',
          message: notice.value, percent: null
        }
        return
      }
      recoveryBatch.value = batch
    }
    const resumingExistingBatch = Boolean(batch)
    let snapshot: Record<string, unknown>
    let reviewedAccountId: number | null
    let reviewedProducts: Set<string> | undefined
    let contexts: Map<number, AccountRunContext>
    let contextErrors = new Map<number, string>()
    let preparedCandidatePlans: Record<string, AccountCandidatePlan> = {}
    let recoveryAccountIds: Set<number> | null = null
    const accountSwitchFailures = new Map<number, AccountSwitchFailure>()

    if (batch) {
      const enabledAccounts = accounts.value
        .filter((item) => item.status === 'active')
        .sort((left, right) => left.id - right.id)
      const batchAccountIds = batch.accounts.map((item) => item.accountId).sort((left, right) => left - right)
      const enabledAccountIds = enabledAccounts.map((item) => item.id)
      const sameScope = batchAccountIds.length === enabledAccountIds.length &&
        batchAccountIds.every((id, index) => id === enabledAccountIds[index])
      if (!sameScope) {
        const batchScope = batch.accounts.map((item) => `${item.accountName}（ID ${item.accountId}）`).join('、') || '无'
        const enabledScope = enabledAccounts.map((item) => `${item.name}（ID ${item.id}）`).join('、') || '无'
        throw new Error(
          `待恢复批次账号范围与数据库当前启用账号不一致；为避免漏跑或扩跑，本次没有修改或恢复批次。` +
          `\n批次范围：${batchScope}\n当前启用：${enabledScope}`
        )
      }
      const pendingAccounts = batch.accounts.filter((item) => item.state !== 'completed')
      const selectedIds = recoverySelectionBatchId.value === batch.batchId
        ? new Set(recoverySelectedAccountIds.value)
        : new Set(pendingAccounts.map((item) => item.accountId))
      const selectedPendingAccounts = filterPendingRecoveryAccounts(batch.accounts, selectedIds)
      if (!selectedPendingAccounts.length) {
        throw new Error('请至少勾选一个待恢复账号；未勾选的账号会保留原检查点。')
      }
      const isAutomaticRecovery = requestedAutoResumeBatchId === batch.batchId
      if (isAutomaticRecovery && !selectedPendingAccounts.some((item) => item.accountId === requestedAutoResumeAccountId)) {
        notice.value = '冷却结束，但限流账号未包含在恢复范围内；请确认待恢复账号后继续。'
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '自动恢复范围已变化',
          message: notice.value, percent: null
        }
        return
      }
      recoveryAccountIds = new Set(selectedPendingAccounts.map((item) => item.accountId))

      const refreshableStage = selectedPendingAccounts.length > 0 && selectedPendingAccounts.every((item) =>
        ['', 'queued', 'quota_check', 'candidate_fetch'].includes(item.stage) &&
        !(item.candidates?.length) && !(item.selectedIndexes?.length) && !(item.completed?.length)
      )
      let refreshableUpdate: {
        settingsChanged: boolean
        settingsDiff: {
          snapshotChanged: boolean
          changedContextAccountIds: number[]
          changedContextFields: Array<{ accountId: number; fields: string[] }>
        }
        currentSnapshot: Record<string, unknown>
        freshContexts: Record<string, AccountRunContext>
      } | null = null
      if (refreshableStage) {
        if (isAutomaticRecovery) {
          // 自动续跑只使用数据库里已保存的设置，忽略页面上尚未保存的编辑漂移。
          await loadGlobal()
          traceSelectionCooldown('automatic_recovery_saved_settings_loaded', batch.batchId, {
            source: 'selection_settings_database',
            pendingAccountIds: pendingAccounts.map((item) => item.accountId)
          })
        }
        const currentSnapshot = JSON.parse(JSON.stringify(global)) as Record<string, unknown>
        const pendingIds = new Set(pendingAccounts.map((item) => item.accountId))
        const pendingRows = accounts.value.filter((item) => pendingIds.has(item.id))
        const prepared = await prepareAccountRunContexts(pendingRows, currentSnapshot)
        const freshContexts: Record<string, AccountRunContext> = {
          ...((batch.payload.accountContexts ?? {}) as Record<string, AccountRunContext>)
        }
        for (const item of pendingAccounts) {
          const reason = prepared.errors.get(item.accountId)
          if (reason) throw new Error('恢复账号「' + item.accountName + '」的资源校验失败：' + reason)
          const fresh = prepared.contexts.get(item.accountId)
          if (!fresh) throw new Error('恢复账号「' + item.accountName + '」未能生成当前设置快照。')
          freshContexts[String(item.accountId)] = fresh
        }
        const previousSnapshot = batch.payload.settingsSnapshot
        const previousContexts = (batch.payload.accountContexts ?? {}) as Record<string, AccountRunContext>
        const settingsDiff = compareRecoverySettings(
          previousSnapshot,
          currentSnapshot,
          previousContexts as Record<string, unknown>,
          freshContexts as Record<string, unknown>,
          pendingAccounts.map((item) => item.accountId)
        )
        refreshableUpdate = {
          settingsChanged: settingsDiff.changed,
          settingsDiff,
          currentSnapshot,
          freshContexts
        }
      }

      const currentById = new Map(enabledAccounts.map((item) => [item.id, item]))
      const selectedSummary = selectedPendingAccounts.map((item) =>
        `${currentById.get(item.accountId)?.name ?? item.accountName}（ID ${item.accountId}）`
      ).join('、')
      const completedSummary = batch.accounts.filter((item) => item.state === 'completed').map((item) =>
        `${currentById.get(item.accountId)?.name ?? item.accountName}（ID ${item.accountId}）`
      ).join('、')
      const preservedSummary = pendingAccounts.filter((item) => !recoveryAccountIds?.has(item.accountId)).map((item) =>
        `${currentById.get(item.accountId)?.name ?? item.accountName}（ID ${item.accountId}）`
      ).join('、')
      const savedAccountContexts = (batch.payload.accountContexts ?? {}) as Record<string, AccountRunContext>
      const displayedContexts = refreshableUpdate?.settingsChanged
        ? refreshableUpdate.freshContexts
        : savedAccountContexts
      const recoveryPathSummary = selectedPendingAccounts.map((item) => {
        const effectivePath = displayedContexts[String(item.accountId)]?.downloadPath
        return `${currentById.get(item.accountId)?.name ?? item.accountName}：${effectivePath || '沿用检查点中已保存的商品目录'}`
      }).join('\n')
      const recoverySummary = [
        `本次恢复：${selectedSummary}`,
        completedSummary ? `已完成并跳过：${completedSummary}` : '',
        preservedSummary ? `未勾选，保留原检查点：${preservedSummary}` : '',
        `素材保存路径${refreshableUpdate?.settingsChanged ? '（候选尚未查询，将按当前已保存设置刷新）' : '（沿用本批次快照）'}：\n${recoveryPathSummary}`,
        '已有素材不会搬迁。'
      ].filter(Boolean).join('\n')
      const recoveryScopeFingerprint = stableJson({
        kind: 'recovery',
        batchId: batch.batchId,
        accounts: selectedPendingAccounts.map((item) => {
          const context = displayedContexts[String(item.accountId)]
          return {
            id: item.accountId,
            profilePath: context?.resolvedProfilePath ?? context?.profilePath ?? '',
            downloadPath: context?.downloadPath ?? ''
          }
        }).sort((left, right) => left.id - right.id),
        settings: refreshableUpdate?.currentSnapshot ?? batch.payload.settingsSnapshot
      })
      const settingsDecision = decideRecoverySettingsChange(
        isAutomaticRecovery,
        refreshableUpdate?.settingsChanged ?? false
      )
      if (settingsDecision === 'auto_continue_current_saved_settings' && refreshableUpdate) {
        traceSelectionCooldown('settings_changed_auto_continue', batch.batchId, {
          source: 'selection_settings_database',
          usesCurrentSavedSettings: true,
          settingsSnapshotChanged: refreshableUpdate.settingsDiff.snapshotChanged,
          changedContextAccountIds: refreshableUpdate.settingsDiff.changedContextAccountIds,
          changedContextFields: refreshableUpdate.settingsDiff.changedContextFields,
          scopeConfirmation: 'automatic_fingerprint_short_circuit'
        })
      } else if (settingsDecision === 'manual_confirmation' && refreshableUpdate) {
        traceSelectionCooldown('manual_recovery_settings_changed', batch.batchId, {
          settingsSnapshotChanged: refreshableUpdate.settingsDiff.snapshotChanged,
          changedContextAccountIds: refreshableUpdate.settingsDiff.changedContextAccountIds,
          changedContextFields: refreshableUpdate.settingsDiff.changedContextFields
        })
      }
      if (isAutomaticRecovery) {
        scopeConfirmationToResume = {
          fingerprint: recoveryScopeFingerprint,
          expiresAt: Date.now() + 5_000
        }
      }
      if (!confirmSelectionScope(recoveryScopeFingerprint,
        `即将恢复批次 ${batch.batchId}：\n\n${recoverySummary}\n\n` +
        '本次只执行勾选的待恢复账号；其他账号状态和检查点保持不变。请确认所选账号的登录态、类目和独立下载路径已准备好。'
      )) {
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '已取消启动',
          message: '未发起平台请求，批次和检查点保持不变。', percent: null
        }
        return
      }

      const outOfScope = selectedPendingAccounts.filter((item) => !isSelectionAccountInScope(item.accountId))
      if (outOfScope.length) {
        throw new Error(
          '所选待恢复账号已不在当前数据库账号范围内（' + outOfScope.map((item) => item.accountName).join('、') +
          '）；本次没有恢复或修改批次。'
        )
      }
      const storedValidation = refreshableUpdate ? null : await validateRecoveryContexts(batch)
      const storedContexts = (batch.payload.accountContexts ?? {}) as Record<string, AccountRunContext>
      const preflightTargets = selectedPendingAccounts.map((item) => {
        const currentAccount = currentById.get(item.accountId)
        const context = refreshableUpdate?.freshContexts[String(item.accountId)] ?? storedValidation?.contexts.get(item.accountId) ?? storedContexts[String(item.accountId)]
        return {
          accountId: item.accountId,
          accountName: currentAccount?.name ?? item.accountName,
          profilePath: context?.profilePath,
          resolvedProfilePath: context?.resolvedProfilePath,
          contextError: storedValidation?.errors.get(item.accountId) ?? (!context ? '恢复批次缺少账号浏览器配置快照。' : undefined)
        }
      })
      for (const [accountId, failure] of await collectAccountSwitchFailures(
        preflightTargets, batch.batchId, recoveryScopeFingerprint
      )) {
        accountSwitchFailures.set(accountId, failure)
      }
      if (loginPreflight.value) return
      const existingCandidatePlan = batch.payload.candidatePlan as Record<string, AccountCandidatePlan> | undefined
      const candidatePlanAccounts = selectedPendingAccounts.filter((item) => !accountSwitchFailures.has(item.accountId))
      const selectedPlanMissing = candidatePlanAccounts.some((item) =>
        !existingCandidatePlan || !Object.prototype.hasOwnProperty.call(existingCandidatePlan, String(item.accountId))
      )
      const shouldRefreshCandidatePlan = Boolean(candidatePlanAccounts.length > 0 && refreshableUpdate &&
        (refreshableUpdate.settingsChanged || selectedPlanMissing))
      if (shouldRefreshCandidatePlan && refreshableUpdate) {
        const pendingRows = candidatePlanAccounts.map((item) => {
          const current = accounts.value.find((account) => account.id === item.accountId)
          return { id: item.accountId, name: current?.name ?? item.accountName, status: 'active' }
        })
        const freshContextMap = new Map<number, AccountRunContext>(pendingRows.flatMap((account) => {
          const context = refreshableUpdate.freshContexts[String(account.id)]
          return context ? [[account.id, context] as const] : []
        }))
        let candidatePlans: Map<number, AccountCandidatePlan>
        try {
          candidatePlans = await prepareBatchCandidatePlans(
            pendingRows, freshContextMap, undefined, undefined, accountSwitchFailures
          )
        } catch (cause) {
          if (toUserSelectionError(cause).classification === 'BAIYING_RATE_LIMIT') {
            const reason = String(cause instanceof Error ? cause.message : cause)
            const retryableCandidateAccounts = candidatePlanAccounts.filter((item) => !accountSwitchFailures.has(item.accountId))
            const reportedAccountId = selectionProgress.value?.accountId
            const failedAccountId = retryableCandidateAccounts.find((item) => item.accountId === reportedAccountId)?.accountId ??
              retryableCandidateAccounts[0]?.accountId
            if (failedAccountId !== undefined && retryableCandidateAccounts.length) {
              await saveCandidateFetchRateLimitCheckpoint(
                batch.batchId,
                retryableCandidateAccounts.map((item) => ({ accountId: item.accountId, accountName: item.accountName })),
                failedAccountId,
                reason
              )
            }
          }
          throw cause
        }
        const payload = {
          ...batch.payload,
          ...(refreshableUpdate.settingsChanged ? {
            settingsSnapshot: refreshableUpdate.currentSnapshot,
            accountContexts: refreshableUpdate.freshContexts,
            reviewedAccountId: null,
            reviewedProductIds: null
          } : {}),
          candidatePlan: {
            ...(existingCandidatePlan ?? {}),
            ...Object.fromEntries(candidatePlans)
          }
        }
        const refreshed = await runRecovery([
          'batch', 'refresh-candidates', '--batch-id', batch.batchId
        ], payload)
        batch = refreshed.batch as SelectionRecoveryBatch
        notice.value = refreshableUpdate.settingsChanged
          ? '上次失败发生在候选查询，尚未加车；已按当前设置刷新检查点、候选计划和下载路径。'
          : '旧批次尚未保存统一候选计划；已只读采集候选并写入跨账号互斥计划。'
      }
      snapshot = batch.payload.settingsSnapshot as Record<string, unknown>
      if (!snapshot || typeof snapshot !== 'object') throw new Error('恢复批次缺少设置快照，任务已保留。')
      reviewedAccountId = typeof batch.payload.reviewedAccountId === 'number' ? batch.payload.reviewedAccountId : null
      const reviewedIds = batch.payload.reviewedProductIds
      reviewedProducts = Array.isArray(reviewedIds) ? new Set(reviewedIds.map(String)) : undefined
      const validated = await validateRecoveryContexts(batch)
      contexts = validated.contexts
      contextErrors = validated.errors
      for (const item of selectedPendingAccounts) {
        const context = contexts.get(item.accountId)
        if (context && !contextErrors.has(item.accountId)) {
          const account = accounts.value.find((row) => row.id === item.accountId)
          const label = '恢复账号「' + (account?.name ?? item.accountName) + '」'
          settingsForRecoveryCheckpoint(context.settings, item, label)
        }
      }
      notice.value = '正在恢复批次 ' + batch.batchId + '；本次执行 ' + selectedSummary +
        (preservedSummary ? '；未勾选的 ' + preservedSummary + ' 保持原检查点。' : '；已完成账号会跳过。')
    } else {
      const enabled = accounts.value.filter((account) =>
        account.status === 'active' && isSelectionAccountInScope(account.id)
      )
      if (!enabled.length) throw new Error('没有启用的账号可执行自动选品。')
      snapshot = JSON.parse(JSON.stringify(global)) as Record<string, unknown>
      const prepared = await prepareAccountRunContexts(enabled, snapshot)
      contexts = prepared.contexts
      for (const account of enabled) {
        const resourceError = prepared.errors.get(account.id)
        if (resourceError) contextErrors.set(account.id, resourceError)
      }
      if (contextErrors.size) {
        throw new Error('账号资源隔离校验失败：' + [...contextErrors].map(([id, reason]) =>
          (accounts.value.find((item) => item.id === id)?.name ?? id) + '（' + reason + '）'
        ).join('；'))
      }
      notice.value = '本机选品记录可用；重点类目按百应当前可见商品数据自动推荐。'
      const batchAccountSummary = enabled.map((account) => `${account.name}（ID ${account.id}）`).join('、')
      const batchPathSummary = enabled.map((account) =>
        `${account.name}：${contexts.get(account.id)?.downloadPath ?? '路径未解析'}`
      ).join('\n')
      const newBatchScopeFingerprint = stableJson({
        kind: 'new',
        accounts: enabled.map((account) => {
          const context = contexts.get(account.id)
          return {
            id: account.id,
            profilePath: context?.resolvedProfilePath ?? context?.profilePath ?? '',
            downloadPath: context?.downloadPath ?? ''
          }
        }).sort((left, right) => left.id - right.id),
        settings: snapshot
      })
      if (!confirmSelectionScope(newBatchScopeFingerprint,
        `本批次将依次运行以下全部启用账号：\n\n${batchAccountSummary}\n\n` +
        `素材保存根目录（每账号独立，后续按日期和商品名称分层）：\n${batchPathSummary}\n\n` +
        '请确认账号登录态和保存路径。重点类目将由系统按百应当前可见商品数据自动推荐。'
      )) {
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '已取消启动',
          message: '未创建批次或发起平台请求。', percent: null
        }
        return
      }
      const preflightTargets = enabled.map((account) => {
        const context = contexts.get(account.id)
        return {
          accountId: account.id,
          accountName: account.name,
          profilePath: context?.profilePath,
          resolvedProfilePath: context?.resolvedProfilePath,
          contextError: context ? undefined : '未能解析该账号专属浏览器配置。'
        }
      })
      for (const [accountId, failure] of await collectAccountSwitchFailures(
        preflightTargets, null, newBatchScopeFingerprint
      )) {
        accountSwitchFailures.set(accountId, failure)
      }
      if (loginPreflight.value) return
      reviewedAccountId = candidateSelectionCustomized.value &&
        previewAccountId.value !== null && previewAccountId.value === accountId.value
        ? previewAccountId.value : null
      reviewedProducts = reviewedAccountId !== null
        ? new Set(candidates.value.filter((candidate) => candidate.selected).map((candidate) => candidate.productId))
        : undefined
      const batchId = window.crypto.randomUUID()
      const payload: Record<string, unknown> = {
        settingsSnapshot: snapshot,
        accountContexts: Object.fromEntries([...contexts].map(([id, context]) => [String(id), context])),
        reviewedAccountId,
        reviewedProductIds: reviewedProducts ? [...reviewedProducts] : null,
        candidatePlan: null,
        accounts: enabled.map((account) => ({ id: account.id, name: account.name }))
      }
      let candidatePlans: Map<number, AccountCandidatePlan>
      const candidatePlanAccounts = enabled.filter((account) => !accountSwitchFailures.has(account.id))
      try {
        candidatePlans = candidatePlanAccounts.length
          ? await prepareBatchCandidatePlans(
            candidatePlanAccounts, contexts, reviewedAccountId, reviewedProducts, accountSwitchFailures
          )
          : new Map()
      } catch (cause) {
        if (toUserSelectionError(cause).classification === 'BAIYING_RATE_LIMIT') {
          const reason = String(cause instanceof Error ? cause.message : cause)
          const created = await runRecovery(['batch', 'create', '--batch-id', batchId], payload)
          batch = created.batch as SelectionRecoveryBatch
          const retryableCandidateAccounts = candidatePlanAccounts.filter((account) => !accountSwitchFailures.has(account.id))
          const reportedAccountId = selectionProgress.value?.accountId
          const failedAccountId = retryableCandidateAccounts.find((account) => account.id === reportedAccountId)?.id ??
            retryableCandidateAccounts[0]?.id
          if (failedAccountId !== undefined && retryableCandidateAccounts.length) {
            await saveCandidateFetchRateLimitCheckpoint(
              batchId,
              retryableCandidateAccounts.map((account) => ({ accountId: account.id, accountName: account.name })),
              failedAccountId,
              reason
            )
          }
          for (const [accountId, failure] of accountSwitchFailures) {
            await runRecovery(['account', 'update', '--batch-id', batchId, '--account-id', String(accountId)], {
              state: 'failed', stage: 'account_preflight', error: failure.reason
            })
            await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', String(accountId)], {
              stage: failure.stage, code: failure.classification,
              message: failure.message, detail: failure.detail
            })
          }
        }
        throw cause
      }
      preparedCandidatePlans = Object.fromEntries(candidatePlans)
      payload.candidatePlan = preparedCandidatePlans
      const created = await runRecovery(['batch', 'create', '--batch-id', batchId], payload)
      batch = created.batch as SelectionRecoveryBatch
      recoveryBatch.value = batch
    }

    const persistedAccounts = batch.accounts
    const activeById = new Map(accounts.value.map((account) => [account.id, account]))
    const taskAccounts: AccountRow[] = persistedAccounts
      .filter((item) => recoveryAccountIds === null || recoveryAccountIds.has(item.accountId))
      .map((item) => {
        const current = activeById.get(item.accountId)
        return { id: item.accountId, name: item.accountName, status: current?.status === 'active' ? 'active' : 'disabled' }
      })
    const resumePriorityAccountId = requestedAutoResumeBatchId === batch.batchId
      ? requestedAutoResumeAccountId : null
    const activeTaskAccounts = taskAccounts
      .filter((item) => item.status === 'active')
      .sort((left, right) => {
        if (resumePriorityAccountId !== null) {
          const leftPriority = left.id === resumePriorityAccountId ? 0 : 1
          const rightPriority = right.id === resumePriorityAccountId ? 0 : 1
          if (leftPriority !== rightPriority) return leftPriority - rightPriority
        }
        return left.id - right.id
      })
    const handledAccountIds = new Set<number>()
    selectionProgress.value = {
      ...selectionProgress.value!,
      accountId: null,
      accountName: '',
      accountIndex: 0,
      accountTotal: activeTaskAccounts.length,
      completedAccounts: 0,
      stage: '开始顺序处理账号',
      message: `本轮将${resumePriorityAccountId !== null ? '优先恢复限流账号、再按账号 ID 顺序处理' : '按账号 ID 顺序处理'} ${activeTaskAccounts.length} 个启用账号。`,
      itemIndex: null,
      itemTotal: null,
      percent: null
    }
    for (const item of persistedAccounts) {
      if (item.state === 'completed') accountStatuses[item.accountId] = '完成'
      else if (activeById.get(item.accountId)?.status !== 'active') accountStatuses[item.accountId] = '待恢复（账号未启用）'
    }
    const batchId = batch.batchId
    const batchPayload = batch.payload
    const batchCandidatePlans = (batchPayload.candidatePlan ?? preparedCandidatePlans) as Record<string, AccountCandidatePlan>
    const appendBatchLog = async (entry: SelectionRunLogInput): Promise<boolean> => {
      try {
        await runRecovery([
          'log', 'append', '--batch-id', batchId, '--account-id', '0'
        ], entry)
        return true
      } catch (cause) {
        runLogWarning.value = '选品过程日志写入失败：' + conciseFailureMessage(cause)
        return false
      }
    }
    const resumeMarkerWritten = await appendBatchLog({
      stage: '选品批次',
      code: resumingExistingBatch ? 'INFO_BATCH_RESUMED' : 'INFO_BATCH_STARTED',
      message: resumingExistingBatch
        ? `恢复批次，本轮执行账号：${activeTaskAccounts.map((item) => `${item.name}（ID ${item.id}）`).join('、') || '无'}。`
        : `新建批次，本轮执行账号：${activeTaskAccounts.map((item) => `${item.name}（ID ${item.id}）`).join('、') || '无'}。`,
      detail: requestedAutoResumeBatchId === batchId
        ? `resumeTrigger=cooldown_auto; rateLimitedAccountId=${requestedAutoResumeAccountId ?? ''}`
        : 'resumeTrigger=manual_or_other'
    })
    if (requestedAutoResumeBatchId === batchId) {
      const intent = cooldownAutoResumeIntent.value
      if (intent?.batchId === batchId && intent.accountId === requestedAutoResumeAccountId) {
        completedCooldownAutoResume = { batchId, accountId: intent.accountId, deadline: intent.deadline }
        cooldownAutoResumeIntent.value = null
        autoResumeAccountId.value = null
      }
      traceSelectionCooldown('renderer_auto_resume_batch_resumed', batchId, {
        rateLimitedAccountId: requestedAutoResumeAccountId,
        resumeTrigger: 'cooldown_auto', resumeMarkerWritten
      })
    }
    const reviewedIds = batchPayload.reviewedProductIds
    const persistedReviewedProducts = Array.isArray(reviewedIds) ? new Set(reviewedIds.map(String)) : reviewedProducts
    const persistedReviewedAccountId = typeof batchPayload.reviewedAccountId === 'number'
      ? batchPayload.reviewedAccountId : reviewedAccountId

    const outcomes = await runSequentialSelectionBatch(
      taskAccounts,
      async (account) => {
        const task = persistedAccounts.find((item) => item.accountId === account.id)
        if (!task) throw new Error('恢复批次中找不到该账号任务。')
        if (task.state === 'completed') return { status: '完成' as const, message: '已完成，跳过重复执行' }
        const persist = async (patch: Record<string, unknown>): Promise<void> => {
          const saved = await runRecovery([
            'account', 'update', '--batch-id', batchId, '--account-id', String(account.id)
          ], patch)
          Object.assign(task, saved.account as SelectionRecoveryAccount)
        }
        const appendAccountLog = async (entry: SelectionRunLogInput): Promise<void> => {
          try {
            await runRecovery([
              'log', 'append', '--batch-id', batchId, '--account-id', String(account.id)
            ], entry)
          } catch (cause) {
            runLogWarning.value = '选品过程日志写入失败：' + conciseFailureMessage(cause)
          }
        }
        const logFailure = appendAccountLog
        const switchFailure = accountSwitchFailures.get(account.id)
        if (switchFailure) {
          await logFailure({
            stage: switchFailure.stage,
            code: switchFailure.classification,
            message: switchFailure.message,
            detail: switchFailure.detail
          })
          await persist({ state: 'failed', stage: 'account_preflight', error: switchFailure.reason })
          throw new Error(switchFailure.reason)
        }
        const contextError = contextErrors.get(account.id)
        if (contextError) {
          await logFailure({
            stage: '账号资源校验', code: 'ACCOUNT_CONTEXT_INVALID',
            message: '账号资源隔离校验失败：' + conciseFailureMessage(contextError), detail: contextError
          })
          await persist({ state: 'needs_retry', stage: task.stage || 'profile_check', error: contextError })
          throw new Error('账号资源隔离校验失败：' + contextError)
        }
        const context = contexts.get(account.id)
        if (!context) {
          const reason = '恢复批次缺少此账号的有效资源配置。'
          await logFailure({ stage: '账号资源校验', code: 'ACCOUNT_CONTEXT_MISSING', message: reason, detail: reason })
          await persist({ state: 'needs_retry', stage: task.stage || 'profile_check', error: reason })
          throw new Error(reason)
        }
        try {
          await appendAccountLog({
            stage: '账号选品', code: 'INFO_ACCOUNT_STARTED',
            message: `开始处理账号「${account.name}」。`
          })
          await persist({ state: 'in_progress', error: '' })
          const outcome = await executeAccountSelection(
            account.id,
            false,
            batchPayload.settingsSnapshot as Record<string, unknown>,
            account.id === persistedReviewedAccountId ? persistedReviewedProducts : undefined,
            context,
            { batchId, state: task, persist, logFailure, logEvent: appendAccountLog },
            persistedReviewedAccountId,
            batchCandidatePlans[String(account.id)]
          )
          await appendAccountLog({
            stage: '账号选品',
            code: outcome.status === '完成' ? 'SUCCESS_ACCOUNT_COMPLETED' : 'WARNING_ACCOUNT_PARTIAL',
            message: `账号处理${outcome.status}。${outcome.message ? conciseFailureMessage(outcome.message) : ''}`,
            detail: outcome.message ?? ''
          })
          return outcome
        } catch (cause) {
          const reason = String(cause instanceof Error ? cause.message : cause)
          if (task.state !== 'failed' && task.state !== 'needs_retry') {
            await logFailure({
              stage: task.stage || '账号任务', code: 'ACCOUNT_TASK_FAILED',
              message: '账号任务失败：' + conciseFailureMessage(reason), detail: reason
            })
            await persist({ state: 'failed', stage: task.stage || 'account_run', error: reason })
          }
          throw cause
        }
      },
      (account, status) => {
        accountStatuses[account.id] = status
        const accountIndex = activeTaskAccounts.findIndex((item) => item.id === account.id) + 1
        if (['完成', '部分完成', '失败'].includes(status)) handledAccountIds.add(account.id)
        const isCurrent = status === '执行中' || status === '失败' || status === '完成' || status === '部分完成'
        const candidatePlanReady = Boolean(batchCandidatePlans[String(account.id)])
        const previousAccountStage = selectionProgress.value?.accountId === account.id
          ? selectionProgress.value.stage : ''
        const taskStage = persistedAccounts.find((item) => item.accountId === account.id)?.stage ?? ''
        const displayedStage = status === '失败'
          ? `账号处理失败：${previousAccountStage || taskStage || '账号任务'}`
          : status === '排队中' ? '等待账号执行'
            : status === '执行中' ? '账号选品执行中' : `账号处理${status}`
        const stagePercent = ['完成', '部分完成'].includes(status) || (candidatePlanReady && ['排队中', '执行中'].includes(status))
          ? 100 : null
        selectionProgress.value = {
          ...selectionProgress.value!,
          state: 'running',
          accountId: account.id,
          accountName: account.name,
          accountIndex,
          accountTotal: activeTaskAccounts.length,
          completedAccounts: handledAccountIds.size,
          stage: displayedStage,
          message: status === '排队中'
            ? `账号 ${accountIndex}/${activeTaskAccounts.length} 已进入等待队列。`
            : status === '未执行'
              ? '前序账号未完成，本账号未发起平台请求；检查点已保留。'
              : isCurrent ? `账号 ${accountIndex}/${activeTaskAccounts.length}：${status}` : '',
          itemIndex: null,
          itemTotal: null,
          percent: stagePercent
        }
      },
      resumePriorityAccountId
    )
    const finalBatchState = getSelectionBatchFinalState(persistedAccounts)
    const allComplete = finalBatchState === 'completed'
    const terminalFailed = finalBatchState === 'failed'
    await runRecovery(['batch', 'update', '--batch-id', batchId, '--state', finalBatchState])
    const succeeded = outcomes.filter((item) => item.status === '完成').length
    const partial = outcomes.filter((item) => item.status === '部分完成').length
    const failed = outcomes.filter((item) => item.status === '失败')
    const skipped = outcomes.filter((item) => item.status === '未执行').length
    const waiting = persistedAccounts.filter((item) => item.state !== 'completed').length
    await appendBatchLog({
      stage: '批次结果',
      code: allComplete ? 'SUCCESS_BATCH_COMPLETED' : terminalFailed ? 'ERROR_BATCH_FAILED' : 'WARNING_BATCH_NEEDS_RETRY',
      message: `本轮结束：完成 ${succeeded} 个账号，部分完成 ${partial} 个，失败 ${failed.length} 个，未执行 ${skipped} 个；${allComplete ? '批次已完成。' : terminalFailed ? '本批次已失败结束，原因已记录。' : `还有 ${waiting} 个账号待恢复。`}`
    })
    notice.value = (allComplete ? '批次完成。' : terminalFailed ? '本批次失败结束，原因已记录，可修正后启动新批次。' : '批次已保存，尚有 ' + waiting + ' 个账号任务待恢复。') +
      '本批次账号 ' + outcomes.length + ' 个：完成 ' + succeeded +
      ' 个，部分完成 ' + partial + ' 个，失败 ' + failed.length + ' 个，未执行 ' + skipped + ' 个。' +
      outcomes.map((item) => item.accountName + '：' + item.status).join('；')
    const rateLimitedAccount = persistedAccounts.find((item) =>
      item.state === 'needs_retry' &&
      toUserSelectionError(item.error).classification === 'BAIYING_RATE_LIMIT'
    )
    const accountSwitchFailure = persistedAccounts.find((item) =>
      item.state === 'failed' && [
        'ACCOUNT_SWITCH_CAPTCHA', 'ACCOUNT_SWITCH_LOGIN', 'ACCOUNT_SWITCH_DISCONNECT', 'LOGIN_OR_PAGE_STATE_UNCONFIRMED',
        'SEARCHBOX_ZERO_EXHAUSTED', 'SEARCHBOX_CONTROL_EXHAUSTED'
      ].includes(String(toUserSelectionError(item.error).classification ?? ''))
    )
    const filterApplyFailure = persistedAccounts.find((item) =>
      item.state === 'failed' && toUserSelectionError(item.error).classification === 'CATEGORY_FILTER_APPLY_FAILED'
    )
    if (rateLimitedAccount) {
      error.value = rateLimitedAccount.error
    } else if (accountSwitchFailure) {
      error.value = accountSwitchFailure.error
    } else if (filterApplyFailure) {
      error.value = filterApplyFailure.error
    } else if (partial || failed.length) {
      error.value = '失败和跳过原因已按批次、账号和商品记录；请展开下方“选品过程日志”查看处理结果与技术细节。'
    }
    selectionProgress.value = {
      ...selectionProgress.value!,
      state: 'finished',
      accountId: null,
      accountName: '',
      accountIndex: activeTaskAccounts.length,
      accountTotal: activeTaskAccounts.length,
      completedAccounts: outcomes.filter((item) => item.status !== '未执行').length,
      stage: allComplete ? '选品批次处理完成' : terminalFailed ? '选品批次失败结束' : '本轮处理结束，部分账号待恢复',
      message: `已处理 ${outcomes.filter((item) => item.status !== '未执行').length}/${activeTaskAccounts.length} 个账号；完成 ${succeeded}，部分完成 ${partial}，失败 ${failed.length}，未执行 ${skipped}。${terminalFailed ? '本批次已结束，无待恢复任务。' : ''}`,
      itemIndex: null,
      itemTotal: null,
      percent: allComplete ? 100 : null
    }
    const latestAfterRun = await runRecovery(['batch', 'latest'])
    if (latestAfterRun.batch) {
      recoveryBatch.value = latestAfterRun.batch as SelectionRecoveryBatch
    } else {
      const recentAfterRun = await runRecovery(['batch', 'recent'])
      recoveryBatch.value = recentAfterRun.batch as SelectionRecoveryBatch | null
    }
    restoreSelectionCooldown(recoveryBatch.value)
    if (runLogWarning.value) notice.value += '；' + runLogWarning.value
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
    const lastProgress = selectionProgress.value
    selectionProgress.value = {
      ...lastProgress!,
      state: 'failed',
      stage: lastProgress?.stage ? `失败于：${lastProgress.stage}` : '批次准备或执行失败',
      message: error.value,
      stepIndex: lastProgress?.stepIndex ?? null,
      stepTotal: lastProgress?.stepTotal ?? null,
      stepUnit: lastProgress?.stepUnit ?? '',
      itemIndex: lastProgress?.itemIndex ?? null,
      itemTotal: lastProgress?.itemTotal ?? null,
      percent: lastProgress?.percent ?? null
    }
  } finally {
    const pendingAutoResume = cooldownAutoResumeIntent.value
    if (requestedAutoResumeBatchId && pendingAutoResume?.batchId === requestedAutoResumeBatchId &&
      pendingAutoResume.accountId === requestedAutoResumeAccountId && pendingAutoResume.phase === 'starting') {
      if (loginPreflight.value) {
        cooldownAutoResumeIntent.value = { ...pendingAutoResume, phase: 'blocked' }
        traceSelectionCooldown('renderer_auto_resume_blocked', requestedAutoResumeBatchId, {
          accountId: requestedAutoResumeAccountId, reason: 'manual_login_or_verification_required'
        })
      } else {
        const attempts = pendingAutoResume.attempts + 1
        if (attempts <= MAX_COOLDOWN_AUTO_RESUME_START_RETRIES) {
          const delayMs = Math.min(8_000, 1_000 * (2 ** (attempts - 1)))
          const retryAt = Date.now() + delayMs
          cooldownAutoResumeIntent.value = {
            ...pendingAutoResume, attempts, phase: 'pending', deadline: retryAt, retryAt,
            awaitingExpiryAck: false
          }
          autoResumeAccountId.value = pendingAutoResume.accountId
          armSelectionCooldown(Math.ceil(delayMs / 1000), retryAt)
          traceSelectionCooldown('renderer_auto_resume_retry_scheduled', requestedAutoResumeBatchId, {
            accountId: pendingAutoResume.accountId, attempts, maxAttempts: MAX_COOLDOWN_AUTO_RESUME_START_RETRIES,
            retryAt, delayMs
          })
        } else {
          cooldownAutoResumeIntent.value = { ...pendingAutoResume, attempts, phase: 'blocked' }
          const message = '冷却自动续选多次未能进入批次执行；待恢复检查点仍保留，请查看日志中的启动失败原因。'
          notice.value = message
          notifyGlobal(message, 'error')
          traceSelectionCooldown('renderer_auto_resume_retry_exhausted', requestedAutoResumeBatchId, {
            accountId: pendingAutoResume.accountId, attempts, maxAttempts: MAX_COOLDOWN_AUTO_RESUME_START_RETRIES,
            error: error.value || selectionProgress.value?.message || ''
          })
        }
      }
    }
    busy.value = false
  }
}

const runSelectionBatchOnce = createSingleFlight(runSelectionBatch)

async function startSelection(): Promise<void> {
  const autoStartBatchId = automaticResumeBatchId
  const autoStartAccountId = automaticResumeAccountId
  const intent = cooldownAutoResumeIntent.value
  const automaticStartMatchesIntent = Boolean(autoStartBatchId && autoStartAccountId !== null && intent &&
    intent.batchId === autoStartBatchId && intent.accountId === autoStartAccountId && intent.phase === 'starting')
  const automaticResumeIsDue = Boolean(automaticStartMatchesIntent && intent &&
    Date.now() >= Math.max(intent.deadline, intent.retryAt ?? 0))
  const deferAutomaticResume = (reason: string): void => {
    if (!automaticStartMatchesIntent || !intent) return
    cooldownAutoResumeIntent.value = { ...intent, phase: 'pending' }
    automaticResumeBatchId = null
    automaticResumeAccountId = null
    traceSelectionCooldown('renderer_auto_resume_deferred', intent.batchId, {
      accountId: intent.accountId, reason, busy: busy.value,
      cooldownRemaining: selectionCooldownRemaining.value,
      deadline: intent.deadline, retryAt: intent.retryAt ?? null
    })
  }
  if (busy.value) {
    deferAutomaticResume('busy_before_start_selection')
    return
  }
  if (selectionCooldownRemaining.value > 0 && !automaticResumeIsDue) {
    deferAutomaticResume('cooldown_countdown_not_ready')
    return
  }
  if (automaticResumeIsDue) selectionCooldownRemaining.value = 0
  void window.api.selectionCooldown.cancel().catch(() => undefined)
  clearAutoResumeTimer()
  selectionCooldownArmed.value = false
  selectionCooldownDeadline = null
  if (automaticResumeBatchId === null) {
    autoResumeAccountId.value = null
    cooldownAutoResumeIntent.value = null
  }
  if (selectionCooldownTimer !== null) window.clearInterval(selectionCooldownTimer)
  selectionCooldownTimer = null
  void runSelectionBatchOnce()
}

watch(accountId, () => {
  candidates.value = []
  categoryRecommendations.value = []
  previewAccountId.value = null
  candidateSelectionCustomized.value = false
  if (activeTab.value === '账号个性化') void loadAccountSettings()
})
watch(activeTab, (tab) => {
  if (tab === '账号个性化') void loadAccountSettings()
})

onMounted(async () => {
  selectionViewUnmounted = false
  if (!props.dialogOnly) stopSelectionCooldownExpired = window.api.selectionCooldown.onExpired(onSelectionCooldownExpired)
  window.addEventListener('focus', refreshCooldownFromDeadline)
  document.addEventListener('visibilitychange', refreshCooldownFromDeadline)
  stopSelectionProgress = window.api.browser.onSelectionProgress((event) => {
    const current = selectionProgress.value
    if (!current || current.state !== 'running') return
    if (current.accountId !== null && current.accountId !== event.accountId) return
    const productProgress = Boolean(event.productId) || event.stage === '核验并处理选中商品' || event.stage === '选中商品处理结果已返回'
    const itemPercent = productProgress && Number.isInteger(event.index) && Number.isInteger(event.total) && (event.total ?? 0) > 0
      ? Math.floor(Math.max(0, Math.min(event.total ?? 0, (event.index ?? 0) - 1)) * 100 / (event.total ?? 1))
      : null
    reportSelectionProgress(event.accountId, event.stage, {
      state: 'running',
      message: event.message ?? '',
      stepIndex: productProgress ? null : event.index ?? null,
      stepTotal: productProgress ? null : event.total ?? null,
      stepUnit: productProgress ? '' : event.unit ?? '',
      itemIndex: productProgress ? event.index ?? null : null,
      itemTotal: productProgress ? event.total ?? null : null,
      percent: event.percent ?? itemPercent,
      indeterminate: event.indeterminate === true
    })
  })
  stopAutoPublishProgress = window.api.publish.onProgress((event) => {
    const current = selectionProgress.value
    if (!current || current.state !== 'running' || current.accountId !== event.accountId ||
      activeAutoPublishQueueId === null || activeAutoPublishQueueId !== event.queueId) return
    reportSelectionProgress(event.accountId ?? current.accountId!, event.stage, {
      state: 'running',
      message: event.message || event.stage,
      itemIndex: current.itemIndex,
      itemTotal: current.itemTotal,
      percent: typeof event.percent === 'number' ? event.percent : null,
      indeterminate: event.indeterminate === true
    })
  })
  busy.value = true
  try {
    defaultDownloadRoot.value = await window.api.dir.getDownloadDir()
    await Promise.all([loadGlobal(), loadAccounts()])
    await loadAccountSettings()
    await Promise.all([reloadCatalog(), loadCachedFilterCatalog()])
    await loadRecoveryStatus()
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    busy.value = false
  }
})

onUnmounted(() => {
  selectionViewUnmounted = true
  stopSelectionProgress?.()
  stopAutoPublishProgress?.()
  stopSelectionCooldownExpired?.()
  window.removeEventListener('focus', refreshCooldownFromDeadline)
  document.removeEventListener('visibilitychange', refreshCooldownFromDeadline)
  if (selectionCooldownTimer !== null) window.clearInterval(selectionCooldownTimer)
  selectionCooldownTimer = null
  clearAutoResumeTimer()
})

</script>

<template>
  <section v-if="props.dialogOnly" class="account-settings-mask" @click.self="closeDialog">
    <div class="account-settings-dialog" role="dialog" aria-modal="true" :aria-label="`选品设置 — ${accounts.find((item) => item.id === accountId)?.name ?? '账号'}`">
      <header class="account-settings-head">
        <div>
          <h2>选品设置 — {{ accounts.find((item) => item.id === accountId)?.name ?? '账号' }}</h2>
          <p>可以沿用通用类目策略、自动推荐或手动指定类目；此处还可调整筛选条件、路径和每日限额。</p>
        </div>
        <div class="account-settings-head-actions">
          <button class="btn" :disabled="busy || accountId === null" @click="openAccountBaiying">打开百应</button>
          <button class="btn" :disabled="busy || accountId === null" @click="refreshAccountCategories">刷新类目</button>
          <button class="btn" :disabled="busy" aria-label="关闭选品设置" @click="closeDialog">×</button>
        </div>
      </header>
      <div class="account-settings-body">
        <p v-if="notice" class="notice">{{ selectionNoticePresentation.userMessage }}</p>
        <details v-if="notice && selectionNoticePresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionNoticePresentation.diagnostic }}</pre>
        </details>
        <p v-if="error" class="error">{{ selectionErrorPresentation.userMessage }}</p>
        <details v-if="error && selectionErrorPresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionErrorPresentation.diagnostic }}</pre>
        </details>
        <p class="hint">账号可沿用通用模式，或单独切换到品类模式；品类模式保留原来的自动推荐和手动类目筛选。</p>
        <section class="card">
          <div class="section-title">
            <h2>类目选择</h2>
            <span class="muted">账号可单独覆盖通用设置</span>
          </div>
          <div class="category-mode-options" role="group" aria-label="账号选品模式">
            <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode" value="inherit" :disabled="busy"> 沿用通用模式</label>
            <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode" value="general" :disabled="busy"> 通用数据模式</label>
            <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode" value="category" :disabled="busy"> 品类模式</label>
          </div>
          <p v-if="accountDraft.selectionMode === 'inherit'" class="hint">当前通用模式：{{ global.selectionMode === 'category' ? '品类模式' : '通用数据模式（默认全部类目）' }}。</p>
          <template v-if="accountDraft.selectionMode === 'category' || (accountDraft.selectionMode === 'inherit' && global.selectionMode === 'category')">
          <div class="category-mode-options" role="group" aria-label="账号品类筛选策略">
            <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode" value="inherit" :disabled="busy"> 沿用通用设置</label>
            <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode" value="auto" :disabled="busy"> 自动推荐</label>
            <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode" value="manual" :disabled="busy"> 手动选择类目</label>
          </div>
          <p v-if="accountDraft.categoryMode === 'inherit'" class="hint">
            当前沿用通用设置：{{ global.selectionMode !== 'category' ? '自动推荐（通用数据模式未启用品类筛选）' : global.categoryMode === 'manual' ? '手动筛选 ' + selectedCategoryCount(global.categorySelection) + ' 个类目' : '按百应当前可见高分商品自动推荐重点类目' }}。
          </p>
          <p v-else-if="accountDraft.categoryMode === 'auto'" class="hint">系统只分析热点趋势、联盟榜单等当前可见商品，并自动推荐重点类目。</p>
          <template v-else>
            <p class="hint">勾选的一级、二级或三级类目均可作为范围；程序只筛当前可见商品，不会按类目搜索或翻页。已选 {{ selectedCategoryCount(accountDraft.categorySelection) }} 项。</p>
          <div class="category-grid">
            <div v-for="(group, index) in [level1Options, accountLevel2Options, accountLevel3Options]" :key="index" class="category-level">
              <label>{{ ['一级类目', '二级类目', '三级类目'][index] }}</label>
              <div class="category-options">
                <label v-for="item in group" :key="item.id" class="category-option">
                  <input
                    type="checkbox"
                    :checked="accountDraft.categorySelection[(['level1', 'level2', 'level3'][index] as CategoryLevel)].includes(item.id)"
                    :disabled="busy"
                    @change="toggleCategory(item.id, (['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', ($event.target as HTMLInputElement).checked)"
                  >
                  <span>{{ item.name }}</span>
                </label>
                <div v-if="!group.length" class="category-empty">{{ index === 0 ? '暂无本地类目；登录百应后刷新' : '请先选择上一级类目' }}</div>
              </div>
              <div class="mini-actions">
                <button :disabled="busy || !group.length" @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'all')">全选</button>
                <button :disabled="busy || !group.length" @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'invert')">反选</button>
                <button :disabled="busy" @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'clear')">清空</button>
              </div>
            </div>
          </div>
          </template>
          </template>
        </section>
        <section class="card">
          <h2>本账号每日配额</h2>
          <label class="check-row account-filter-toggle">
            <input v-model="accountDraft.dailyQuotaOverride.enabled" type="checkbox" :disabled="busy">
            单独设置此账号的每日限额
          </label>
          <div class="form-grid">
            <label>每日商品数上限<input v-model.number="accountDraft.dailyQuotaOverride.perAccountProducts" type="number" min="1" :disabled="busy || !accountDraft.dailyQuotaOverride.enabled"></label>
            <label>每日视频数上限<input v-model.number="accountDraft.dailyQuotaOverride.perAccountVideos" type="number" min="1" :disabled="busy || !accountDraft.dailyQuotaOverride.enabled"></label>
          </div>
          <p class="hint">关闭时继承通用默认值（{{ global.quota.perAccountProducts }} 个商品 / {{ global.quota.perAccountVideos }} 条视频）。只影响后续选品和新建计划；已有计划仍计入对应日期用量。限额是客户端调度设置，不代表平台承诺的处理能力。</p>
        </section>
        <section class="card">
          <label class="check-row account-filter-toggle">
            <input type="checkbox" :checked="accountFilterOverridesEnabled" :disabled="busy" @change="setAccountFilterOverrides(($event.target as HTMLInputElement).checked)">
            为此账号单独配置选品筛选条件
          </label>
          <p class="hint">关闭时继承通用筛选条件。屏蔽词始终全局生效，执行设置保持通用。</p>
          <SelectionFilterEditor
            v-if="accountFilterOverridesEnabled"
            v-model="accountFilterDraft"
            title="账号选品条件"
            :filter-groups="filterGroups"
            :feature-options="featureOptions"
            :filter-catalog-updated-at="filterCatalogUpdatedAt"
            :disabled="busy"
            :show-sync="true"
            :syncing="busy"
            @sync="refreshFilterCatalog"
          />
          <p v-else class="account-inherit-state">当前账号继承通用筛选设置。</p>
        </section>
        <section class="card">
          <h2>账号路径</h2>
          <label class="path-field">浏览器缓存路径<div><input v-model="accountDraft.browserProfilePath" :disabled="busy" placeholder="留空使用此账号的独立 profile"><button class="btn" :disabled="busy" @click="chooseProfilePath">选择文件夹</button><button class="btn" :disabled="busy" @click="accountDraft.browserProfilePath = ''">恢复默认</button></div></label>
          <label class="path-field">账号下载路径<div><input v-model="accountDraft.downloadPath" :disabled="busy" placeholder="留空使用此账号的独立下载目录"><button class="btn" :disabled="busy" @click="chooseDownloadPath">选择文件夹</button><button class="btn" :disabled="busy" @click="accountDraft.downloadPath = ''">恢复默认</button></div></label>
          <p v-if="accountPathsHaveUnsavedChanges" class="hint warning">路径有未保存修改；新批次仍会使用上次保存的目录。点击下方“保存”后才生效；已有素材不会移动，已开始处理的恢复批次沿用原路径。</p>
          <p class="hint warning">百应、抖音授权与账号自动操作共用该账号 profile。自定义目录必须包含 Default；路径变更后先保存，再刷新类目或打开授权。账号下载目录与其他账号隔离。</p>
        </section>
      </div>
      <footer class="account-settings-foot">
        <button class="btn" :disabled="busy" @click="closeDialog">取消</button>
        <button class="btn primary" :disabled="busy || accountId === null" @click="saveAccount">{{ busy ? '保存中…' : '保存' }}</button>
      </footer>
    </div>
  </section>

  <section v-else class="selection-page">
    <header class="page-head">
      <div>
        <h1>选品设置与自动选品</h1>
        <p>从百应当前可见热点、联盟来源自动选品，并按高分商品数和平均分推荐重点类目；路径、筛选条件和限额仍可按账号设置。</p>
      </div>
      <div class="head-actions">
        <button class="btn" :disabled="busy" @click="previewCandidates">预览候选</button>
        <button class="btn primary" :disabled="busy || selectionCooldownRemaining > 0 || (recoveryBatch?.state === 'needs_retry' && selectedRecoveryAccounts.length === 0)" @click="startSelection">
          {{ selectionCooldownRemaining > 0 ? `冷却中（${selectionCooldownRemaining} 秒）` : recoveryBatch?.state === 'needs_retry' ? '继续选品' : '自动选品一起工作' }}
        </button>
      </div>
    </header>
    <div v-if="accounts.length" class="account-statuses"><b>账号选品状态</b><span v-for="account in accounts" :key="account.id">{{ account.name }}：{{ accountStatuses[account.id] }}</span></div>
    <section v-if="selectionCooldownArmed" class="selection-cooldown" role="status" aria-live="polite">
      <strong>百应暂时限流</strong>
      <p v-if="selectionCooldownRemaining > 0 && autoResumeAccountId !== null">已暂停后续百应请求，约 {{ selectionCooldownRemaining }} 秒后将从本机保存的检查点自动继续选品。</p>
      <p v-else-if="selectionCooldownRemaining > 0">百应请求暂缓约 {{ selectionCooldownRemaining }} 秒；当前没有可自动恢复的限流检查点。</p>
      <p v-else-if="autoResumeAccountId !== null">冷却已结束，正在从保存的检查点自动继续；也可手动点击“继续选品”。</p>
      <p v-else>冷却已结束；当前没有可自动恢复的限流检查点，可在确认页面后重新选品。</p>
    </section>
    <section v-if="!dialogOnly" class="card registry-config-card">
      <div class="registry-config-summary">
        <div class="registry-config-heading">
          <h2>本机选品登记</h2>
          <span class="registry-online">单机模式</span>
        </div>
      </div>
      <p class="registry-config-note">商品去重和账号已选类目记录保存在这台电脑上，选品时自动使用，无需填写服务器地址或密钥。跨电脑同步暂未启用。</p>
    </section>
    <section v-if="selectionProgress" class="selection-progress-card" :class="selectionProgress.state" aria-live="polite">
      <div class="selection-progress-head">
        <strong>{{ selectionProgress.mode === 'preview' ? '候选预览进度' : '自动选品进度' }}</strong>
        <span v-if="selectionProgress.accountId !== null">
          已处理 {{ selectionProgress.completedAccounts }}/{{ selectionProgress.accountTotal }} 个账号 · 当前 {{ selectionProgress.accountName }}（{{ selectionProgress.accountIndex }}/{{ selectionProgress.accountTotal }}）
        </span>
        <span v-else-if="selectionProgress.accountTotal > 0">
          已处理账号 {{ selectionProgress.completedAccounts }}/{{ selectionProgress.accountTotal }}
        </span>
      </div>
      <div class="selection-progress-overall">
        <strong>{{ selectionWorkflowPercent }}%</strong>
        <span>{{ selectionProgress.stage }}</span>
      </div>
      <div class="selection-progress-track" role="progressbar"
        :aria-valuenow="selectionWorkflowPercent" :aria-valuemin="0" :aria-valuemax="100"
        :aria-valuetext="`当前账号流程 ${selectionWorkflowPercent}%`">
        <span class="selection-progress-fill" :style="{ width: `${selectionWorkflowPercent}%` }"></span>
      </div>
      <div class="selection-progress-meta">
        <strong v-if="selectionProgress.itemTotal !== null">当前商品 {{ selectionProgress.itemIndex ?? 0 }}/{{ selectionProgress.itemTotal }}</strong>
        <span v-else>阶段 {{ selectionWorkflowStageIndex + 1 }}/{{ selectionWorkflowSteps.length }}</span>
      </div>
      <details class="selection-progress-details">
        <summary>查看完整流程与详情（{{ selectionWorkflowSteps.length }} 个阶段）</summary>
        <ol class="selection-progress-steps" aria-label="选品完整流程">
          <li v-for="(step, index) in selectionWorkflowStepStates" :key="step.key" :class="step.status">
            <span class="selection-progress-step-marker">{{ step.status === 'completed' ? '✓' : index + 1 }}</span>
            <strong>{{ step.label }}</strong>
            <span class="selection-progress-step-status">{{ step.statusText }}</span>
          </li>
        </ol>
        <div class="selection-progress-meta">
          <strong v-if="selectionWorkflowStagePercent !== null">本阶段 {{ selectionWorkflowStagePercent }}%</strong>
          <span v-else-if="selectionProgress.state === 'running'">当前阶段执行中，平台暂未提供精确比例</span>
          <span v-if="selectionProgress.stepTotal !== null && selectionProgress.stepTotal !== undefined">
            {{ selectionProgress.stepUnit === '秒' ? '已等待' : '阶段计数' }}
            {{ selectionProgress.stepIndex ?? 0 }}/{{ selectionProgress.stepTotal }}{{ selectionProgress.stepUnit ? ` ${selectionProgress.stepUnit}` : '' }}
          </span>
          <span v-if="selectionProgress.itemTotal !== null">当前商品 {{ selectionProgress.itemIndex ?? 0 }}/{{ selectionProgress.itemTotal }}</span>
        </div>
        <small>流程百分比按阶段权重估算；商品计数和可用的阶段比例来自实时回报。</small>
        <p v-if="selectionProgress.message" class="selection-progress-message">{{ selectionProgressPresentation.userMessage }}</p>
        <details v-if="selectionProgress.message && selectionProgressPresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionProgressPresentation.diagnostic }}</pre>
        </details>
      </details>
    </section>

    <div class="tabs">
      <button :class="{ active: activeTab === '通用设置' }" @click="activeTab = '通用设置'">通用设置</button>
      <button :class="{ active: activeTab === '账号个性化' }" @click="activeTab = '账号个性化'">账号个性化选品</button>
      <button v-if="activeTab === '账号个性化'" class="account-state" disabled>当前账号：{{ accounts.find((item) => item.id === accountId)?.name ?? '未选择' }}</button>
    </div>

    <div class="body-layout">
      <div class="settings-scroll">
        <p v-if="notice" class="notice">{{ selectionNoticePresentation.userMessage }}</p>
        <details v-if="notice && selectionNoticePresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionNoticePresentation.diagnostic }}</pre>
        </details>
        <p v-if="error" class="error">{{ selectionErrorPresentation.userMessage }}</p>
        <details v-if="error && selectionErrorPresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionErrorPresentation.diagnostic }}</pre>
        </details>
        <section v-if="recoveryBatch?.state === 'needs_retry'" class="recovery-card">
          <div>
            <strong>有待恢复批次：{{ recoveryBatch.batchId }}</strong>
            <p>本次只恢复勾选账号；未勾选账号保留原检查点和队列状态，已完成账号自动跳过。</p>
            <div class="recovery-account-list">
              <label v-for="account in pendingRecoveryAccounts" :key="account.accountId" class="recovery-account-option">
                <input
                  type="checkbox"
                  :checked="recoverySelectedAccountIds.includes(account.accountId)"
                  :disabled="busy"
                  @change="setRecoveryAccountSelected(account.accountId, ($event.target as HTMLInputElement).checked)"
                >
                <span>{{ account.accountName }}（ID {{ account.accountId }}）</span>
                <small>{{ recoveryAccountStageLabel(account) }}</small>
              </label>
              <span
                v-for="account in completedRecoveryAccounts"
                :key="account.accountId"
                class="recovery-account-completed"
              >{{ account.accountName }}（ID {{ account.accountId }}）：已完成，跳过</span>
            </div>
            <div class="recovery-account-actions">
              <button class="btn" :disabled="busy || pendingRecoveryAccounts.length === 0" @click="selectAllPendingRecoveryAccounts">全选待恢复账号</button>
              <button class="btn" :disabled="busy || recoverySelectedAccountIds.length === 0" @click="clearPendingRecoveryAccountSelection">清除勾选</button>
              <span class="hint">已选 {{ selectedRecoveryAccounts.length }} / {{ pendingRecoveryAccounts.length }} 个待恢复账号</span>
            </div>
          </div>
          <button class="btn" :disabled="busy" @click="abandonStaleBatch">结束旧批次并保留检查点</button>
        </section>
        <section v-if="!dialogOnly" class="card failure-log-card">
          <div class="section-title">
            <h2>选品过程日志</h2>
            <button class="btn" type="button" @click="emit('navigate', '选品日志')">查看选品日志</button>
          </div>
          <div v-if="recoveryBatch" class="run-log-summary">
            <strong>最近批次 {{ recoveryBatch.batchId }}</strong>
            <span>{{ visibleRunLogs.length }} 条记录</span>
            <span class="log-count-info">进度 {{ visibleRunLogCounts.info }}</span>
            <span class="log-count-success">成功 {{ visibleRunLogCounts.success }}</span>
            <span class="log-count-warning">跳过 / 提醒 {{ visibleRunLogCounts.warning }}</span>
            <span class="log-count-error">失败 {{ visibleRunLogCounts.error }}</span>
            <span class="hint">{{ ['in_progress', 'needs_retry'].includes(recoveryBatch.state) ? '批次处理中或待恢复' : '最近批次已结束' }} · 日志保存在本机</span>
          </div>
          <p v-else class="hint">每次选品批次和商品处理记录都会保存在本机，可按账号、状态和日期查看历史详情。</p>
        </section>

        <template v-if="activeTab === '通用设置'">
          <section class="card">
            <div class="section-title"><h2>选品模式</h2><button class="btn" @click="browserVisible = !browserVisible">{{ browserVisible ? '隐藏百应登录入口' : '显示百应登录入口' }}</button></div>
            <p class="hint">默认从选品广场、热点趋势和联盟榜单采集候选，按统一数据门槛筛选。类目可以作为可选筛选条件。</p>
            <div class="category-mode-options" role="group" aria-label="通用选品模式">
              <label><input v-model="global.selectionMode" type="radio" name="global-selection-mode" value="general" :disabled="busy"> 通用数据模式（默认全部类目）</label>
              <label><input v-model="global.selectionMode" type="radio" name="global-selection-mode" value="category" :disabled="busy"> 品类模式</label>
            </div>
            <p v-if="global.selectionMode === 'general'" class="hint">通用数据模式不因类目未识别或类目未匹配而排除商品；同一商品 ID 仍会在所有账号间互斥。</p>
            <template v-if="global.selectionMode === 'category'">
            <div class="category-mode-options" role="group" aria-label="通用类目策略">
              <label><input v-model="global.categoryMode" type="radio" name="global-category-mode" value="auto" :disabled="busy"> 自动推荐（默认）</label>
              <label><input v-model="global.categoryMode" type="radio" name="global-category-mode" value="manual" :disabled="busy"> 手动选择类目</label>
            </div>
            <p v-if="global.categoryMode === 'auto'" class="hint">系统只分析百应当前可见商品，按高分商品数量和平均分推荐重点末级类目并自动加车。</p>
            <template v-else>
              <p class="hint">勾选的一级、二级或三级类目均可作为范围。手动范围只筛当前可见商品，不会按类目搜索或翻页。已选 {{ selectedCategoryCount(global.categorySelection) }} 项。</p>
            <div class="category-grid">
              <div v-for="(group, index) in [level1Options, level2Options, level3Options]" :key="index" class="category-level">
                <label>{{ ['一级类目', '二级类目', '三级类目'][index] }}</label>
                <div class="category-options">
                  <label v-for="item in group" :key="item.id" class="category-option">
                    <input
                      type="checkbox"
                      :checked="global.categorySelection[(['level1', 'level2', 'level3'][index] as CategoryLevel)].includes(item.id)"
                      :disabled="busy"
                      @change="toggleCategory(item.id, (['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', ($event.target as HTMLInputElement).checked)"
                    >
                    <span>{{ item.name }}</span>
                  </label>
                  <div v-if="!group.length" class="category-empty">{{ index === 0 ? '登录百应后等待类目捕获' : '请先选择上一级类目' }}</div>
                </div>
                <div class="mini-actions">
                  <button :disabled="busy || !group.length" @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', 'all')">全选</button>
                  <button :disabled="busy || !group.length" @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', 'invert')">反选</button>
                  <button :disabled="busy" @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', 'clear')">清空</button>
                </div>
              </div>
            </div>
            </template>
            <p class="hint">品类模式沿用现有自动推荐或手动限定逻辑；账号历史用过的末级类目会强制排除。跨账号商品 ID 始终全局互斥。</p>
            </template>
          </section>

          <section class="card">
            <div class="section-title">
              <h2>通用下载目录</h2>
              <button class="btn" :disabled="busy" @click="chooseGlobalDownloadPath">选择下载目录</button>
            </div>
            <label class="path-field global-download-path">当前通用根目录
              <input :value="defaultDownloadRoot || '正在读取…'" readonly>
            </label>
            <p class="hint">选择后立即保存。账号未设置单独路径时，素材保存到此目录下的「账号-ID」子目录，再按日期和商品名称分层；账号个性化中的自定义下载路径优先。已有素材不会移动；未获取候选的检查点可按新设置继续，已开始处理的批次沿用原路径。</p>
          </section>

          <section class="card">
            <SelectionFilterEditor
              v-model="globalFilterSettings"
              title="选品条件"
              :filter-groups="filterGroups"
              :feature-options="featureOptions"
              :filter-catalog-updated-at="filterCatalogUpdatedAt"
              :show-sync="true"
              :syncing="busy"
              @sync="refreshFilterCatalog"
            />
          </section>

          <div class="banned-words-trigger">
            <button class="btn" type="button" :disabled="busy" @click="bannedWordsDialogOpen = true">屏蔽词</button>
          </div>

          <section class="card">
            <h2>执行设置与每日配额</h2>
            <div class="form-grid">
              <label>发布方式<select value="全自动发布（单项异常隔离；继续后续任务）" disabled><option>全自动发布（单项异常隔离；继续后续任务）</option></select></label>
              <label>置顶发布时间<input v-model="global.execution.pinTime" type="time"></label>
              <label>随机间隔下限（分钟）<input v-model.number="global.execution.randomIntervalMin" type="number" min="0"></label>
              <label>随机间隔上限（分钟）<input v-model.number="global.execution.randomIntervalMax" type="number" min="0"></label>
              <label>通用默认每品视频上限<input v-model.number="global.quota.perProductVideos" type="number" min="1"></label>
              <label>通用默认每账号商品上限<input v-model.number="global.quota.perAccountProducts" type="number" min="1"></label>
              <label>通用默认每账号视频上限<input v-model.number="global.quota.perAccountVideos" type="number" min="1"></label>
            </div>
            <p class="hint">账号未单独设置时使用这些默认值。限额只控制客户端选品和发布计划，不代表平台吞吐承诺；待确认和已排期任务会占用计划日期用量。</p>
          </section>
          <div class="save-row"><button class="btn primary" :disabled="busy" @click="saveGlobal">保存通用设置</button></div>
        </template>

        <template v-else>
          <section class="card">
            <div class="section-title"><h2>账号与品类</h2><select v-model.number="accountId" class="account-select" :disabled="busy"><option v-for="account in selectionScopeAccounts" :key="account.id" :value="account.id">{{ account.name }}（{{ account.status }}）</option></select></div>
            <p v-if="!accounts.length" class="hint">请先在账号管理中添加账号。</p>
            <p class="hint">账号可继承通用数据/品类模式，也可单独切换；通用数据模式默认覆盖全部类目。</p>
            <div class="category-mode-options" role="group" aria-label="账号选品模式">
              <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode-settings" value="inherit" :disabled="busy"> 沿用通用模式</label>
              <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode-settings" value="general" :disabled="busy"> 通用数据模式</label>
              <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode-settings" value="category" :disabled="busy"> 品类模式</label>
            </div>
            <p v-if="accountDraft.selectionMode === 'inherit'" class="hint">当前通用模式：{{ global.selectionMode === 'category' ? '品类模式' : '通用数据模式（全部类目）' }}。</p>
            <template v-if="accountDraft.selectionMode === 'category' || (accountDraft.selectionMode === 'inherit' && global.selectionMode === 'category')">
            <div class="category-mode-options" role="group" aria-label="账号品类筛选策略">
              <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode-settings" value="inherit" :disabled="busy"> 沿用通用设置</label>
              <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode-settings" value="auto" :disabled="busy"> 自动推荐</label>
              <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode-settings" value="manual" :disabled="busy"> 手动选择类目</label>
            </div>
            <p v-if="accountDraft.categoryMode === 'inherit'" class="hint">
              当前沿用通用设置：{{ global.selectionMode !== 'category' ? '自动推荐（通用数据模式未启用品类筛选）' : global.categoryMode === 'manual' ? '手动筛选 ' + selectedCategoryCount(global.categorySelection) + ' 个类目' : '按百应当前可见高分商品自动推荐重点类目' }}。
            </p>
            <p v-else-if="accountDraft.categoryMode === 'auto'" class="hint">此账号会按百应当前可见高分商品自动推荐重点类目。</p>
            <template v-else>
              <p class="hint">勾选的一级、二级或三级类目均可作为范围；只筛当前可见商品，不会按类目搜索或翻页。已选 {{ selectedCategoryCount(accountDraft.categorySelection) }} 项。</p>
            <div class="category-grid">
              <div v-for="(group, index) in [level1Options, accountLevel2Options, accountLevel3Options]" :key="index" class="category-level">
                <label>{{ ['一级类目', '二级类目', '三级类目'][index] }}</label>
                <div class="category-options">
                  <label v-for="item in group" :key="item.id" class="category-option">
                    <input
                      type="checkbox"
                      :checked="accountDraft.categorySelection[(['level1', 'level2', 'level3'][index] as CategoryLevel)].includes(item.id)"
                      @change="toggleCategory(item.id, (['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', ($event.target as HTMLInputElement).checked)"
                    >
                    <span>{{ item.name }}</span>
                  </label>
                  <div v-if="!group.length" class="category-empty">{{ index === 0 ? '登录百应后等待类目捕获' : '请先选择上一级类目' }}</div>
                </div>
                <div class="mini-actions"><button @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'all')">全选</button><button @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'invert')">反选</button><button @click="categoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'clear')">清空</button></div>
              </div>
            </div>
            </template>
            </template>
          </section>
          <section class="card">
            <h2>本账号每日配额</h2>
            <label class="check-row account-filter-toggle">
              <input v-model="accountDraft.dailyQuotaOverride.enabled" type="checkbox" :disabled="busy">
              单独设置此账号的每日限额
            </label>
            <div class="form-grid">
              <label>每日商品数上限<input v-model.number="accountDraft.dailyQuotaOverride.perAccountProducts" type="number" min="1" :disabled="busy || !accountDraft.dailyQuotaOverride.enabled"></label>
              <label>每日视频数上限<input v-model.number="accountDraft.dailyQuotaOverride.perAccountVideos" type="number" min="1" :disabled="busy || !accountDraft.dailyQuotaOverride.enabled"></label>
            </div>
            <p class="hint">关闭时继承通用默认值（{{ global.quota.perAccountProducts }} 个商品 / {{ global.quota.perAccountVideos }} 条视频）。只影响后续选品和新建计划；已有计划仍计入对应日期用量。限额是客户端调度设置，不代表平台承诺的处理能力。</p>
          </section>
          <section class="card">
            <label class="check-row account-filter-toggle">
              <input type="checkbox" :checked="accountFilterOverridesEnabled" :disabled="busy" @change="setAccountFilterOverrides(($event.target as HTMLInputElement).checked)">
              为此账号单独配置选品筛选条件
            </label>
            <p class="hint">关闭时继承通用筛选条件。屏蔽词始终全局生效，执行设置保持通用。</p>
            <SelectionFilterEditor
              v-if="accountFilterOverridesEnabled"
              v-model="accountFilterDraft"
              title="账号选品条件"
              :filter-groups="filterGroups"
              :feature-options="featureOptions"
              :filter-catalog-updated-at="filterCatalogUpdatedAt"
              :disabled="busy"
              :show-sync="true"
              :syncing="busy"
              @sync="refreshFilterCatalog"
            />
            <p v-else class="account-inherit-state">当前账号继承通用筛选设置。</p>
          </section>
          <section class="card">
            <h2>账号路径</h2>
            <label class="path-field">托管 Chrome profile 路径（目录须包含 Default）<div><input v-model="accountDraft.browserProfilePath" placeholder="留空使用客户端专用账号 profile"><button class="btn" @click="chooseProfilePath">选择文件夹</button><button class="btn" @click="accountDraft.browserProfilePath = ''">恢复默认</button></div></label>
            <label class="path-field">账号下载路径<div><input v-model="accountDraft.downloadPath" placeholder="留空使用账号独立默认目录"><button class="btn" @click="chooseDownloadPath">选择文件夹</button><button class="btn" @click="accountDraft.downloadPath = ''">恢复默认</button></div></label>
            <p v-if="accountPathsHaveUnsavedChanges" class="hint warning">路径有未保存修改；新批次仍会使用上次保存的目录。点击“保存账号设置”后才生效；已有素材不会移动，已开始处理的恢复批次沿用原路径。</p>
            <p class="hint warning">留空时客户端会在 D 盘创建独立 profile，首次使用需在新开的 Chrome 中登录。Chrome 136+ 不允许对默认用户资料目录开放 CDP；若选择已有 profile，请确保它不是 Chrome 默认目录且没有被其他 Chrome 窗口占用。</p>
          </section>
          <div class="save-row"><button class="btn primary" :disabled="busy || accountId === null" @click="saveAccount">保存账号设置</button><button class="btn" :disabled="busy" @click="resetAccountPaths">恢复两个路径默认值</button></div>
        </template>

        <section v-if="candidates.length" class="card candidate-card">
          <h2>百应候选商品（{{ candidates.length }}）</h2>
          <p class="hint">候选来自百应热点趋势、联盟榜单等当前可见列表。系统按数据分自动排重点类目并自动加车；此处仅展示候选和原因，不需要手动勾选商品。</p>
          <div v-if="categoryRecommendations.length" class="category-recommendations">
            <strong>系统重点类目推荐</strong>
            <span v-for="item in categoryRecommendations.slice(0, 3)" :key="item.categoryId" class="category-recommendation">
              {{ categoryName(item.categoryId) }}（{{ item.categoryId }}） · 高分商品 {{ item.highScoreCount }} 个 · 平均分 {{ item.averageScore }}
            </span>
            <details v-if="categoryRecommendations.length > 3" class="category-recommendation-more">
              <summary>查看其余 {{ categoryRecommendations.length - 3 }} 个推荐类目</summary>
              <span v-for="item in categoryRecommendations.slice(3)" :key="item.categoryId" class="category-recommendation">
                {{ categoryName(item.categoryId) }}（{{ item.categoryId }}） · 高分商品 {{ item.highScoreCount }} 个 · 平均分 {{ item.averageScore }}
              </span>
            </details>
          </div>
          <label v-for="candidate in candidates" :key="candidate.productId" class="candidate-row">
            <input type="checkbox" :checked="candidate.selected" disabled>
            <span class="candidate-main"><b>{{ candidate.title }}</b><small>商品 ID：{{ candidate.productId }} · 月销：{{ candidate.monthSales ?? '平台未提供' }} · 佣金：{{ candidate.commissionRate ?? '平台未提供' }}% · 体验分：{{ candidate.dataMetrics?.experienceScore ?? candidate.shopScore ?? '平台未提供' }} · {{ candidate.trendRising ? '走势上升' : '走势未确认上升' }}</small><small v-if="candidate.detailCapturedAt || candidate.analysisScore !== undefined || candidate.unifiedScore !== undefined">统一分：{{ candidate.unifiedScore ?? candidate.analysisScore ?? '暂无可评分实测数据' }}/100<span v-if="candidate.hotOrRankBoost">（热点/榜单加权 +{{ candidate.hotOrRankBoost }}）</span> · 数据覆盖 {{ candidate.analysisCoverage ?? 0 }}/{{ candidate.analysisMetricCount ?? 0 }} 项 · {{ candidate.analysisStatus ?? '已分析' }}<span v-if="candidate.status"> · {{ candidate.status }}</span></small><details v-if="candidate.analysisReasons?.length" class="candidate-analysis" @click.stop><summary>查看数据分析依据</summary><small>{{ candidate.analysisReasons.join('；') }}</small></details><small v-if="candidate.sources?.length" class="source-badges">来源命中：{{ candidateSourceLabels(candidate) }}</small><small v-if="candidate.categoryStatus === '类目未知'" class="warning">类目未知；通用数据模式可按数据门槛参与，品类推荐不计入</small><small v-if="candidate.publicationLock" class="warning">{{ productPublishLockLabel(candidate.publicationLock) }}</small></span>
          </label>
        </section>
      </div>

      <aside v-if="browserVisible" class="browser-panel">
        <BrowserPane default-target="baiying" :profile-key="browserProfileKey" :profile-path="accountDraft.browserProfilePath || undefined" />
      </aside>
    </div>
    <Teleport to="body">
      <section
        v-if="bannedWordsDialogOpen"
        class="banned-words-mask"
        @click.self="bannedWordsDialogOpen = false"
        @keydown.esc.window.stop="bannedWordsDialogOpen = false"
      >
        <div class="banned-words-dialog" role="dialog" aria-modal="true" aria-labelledby="banned-words-title">
          <header class="banned-words-head">
            <h2 id="banned-words-title">屏蔽词</h2>
            <button class="btn" type="button" aria-label="关闭屏蔽词" @click="bannedWordsDialogOpen = false">×</button>
          </header>
          <p class="hint">商品名称包含任一词即排除；每行一个词，可增删。屏蔽词全局生效。</p>
          <p class="hint">原型中的达人、账号和店铺名称已加入默认词表。当前按名称包含匹配；「小明、与辉、朱朱、亮亮、安安」等短词可能误排正常商品，可在列表中删除。</p>
          <textarea v-model="bannedWordsText" class="ban-words" spellcheck="false" aria-label="全局屏蔽词列表" :disabled="busy"></textarea>
          <section class="banned-words-suggestions" aria-label="商城推荐标题候选词">
            <div class="banned-words-suggestion-head">
              <strong>可选的商城标题过滤词</strong>
              <button class="btn" type="button" :disabled="busy" @click="appendOptionalMallTitleBannedWords">加入未添加项</button>
            </div>
            <p class="hint">参考抖音商城推荐标题规范：{{ OPTIONAL_MALL_TITLE_BANNED_WORDS.join('、') }}。这些词不默认屏蔽，也不代表平台禁售；加入后会按商品名称包含规则全局排除，编辑完成仍需保存通用设置。</p>
          </section>
          <footer class="banned-words-foot">
            <span class="hint">关闭后仍需点击页面底部「保存通用设置」使修改生效。</span>
            <button class="btn primary" type="button" @click="bannedWordsDialogOpen = false">完成</button>
          </footer>
        </div>
      </section>
    </Teleport>
    <Teleport to="body">
      <section
        v-if="loginPreflight"
        class="login-preflight-mask"
        @click.self="!loginPreflight.checking && (loginPreflight = null)"
        @keydown.esc.window.stop="!loginPreflight.checking && (loginPreflight = null)"
      >
        <div class="login-preflight-dialog" role="dialog" aria-modal="true" aria-labelledby="login-preflight-title">
          <header class="login-preflight-head">
            <div>
              <h2 id="login-preflight-title">选品前请确认账号登录</h2>
              <p>每个执行账号都需要分别登录百应和抖音创作者中心。两站任一未确认时，不会创建或恢复选品批次。</p>
            </div>
            <button class="btn" type="button" aria-label="关闭登录检查" :disabled="loginPreflight.checking" @click="loginPreflight = null">×</button>
          </header>
          <p v-if="loginPreflight.checking" class="login-preflight-progress">正在逐个检查账号的两站登录状态，请稍候。</p>
          <p v-else-if="loginPreflightReady(loginPreflight.accounts)" class="login-preflight-success">每个账号的百应和创作者中心均已自动或手动确认。点击「继续选品」后将进入选品流程。</p>
          <p v-else class="login-preflight-warning">尚未创建或修改批次。请先打开未登录的平台完成登录/验证，再点击「重新检查登录状态」；显示“需要登录”或“需要人工验证”时必须重新检查通过，不能跳过。</p>
          <div class="login-preflight-list">
            <article v-for="item in loginPreflight.accounts" :key="item.accountId" class="login-preflight-account">
              <strong>{{ item.accountName }}（ID {{ item.accountId }}）</strong>
              <div class="login-preflight-platform">
                <span>
                  <b>百应</b>
                  <em :class="['login-state', item.manualBaiyingConfirmed ? 'logged_in' : item.baiying.state]">{{ item.manualBaiyingConfirmed ? '已手动确认' : loginStateLabel(item.baiying.state) }}</em>
                  <small>{{ item.baiying.message }}</small>
                </span>
                <div class="login-preflight-actions">
                  <button class="btn" type="button" :disabled="loginPreflight.checking || openingLoginTarget !== null" @click="openPreflightLogin(item, 'baiying')">
                    {{ openingLoginTarget === `${item.accountId}:baiying` ? '正在打开…' : '打开百应登录' }}
                  </button>
                  <button
                    v-if="item.baiying.state === 'unknown'"
                    class="btn mini"
                    type="button"
                    :disabled="loginPreflight.checking || !!item.contextError || !(item.resolvedProfilePath || item.profilePath)"
                    @click="toggleManualLoginConfirmation(item, 'baiying')"
                  >{{ item.manualBaiyingConfirmed ? '已确认（撤销）' : '我已确认登录' }}</button>
                </div>
              </div>
              <div class="login-preflight-platform">
                <span>
                  <b>抖音创作者中心</b>
                  <em :class="['login-state', item.manualCreatorCenterConfirmed ? 'logged_in' : item.creatorCenter.state]">{{ item.manualCreatorCenterConfirmed ? '已手动确认' : loginStateLabel(item.creatorCenter.state) }}</em>
                  <small>{{ item.creatorCenter.message }}</small>
                </span>
                <div class="login-preflight-actions">
                  <button class="btn" type="button" :disabled="loginPreflight.checking || openingLoginTarget !== null" @click="openPreflightLogin(item, 'creatorCenter')">
                    {{ openingLoginTarget === `${item.accountId}:creatorCenter` ? '正在打开…' : '打开创作者中心登录' }}
                  </button>
                  <button
                    v-if="item.creatorCenter.state === 'unknown'"
                    class="btn mini"
                    type="button"
                    :disabled="loginPreflight.checking || !!item.contextError || !(item.resolvedProfilePath || item.profilePath)"
                    @click="toggleManualLoginConfirmation(item, 'creatorCenter')"
                  >{{ item.manualCreatorCenterConfirmed ? '已确认（撤销）' : '我已确认登录' }}</button>
                </div>
              </div>
            </article>
          </div>
          <footer class="login-preflight-foot">
            <button class="btn" type="button" :disabled="loginPreflight.checking" @click="recheckSelectionLogins">
              {{ loginPreflight.checking ? '正在检查…' : '重新检查登录状态' }}
            </button>
            <button
              v-if="loginPreflightReady(loginPreflight.accounts)"
              class="btn primary"
              type="button"
              :disabled="loginPreflight.checking || busy"
              @click="continueAfterLoginPreflight"
            >继续选品</button>
            <button v-else class="btn" type="button" :disabled="loginPreflight.checking" @click="loginPreflight = null">稍后处理</button>
          </footer>
        </div>
      </section>
    </Teleport>
  </section>
</template>

<style scoped>
.account-settings-mask { position: fixed; inset: 0; z-index: 1200; display: grid; place-items: center; padding: 24px; background: rgb(15 23 42 / 46%); }
.account-settings-dialog { display: flex; flex-direction: column; width: min(1120px, 96vw); max-height: 92vh; overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-panel); box-shadow: 0 18px 56px rgb(15 23 42 / 24%); }
.account-settings-head, .account-settings-foot { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 15px 18px; border-bottom: 1px solid var(--border); }
.account-settings-head h2 { margin: 0; font-size: 18px; }
.account-settings-head p { margin: 4px 0 0; color: var(--text-muted); font-size: 12px; }
.account-settings-head-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.account-settings-body { min-height: 0; overflow: auto; padding: 14px 18px; }
.account-settings-body > .card { margin-top: 10px; }
.account-settings-foot { justify-content: flex-end; border-top: 1px solid var(--border); border-bottom: 0; }
.account-settings-dialog .category-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.account-filter-toggle { margin-bottom: 7px; color: var(--text); font-size: 13px; }
.account-inherit-state { margin: 10px 0 0; padding: 9px 11px; border-radius: 6px; background: var(--bg-placeholder); color: var(--text-muted); font-size: 12px; }
.account-settings-dialog .filter-editor { padding-top: 12px; border-top: 1px solid var(--border); }
.account-settings-dialog .category-options { max-height: 210px; overflow-y: auto; }
.account-settings-dialog .path-field > div { display: flex; gap: 7px; }
.account-settings-dialog .path-field input { flex: 1; min-width: 120px; }
.account-settings-dialog .path-field button { white-space: nowrap; }
.selection-page { display: flex; flex-direction: column; width: 100%; min-width: 0; min-height: 0; gap: 10px; color: var(--text); }
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.page-head h1 { margin: 0; font-size: 20px; }
.page-head p, .hint { margin: 5px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.55; }
.tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--border); }
.tabs button { padding: 8px 12px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-muted); }
.tabs button.active { border-bottom-color: var(--brand); color: var(--brand); }
.tabs .account-state { margin-left: auto; cursor: default; }
.account-statuses { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; color: var(--text-muted); font-size: 12px; }
.global-download-path { margin-top: 10px; }
.selection-progress-card { padding: 11px 13px; border: 1px solid #93c5fd; border-radius: var(--radius); background: #eff6ff; color: #1e3a8a; }
.selection-progress-card.finished { border-color: #86efac; background: #f0fdf4; color: #166534; }
.selection-progress-card.failed { border-color: #fecaca; background: #fef2f2; color: #991b1b; }
.selection-cooldown { margin: 10px 0; padding: 11px 13px; border: 1px solid #f0c36d; border-radius: var(--radius); background: #fff8e8; color: #704b00; }
.selection-cooldown p { margin: 5px 0 0; font-size: 12px; }
.selection-diagnostic { margin: 4px 0 8px; color: var(--text-muted); font-size: 11px; }
.selection-diagnostic summary { cursor: pointer; }
.selection-diagnostic pre { max-height: 180px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.selection-progress-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.selection-progress-head span, .selection-progress-card small, .selection-progress-message { color: var(--text-muted); font-size: 11px; }
.selection-progress-overall { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-top: 8px; }
.selection-progress-overall strong { flex: 0 0 auto; font-size: 24px; line-height: 1; }
.selection-progress-overall span { min-width: 0; overflow: hidden; color: var(--text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.selection-progress-track { position: relative; height: 7px; margin-top: 8px; overflow: hidden; border-radius: 999px; background: rgba(59, 130, 246, .16); }
.selection-progress-fill { display: block; height: 100%; border-radius: inherit; background: #3478f6; transition: width .2s ease; }
.selection-progress-card.failed .selection-progress-fill { background: #dc2626; }
.selection-progress-indeterminate { position: absolute; inset: 0 auto 0 0; width: 35%; border-radius: inherit; background: #3478f6; animation: selection-progress-slide 1.2s ease-in-out infinite alternate; }
.selection-progress-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 5px 8px; margin: 9px 0 0; padding: 0; list-style: none; }
.selection-progress-steps li { display: flex; align-items: center; min-width: 0; gap: 7px; padding: 5px 7px; border: 1px solid rgba(100, 116, 139, .16); border-radius: 6px; color: var(--text-muted); background: rgba(255, 255, 255, .48); font-size: 11px; }
.selection-progress-steps li.running { border-color: #93c5fd; color: #1d4ed8; background: rgba(219, 234, 254, .7); }
.selection-progress-steps li.completed { color: #15803d; }
.selection-progress-steps li.failed { border-color: #fca5a5; color: #b91c1c; background: rgba(254, 226, 226, .6); }
.selection-progress-step-marker { display: inline-flex; flex: 0 0 19px; width: 19px; height: 19px; align-items: center; justify-content: center; border-radius: 50%; background: rgba(100, 116, 139, .12); font-size: 10px; font-weight: 700; }
.selection-progress-steps li.running .selection-progress-step-marker { background: #dbeafe; }
.selection-progress-steps li.completed .selection-progress-step-marker { background: #dcfce7; }
.selection-progress-steps li.failed .selection-progress-step-marker { background: #fee2e2; }
.selection-progress-steps li strong { min-width: 0; font-weight: 600; }
.selection-progress-step-status { margin-left: auto; flex: 0 0 auto; font-size: 10px; opacity: .8; }
.selection-progress-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 12px; margin-top: 6px; color: var(--text-muted); font-size: 11px; }
.selection-progress-meta strong { color: inherit; font-size: 13px; }
.selection-progress-message { margin: 6px 0 0; line-height: 1.45; }
.selection-progress-details { margin-top: 7px; color: var(--text-muted); font-size: 11px; }
.selection-progress-details > summary, .category-recommendation-more > summary { width: fit-content; cursor: pointer; }
.selection-progress-details[open] > summary { margin-bottom: 4px; }
@keyframes selection-progress-slide { from { transform: translateX(-20%); } to { transform: translateX(210%); } }
.body-layout { display: flex; flex: 1; min-height: 0; gap: 10px; }
.settings-scroll { flex: 1; min-width: 0; overflow: auto; padding: 1px 4px 12px 1px; }
.browser-panel { width: 43%; min-width: 420px; min-height: 0; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.card { margin-bottom: 10px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.card h2 { margin: 0 0 10px; font-size: 15px; }
.section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.category-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.category-mode-options { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 10px; }
.category-mode-options label { display: inline-flex; align-items: center; gap: 6px; color: var(--text); font-size: 13px; cursor: pointer; }
.category-mode-options input { margin: 0; accent-color: var(--brand); }
.category-level { min-width: 0; }
.category-plan { display: flex; flex-direction: column; gap: 5px; margin-top: 10px; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius); font-size: 12px; }
.category-plan.conflict { border-color: #d98b26; background: #fff8ed; }
.category-plan strong { color: var(--text); }
.category-plan p { margin: 0; line-height: 1.55; }
.category-plan-error { color: #9b5410; }
.category-plan details { color: var(--text-muted); }
.category-plan summary { cursor: pointer; }
.category-plan ol { max-height: 220px; margin: 6px 0 0; padding-left: 24px; overflow: auto; line-height: 1.55; }
.category-level > label, .form-grid label, .path-field { display: flex; flex-direction: column; gap: 5px; color: var(--text-muted); font-size: 12px; }
.category-options { width: 100%; height: 132px; box-sizing: border-box; overflow: auto; padding: 5px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); }
.category-option { display: flex !important; flex-direction: row !important; align-items: center; gap: 7px !important; min-height: 24px; padding: 2px 4px; border-radius: 4px; color: var(--text) !important; cursor: pointer; }
.category-option:hover { background: var(--brand-soft); }
.category-option input { margin: 0; }
.category-empty { padding: 8px 4px; color: var(--text-muted); font-size: 12px; }
.mini-actions { display: flex; gap: 4px; margin-top: 5px; }
.mini-actions button, .pool-list button { padding: 3px 7px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-panel); color: var(--text); font-size: 11px; }
.registry-config-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.registry-config-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.registry-config-heading h2 { margin: 0; font-size: 14px; }
.registry-online, .registry-offline { display: inline-flex; align-items: center; min-height: 20px; padding: 1px 8px; border-radius: 10px; font-size: 11px; }
.registry-online { background: #e7f6ee; color: #087443; }
.registry-offline { background: var(--bg-placeholder); color: var(--text-muted); }
.registry-config-note { margin: 6px 0 0; color: var(--text-muted); font-size: 11px; }
.registry-config-fields { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }
.registry-config-fields .form-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 0; }
.registry-config-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
.registry-config-detail { min-width: 0; color: var(--text-muted); font-size: 11px; overflow-wrap: anywhere; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
.form-grid input, .form-grid select, .path-field input, .account-select { width: 100%; height: 32px; box-sizing: border-box; padding: 0 8px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); color: var(--text); }
.form-grid.textareas { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid textarea { min-height: 92px; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); color: var(--text); font: 12px/1.45 Consolas, monospace; }
.platform-filter-section { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border); }
.platform-filter-section h3 { margin: 0 0 8px; font-size: 13px; }
.platform-filter-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.platform-filter-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; color: var(--text-muted); font-size: 12px; }
.platform-filter-field > select { width: 100%; height: 32px; box-sizing: border-box; padding: 0 8px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); color: var(--text); }
.platform-filter-multi, .platform-filter-chips { display: flex; flex-wrap: wrap; gap: 5px 10px; }
.platform-filter-multi .check-row { margin: 0; font-size: 12px; }
.filter-chip { display: inline-flex; align-items: center; gap: 5px; padding: 5px 9px; border: 1px solid var(--border); border-radius: 16px; color: var(--text); cursor: pointer; }
.filter-chip:has(input:checked) { border-color: var(--brand); background: var(--brand-soft); }
.candidate-row { display: flex; align-items: flex-start; gap: 9px; padding: 9px 2px; border-top: 1px solid var(--border); cursor: pointer; }
.candidate-row > input { margin-top: 3px; }
.candidate-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.candidate-main b { overflow-wrap: anywhere; color: var(--text); font-size: 13px; }
.candidate-main small { color: var(--text-muted); font-size: 11px; }
.candidate-analysis { color: var(--text-muted); font-size: 11px; }
.candidate-analysis summary { cursor: pointer; }
.candidate-analysis small { display: block; margin-top: 4px; line-height: 1.5; }
.check-row { display: flex; align-items: center; gap: 7px; margin-top: 10px; font-size: 13px; }
.pool-list { margin: 0; padding-left: 22px; }
.pool-list li { display: flex; justify-content: space-between; max-width: 420px; padding: 5px 0; }
.pool-list li span:last-child { display: flex; gap: 4px; }
.ban-words { width: 100%; min-height: 120px; margin-top: 8px; padding: 8px; box-sizing: border-box; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); color: var(--text); font: 12px/1.5 Consolas, monospace; }
.banned-words-trigger { display: flex; justify-content: flex-start; }
.banned-words-mask { position: fixed; inset: 0; z-index: 1300; display: grid; place-items: center; padding: 24px; background: rgb(15 23 42 / 46%); }
.banned-words-dialog { display: flex; flex-direction: column; width: min(680px, 94vw); max-height: 88vh; overflow: auto; padding: 18px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-panel); box-shadow: 0 18px 56px rgb(15 23 42 / 24%); }
.banned-words-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.banned-words-head h2 { margin: 0; font-size: 18px; }
.banned-words-dialog .ban-words { height: 48vh; min-height: 220px; max-height: 58vh; margin-top: 10px; resize: vertical; }
.banned-words-suggestions { margin-top: 12px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-placeholder); }
.banned-words-suggestion-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.banned-words-suggestions p { margin: 7px 0 0; }
.banned-words-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; }
.login-preflight-mask { position: fixed; inset: 0; z-index: 1400; display: grid; place-items: center; padding: 22px; background: rgb(15 23 42 / 48%); }
.login-preflight-dialog { display: flex; flex-direction: column; width: min(760px, 96vw); max-height: 88vh; overflow: hidden; padding: 18px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-panel); box-shadow: 0 18px 56px rgb(15 23 42 / 28%); }
.login-preflight-head, .login-preflight-foot { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.login-preflight-head h2 { margin: 0; font-size: 18px; }
.login-preflight-head p, .login-preflight-warning, .login-preflight-progress, .login-preflight-success { margin: 6px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }
.login-preflight-warning { padding: 9px 11px; border-radius: 6px; background: #fff7ed; color: #9a3412; }
.login-preflight-progress { padding: 9px 11px; border-radius: 6px; background: var(--brand-soft); }
.login-preflight-success { padding: 9px 11px; border-radius: 6px; background: #f0fdf4; color: #166534; }
.login-preflight-list { min-height: 0; overflow: auto; margin-top: 10px; }
.login-preflight-account { margin-top: 8px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 7px; }
.login-preflight-platform { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
.login-preflight-actions { display: flex; flex: 0 0 auto; align-items: center; justify-content: flex-end; gap: 6px; }
.login-preflight-platform > span { display: grid; grid-template-columns: auto auto; align-items: center; justify-content: start; gap: 3px 8px; min-width: 0; }
.login-preflight-platform small { grid-column: 1 / -1; color: var(--text-muted); font-size: 11px; line-height: 1.4; }
.login-state { padding: 2px 7px; border-radius: 999px; font-size: 11px; font-style: normal; }
.login-state.logged_in { background: #dcfce7; color: #166534; }
.login-state.login_required, .login-state.verification_required { background: #fee2e2; color: #991b1b; }
.login-state.unknown, .login-state.checking { background: #f1f5f9; color: #475569; }
.login-preflight-foot { align-items: center; justify-content: flex-end; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }
.path-field { margin-top: 12px; }
.path-field div { display: flex; gap: 6px; }
.path-field input { flex: 1; min-width: 0; }
.warning { color: #a56a00; }
.source-badges { color: var(--brand); }
.category-recommendations { display: flex; flex-direction: column; gap: 5px; margin: 8px 0 12px; padding: 10px 12px; border-radius: var(--radius); background: var(--surface-muted); }
.category-recommendation-more { display: flex; flex-direction: column; gap: 5px; }
.category-recommendation { font-size: 12px; color: var(--text-muted); }
.save-row { display: flex; gap: 8px; padding-bottom: 10px; }
.btn { min-height: 30px; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); color: var(--text); }
.btn.primary { border-color: var(--brand); background: var(--brand); color: #fff; }
.btn:disabled { opacity: .5; }
.notice, .error { padding: 8px 10px; border-radius: var(--radius); font-size: 12px; }
.notice { background: var(--brand-soft); color: var(--text); }
.error { background: #fdecea; color: #b73b2f; }
.recovery-card { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin: 8px 0; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.recovery-card p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.recovery-account-list { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
.recovery-account-option, .recovery-account-completed { display: inline-flex; align-items: center; gap: 6px; min-height: 30px; padding: 4px 8px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); color: var(--text); font-size: 12px; }
.recovery-account-option input { margin: 0; }
.recovery-account-option small { color: var(--text-muted); }
.recovery-account-completed { color: var(--text-muted); }
.recovery-account-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 9px; }
.failure-log-card { margin-bottom: 10px; }
.run-log-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 4px 0 8px; color: var(--text-muted); font-size: 12px; }
.run-log-summary strong { color: var(--text); }
.log-count-success { color: #087443; }
.log-count-warning { color: #9a6700; }
.log-count-error { color: #a33125; }
.run-log-history-note { margin: 0 0 8px; }
.failure-log-entry { padding: 10px 0; border-top: 1px solid var(--border); }
.failure-log-meta { display: flex; justify-content: space-between; gap: 12px; color: var(--text-muted); font-size: 11px; }
.failure-log-meta strong { min-width: 0; overflow-wrap: anywhere; color: var(--text); font-size: 12px; }
.run-log-action { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 6px; color: var(--text-muted); font-size: 11px; }
.run-log-action strong { color: var(--text); font-size: 12px; }
.run-log-level { display: inline-flex; align-items: center; min-height: 20px; padding: 1px 7px; border-radius: 10px; font-size: 10px; }
.run-log-level.is-success { background: #e7f6ee; color: #087443; }
.run-log-level.is-info { background: #eaf1ff; color: #2458a6; }
.run-log-level.is-warning { background: #fff4dc; color: #9a6700; }
.run-log-level.is-error { background: #fdecea; color: #a33125; }
.run-log-message { margin: 5px 0; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.run-log-message.is-success { color: #087443; }
.run-log-message.is-info { color: var(--text); }
.run-log-message.is-warning { color: #8a5b00; }
.run-log-message.is-error { color: #a33125; }
.run-log-product-id { display: block; margin: 3px 0 5px; color: var(--text-muted); font-size: 11px; }
.failure-log-entry details { color: var(--text-muted); font-size: 11px; }
.failure-log-entry summary { cursor: pointer; }
.failure-log-entry pre { max-height: 240px; margin: 6px 0 0; padding: 8px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; border-radius: var(--radius); background: var(--bg-app); font: 11px/1.45 Consolas, monospace; }
@media (max-width: 1150px) { .browser-panel { width: 38%; min-width: 340px; } .form-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
