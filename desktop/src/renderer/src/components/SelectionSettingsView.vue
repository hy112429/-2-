<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useAppRefresh } from '../utils/app_refresh'
import BrowserPane from './BrowserPane.vue'
import SelectionFilterEditor from './SelectionFilterEditor.vue'
import { createSelectionFilterSettings } from '../utils/selection_filters'
import type { CandidatePoolQuotaRule, SelectionFilterSettings } from '../utils/selection_filters'
import {
  allocateExclusiveCandidates,
  candidateLeafCategoryId,
  filterCandidatesByRegistry,
  rankByRecommendedCategories,
  rankCandidatesForBatch,
  scoreUnifiedCandidatePools,
  selectionFilterFingerprint,
  selectionPoolVisibilityFingerprint
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

function isBaiyingCooldownClassification(value: string | undefined): boolean {
  return value === 'BAIYING_RATE_LIMIT' || value === 'BAIYING_NETWORK_ERROR' ||
    value === 'BAIYING_11001_UNCONFIRMED'
}

function consumer11001RetryPlan(reason: string, logs: unknown, accountId: number): {
  code: 'BAIYING_RATE_LIMIT' | 'BAIYING_NETWORK_ERROR' | 'BAIYING_11001_UNCONFIRMED'
  label: string
  cooldownSeconds: number
} {
  const classification = toUserSelectionError(reason).classification
  const code = classification === 'BAIYING_NETWORK_ERROR' || classification === 'BAIYING_11001_UNCONFIRMED'
    ? classification : 'BAIYING_RATE_LIMIT'
  const label = code === 'BAIYING_NETWORK_ERROR'
    ? '百应商品搜索返回 11001，平台提示网络错误'
    : code === 'BAIYING_11001_UNCONFIRMED'
      ? '百应商品搜索返回 11001，原因未确认'
      : '百应商品搜索返回 11001，平台提示频控'
  const accountLogs = Array.isArray(logs) ? logs.filter((entry) => {
    const log = entry && typeof entry === 'object' ? entry as Record<string, unknown> : {}
    return Number(log.accountId) === accountId
  }) as Record<string, unknown>[] : []
  const lastConfirmedCartId = accountLogs.reduce((latest, log) =>
    String(log.code ?? '') === 'SUCCESS_PRODUCT_CART_ADDED' ? Math.max(latest, Number(log.id) || 0) : latest, 0)
  const previousCount = accountLogs.filter((log) => {
    const previousCode = String(log.code ?? '')
    return (previousCode === code ||
      (code === 'BAIYING_11001_UNCONFIRMED' && previousCode === 'BAIYING_RATE_LIMIT')) &&
      (Number(log.id) || 0) > lastConfirmedCartId
  }).length
  const baseSeconds = code === 'BAIYING_NETWORK_ERROR' ? 30 : 60
  return { code, label, cooldownSeconds: Math.min(15 * 60, baseSeconds * (2 ** Math.min(previousCount, 5))) }
}

function safeCandidateCollectorErrorDetail(input: unknown): string {
  const raw = String(input instanceof Error ? input.message : input ?? '').trim()
  if (!raw) return ''
  return raw
    .replace(/https?:\/\/[^\s)]+/gi, (value) => {
      try {
        const url = new URL(value)
        for (const key of [...url.searchParams.keys()]) {
          if (/(cookie|token|secret|auth|session|password|signature|sign)/i.test(key)) {
            url.searchParams.set(key, '[redacted]')
          }
        }
        return url.toString()
      } catch {
        return '[redacted-url]'
      }
    })
    .replace(/(cookie|token|secret|auth|session|password|signature|sign)\s*[=:]\s*[^\s,;]+/gi, '$1=[redacted]')
    .slice(0, 1_200)
}

const props = withDefaults(defineProps<{
  initialAccountId?: number | null
  dialogOnly?: boolean
  active?: boolean
  scheduledStart?: SelectionScheduleDue | null
}>(), {
  initialAccountId: null,
  dialogOnly: false,
  active: true,
  scheduledStart: null
})
const emit = defineEmits<{
  (event: 'close'): void
  (event: 'saved', accountId: number): void
  (event: 'navigate', value: string): void
  (event: 'scheduled-start-handled', date: string, outcome: string): void
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
  sourcePoolState?: {
    sources: Record<string, { fingerprint: string; productCount: number; readAt: string; pagePath?: string; scrollPosition?: number }>
    samePoolCount: number
    nextRetryAt?: string
    detailAttemptedIds?: string[]
    tabOffsets?: Record<string, number>
  } | null
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
  status: 'read' | 'empty' | 'skipped' | 'disabled'
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
  dataCapturedAt?: string
  visibilityFingerprint?: string
  productIdFingerprint?: string
  readAt?: string
  pagePath?: string
  scrollPosition?: number | { top?: number; height?: number; viewport?: number }
  pageReads?: Array<{ query?: number; step: number; scrollTop?: number; newProductIds?: number }>
  categoryTabNextOffset?: number
  categoryTabTotal?: number
  categoryTabReadLimitReached?: boolean
  sourceReadLimitReached?: boolean
  scrollPositions?: Record<string, number>
  sourceFilterRejected?: number
  rankSelection?: Record<string, string>
  sourceCategoryTabs?: Array<{ name?: string; categoryId?: string; status?: string; level?: number; selected?: boolean }>
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
  logEvents: (values: SelectionRunLogInput[]) => Promise<void>
}

interface AccountCandidatePlan {
  candidates: SelectionCandidate[]
  /** 完成本机/配额/发布锁过滤后的唯一三源候选池；只留在预览缓存，不写入批次 payload。 */
  candidatePool?: SelectionCandidate[]
  /** 来源统一筛选后的原始候选池；允许同条件账号复用，账号登记/配额不包含在内。 */
  sharedCandidatePool?: SelectionCandidate[]
  candidatePoolMeta?: {
    createdByAccountId: number
    filterFingerprint: string
    visibilityFingerprint: string
    dataCapturedAt: string
    reuseStatus: 'created' | 'reused' | 'not_reused'
    reuseReason?: string
  }
  registryExcluded: Array<{ candidate: SelectionCandidate; reason: string }>
  mutualExclusionSkipped: SelectionCandidate[]
  rejectedCandidates: SelectionCandidate[]
  prioritySourceResults: PrioritySourceReadResult[]
  warnings: string[]
  recommendedCategories: Array<{ categoryId: string; highScoreCount: number; averageScore: number }>
  categoryEnrichment?: { summary?: string; detailUnresolvedProductIds?: string[]; deferredCount?: number }
}

interface BatchCandidatePool {
  target: number
  mode?: 'total' | 'quota'
  quotaRules?: CandidatePoolQuotaRule[]
  quotaProgress?: Array<{ ruleId: string; collected: number; target: number; effectiveTarget?: number; transferredIn?: number; transferredOut?: number; deficit: number }>
  quotaTransfers?: Array<{ fromRuleId: string; toRuleId: string; count: number; transferredAt: string }>
  excludedProductIds?: string[]
  collectorAccountIds: number[]
  filterFingerprint: string
  rows: SelectionCandidate[]
  sourceState: { autoResumeAllowed?: boolean; collectors: Record<string, { tabOffsets: Record<string, number>; samePoolCount: number; rateLimitCount?: number; nextRetryAt?: string; scrollSteps?: number; rawLimit?: number; scrollPositions?: Record<string, number>; detailAttemptedIds?: string[]; ruleStates?: Record<string, { samePoolCount: number; nextRetryAt?: string; exhausted?: boolean; scrollSteps?: number; rawLimit?: number; scrollPosition?: number; categoryTabOffset?: number; detailAttemptedIds?: string[] }> }> }
  status: 'collecting' | 'ready'
  capturedAt: string
}

interface SavedLocalCandidatePool {
  batchId: string
  duplicateBatches: Array<{ batchId: string; capturedAt: string; updatedAt: string }>
  count: number
  recentCount: number
  target: number
  mode: 'total' | 'quota'
  capturedAt: string
  updatedAt: string
  collectorAccountIds: number[]
  sourceCounts: Record<string, number>
  minimumCommissionRate: number | null
  maximumCommissionRate: number | null
  excludedCategoryCount: number
  preview: Array<{ productId: string; title: string; sourceKey: string }>
}

interface FilterField {
  label: string
  options: string[]
  multiple: boolean
}

interface SelectionCandidate {
  collectedByAccountId?: number
  capturedAt?: string
  cardIndex: number
  productId: string
  cartAdded?: boolean
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
  trendKnown?: boolean
  trendStatus?: string
  trendSlope?: number | null
  trendPoints?: number
  poolRank: number
  sources?: CandidateSource[]
  dataMetrics?: Record<string, number | null>
  batchRandomKey?: number
  metricSources?: Record<string, string>
  analysisScore?: number | null
  unifiedScore?: number | null
  hotOrRankBoost?: number
  businessPriorityBoost?: number
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
  titleBannedWord?: string
  titleFilterExcluded?: boolean
  status?: string
  selected: boolean
  sourceCategorySelection?: { level1: string[]; level2: string[]; level3: string[] }
  sourceCategoryId?: string
  sourceCategoryName?: string
  sourceRecords?: Array<{ sourceKey: string; categoryPath?: string[]; rankBoard?: string; sourceCategoryId?: string; sourceCategoryName?: string }>
  categoryIds?: string[]
  leafCategoryId?: string
  sourceKey?: string
  quotaRuleId?: string
  sourceUrl?: string
  selectionMode?: 'general' | 'category'
  categoryMode?: 'auto' | 'manual'
  categoryStatus?: string
  categorySource?: string
  categoryError?: string
  mutualExclusionReason?: string
  mutualExclusionWinnerAccountId?: number
}

interface CandidateDetail extends Partial<SelectionCandidate> {
  productId: string
  title: string
  sourceCategoryIds?: string[]
  reusedFromBatchId?: string
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
const SUPERSTITION_BANNED_WORDS = [
  '封建迷信', '迷信用品', '算命', '算卦', '占卜', '占星', '看相', '看手相', '手相', '面相',
  '命理', '星座运势', '生肖运势', '风水', '玄学', '运势预测', '改命', '改运', '转运', '开运',
  '招财', '旺财', '旺运', '催财', '聚财', '招桃花', '催姻缘', '旺姻缘', '辟邪', '驱邪',
  '镇宅', '化煞', '消灾', '消灾解难', '符咒', '灵符', '护身符', '平安符', '招财符', '太岁符',
  '五帝钱', '八卦镜', '转运珠', '本命佛', '佛牌', '开光', '法器', '开运摆件', '招财摆件', '风水摆件'
]
const OPTIONAL_MALL_TITLE_BANNED_WORDS = ['编码', '过号', '集合', '随机', '测试', '勿拍', '链接', '补差价']
const DEFAULT_BANNED_WORDS = Array.from(new Set([
  ...COMPLIANCE_DEFAULT_BANNED_WORDS, ...PROTOTYPE_BANNED_WORDS, ...SUPERSTITION_BANNED_WORDS
]))

function matchedSelectionTitleBannedWord(title: unknown, settings: Record<string, unknown>): string {
  const configured = Array.isArray(settings.bannedWords) ? settings.bannedWords : []
  const words = Array.from(new Set([
    ...configured.map((word) => String(word).trim()).filter(Boolean),
    ...SUPERSTITION_BANNED_WORDS
  ]))
  const normalizedTitle = String(title ?? '').toLocaleLowerCase()
  return words.find((word) => normalizedTitle.includes(word.toLocaleLowerCase())) ?? ''
}

const DEFAULTS = {
  candidateLimit: 5,
  candidatePoolTarget: 300,
  candidatePoolMode: 'total' as 'total' | 'quota',
  candidatePoolQuotas: [] as CandidatePoolQuotaRule[],
  candidateCollectorAccountIds: [] as number[],
  commissionRateRule: 'minimum' as const,
  selectionMode: 'general' as 'general' | 'category',
  categoryMode: 'auto' as 'auto' | 'manual',
  categorySelection: { level1: [] as string[], level2: [] as string[], level3: [] as string[] },
  excludedCategorySelection: { level1: [] as string[], level2: [] as string[], level3: [] as string[] },
  filters: { delivery: {} as Record<string, unknown>, product: {} as Record<string, unknown>, features: [] as string[] },
  poolPriority: ['流量扶持', '小二严选', '低价好货', '福袋奖品'],
  poolFallback: true,
  sources: { hotSpotEnabled: true, allianceRankEnabled: true, selectionSquareEnabled: true, rankBoard: '爆款榜' as SelectionFilterSettings['sources']['rankBoard'], rankPeriod: '昨日' as SelectionFilterSettings['sources']['rankPeriod'], rankCarrier: '不限载体' as SelectionFilterSettings['sources']['rankCarrier'], rankPriceRange: '不限' as SelectionFilterSettings['sources']['rankPriceRange'], rankCategoryPath: [] as string[], hotSpotMinExposure: null as number | null, hotSpotMinRelatedProducts: null as number | null, priority: ['hotSpot', 'allianceRank'] as Array<'hotSpot' | 'allianceRank'> },
  thresholds: { salesAmount: null as number | null, salesCount: null as number | null, conversionRate: null as number | null, views: null as number | null, commissionRate: null as number | null, commissionRateMax: null as number | null, experienceScore: null as number | null },
  trendPreference: '上升优先',
  bannedWords: [...DEFAULT_BANNED_WORDS],
  bannedWordsSeedVersion: 2,
  execution: { publishMode: '自动', pinTime: '', randomIntervalMin: 1, randomIntervalMax: 5 },
  quota: { perProductVideos: 10, perAccountProducts: 5, perAccountVideos: 50 }
}

const busy = ref(false)
const commissionRuleMigrationRequired = ref(false)
const error = ref('')
const notice = ref('')
const selectionInitialized = ref(false)
const selectionUiDataReady = ref(false)
let selectionUiDataPromise: Promise<void> | null = null
const dailySelectionSchedule = reactive({
  enabled: false,
  time: '09:00',
  launchAtLogin: false,
  canLaunchAtLogin: false,
  saving: false
})
let savedDailySelectionScheduleEnabled = false
const handledScheduledStartDates = new Set<string>()
const scheduledRetryWaiting = ref(false)
let automaticScheduledStartActive = false
let activeScheduledStartDate: string | null = null
let queuedScheduledStart: SelectionScheduleDue | null = null
const SCHEDULED_RETRY_BASE_DELAY_MS = 30_000
const SCHEDULED_RETRY_MAX_DELAY_MS = 5 * 60_000
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
let selectionProgressFlushTimer: number | null = null
let pendingSelectionProgressUpdate: (() => void) | null = null
let selectionViewUnmounted = false
let stopSelectionCooldownExpired: (() => void) | undefined
let automaticResumeBatchId: string | null = null
let automaticResumeAccountId: number | null = null
let candidatePoolResumeTimer: number | null = null
let automaticPoolResumeBatchId: string | null = null
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
  resultSummary?: string
}
const selectionProgress = ref<SelectionProgressState | null>(null)
interface CandidatePoolProgressState {
  batchId: string
  count: number | null
  target: number
  status: 'collecting' | 'ready'
  mode: 'total' | 'quota'
  sourceCounts: Record<string, number>
  quotaRules: CandidatePoolQuotaRule[]
  quotaProgress: NonNullable<BatchCandidatePool['quotaProgress']>
  quotaTransfers: NonNullable<BatchCandidatePool['quotaTransfers']>
}
const candidatePoolProgress = ref<CandidatePoolProgressState | null>(null)
const batchProgressPhase = ref<'pool' | 'account'>('pool')
const manualPauseRequested = ref(false)
const manualPauseSaving = ref(false)
const activeSelectionRunBatchId = ref<string | null>(null)
let manualPauseWritePromise: Promise<void> | null = null
let stopSelectionProgress: (() => void) | undefined
let stopAutoPublishProgress: (() => void) | undefined
let activeAutoPublishQueueId: number | null = null

interface SelectionWorkflowStep {
  key: string
  label: string
  weight: number
}

const LEGACY_BATCH_SELECTION_WORKFLOW: SelectionWorkflowStep[] = [
  { key: 'preflight', label: '检查账号、浏览器与登录状态', weight: 8 },
  { key: 'collect', label: '采集百应当前可见商品', weight: 20 },
  { key: 'quota_check', label: '检查账号剩余商品配额', weight: 5 },
  { key: 'filter', label: '统一筛选、评分、类目推荐与 ID 去重', weight: 12 },
  { key: 'cart', label: '逐个核验候选并加入选品车', weight: 20 },
  { key: 'quota_record', label: '登记商品配额', weight: 3 },
  { key: 'video', label: '处理详情图并生成视频', weight: 20 },
  { key: 'queue', label: '写入发布队列（全自动时继续发布）', weight: 12 }
]

const SHARED_POOL_ACCOUNT_WORKFLOW: SelectionWorkflowStep[] = [
  { key: 'account_check', label: '检查当前账号与商品配额', weight: 10 },
  { key: 'allocate', label: '从共享候选池分配商品 ID', weight: 10 },
  { key: 'cart', label: '按精确商品 ID 核验并加车', weight: 40 },
  { key: 'quota_record', label: '登记商品配额', weight: 5 },
  { key: 'video', label: '处理详情图并生成视频', weight: 20 },
  { key: 'queue', label: '写入队列或自动发布并读取回执', weight: 15 }
]

const BATCH_FALLBACK_CANDIDATE_LIMIT = 50

const PREVIEW_SELECTION_WORKFLOW: SelectionWorkflowStep[] = [
  { key: 'preflight', label: '检查账号设置与本机目录', weight: 15 },
  { key: 'collect', label: '采集百应当前可见商品', weight: 60 },
  { key: 'filter', label: '统一筛选、商品 ID 去重与类目推荐', weight: 20 },
  { key: 'result', label: '整理并显示候选预览', weight: 5 }
]

function poolSourceCounts(rows: SelectionCandidate[]): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const row of rows) {
    const source = row.sourceKey || 'unknown'
    counts[source] = (counts[source] ?? 0) + 1
  }
  return counts
}

function showSavedPoolCheckpoint(batch: SelectionRecoveryBatch): void {
  const settings = batch.payload.settingsSnapshot as Record<string, unknown> | undefined
  const collectors = settings?.candidateCollectorAccountIds
  if (!Array.isArray(collectors) || !collectors.length) {
    candidatePoolProgress.value = null
    return
  }
  const target = Number(settings?.candidatePoolTarget)
  const pool = batch.payload.candidatePool as BatchCandidatePool | undefined
  const validPool = pool && Array.isArray(pool.rows) && pool.target === target &&
    pool.filterFingerprint === batchCandidatePoolFingerprint(settings ?? {}) &&
    pool.collectorAccountIds?.join(',') === collectors.map(Number).join(',') ? pool : undefined
  candidatePoolProgress.value = {
    batchId: batch.batchId,
    count: validPool ? validPool.rows.length : null,
    target: Number.isInteger(target) && target > 0 ? target : 0,
    status: validPool?.status ?? 'collecting',
    mode: settings?.candidatePoolMode === 'quota' ? 'quota' : 'total',
    sourceCounts: validPool ? poolSourceCounts(validPool.rows) : {},
    quotaRules: validPool?.quotaRules ?? [],
    quotaProgress: validPool?.quotaProgress ?? [],
    quotaTransfers: validPool?.quotaTransfers ?? []
  }
}

function showConfirmedPoolSave(batchId: string, target: number, mode: 'total' | 'quota',
                               status: 'collecting' | 'ready', rows: SelectionCandidate[],
                               rules: CandidatePoolQuotaRule[] = [],
                               transfers: NonNullable<BatchCandidatePool['quotaTransfers']> = []): void {
  const effectiveTargets = new Map(rules.map((rule) => [rule.id, rule.target]))
  for (const transfer of transfers) {
    effectiveTargets.set(transfer.fromRuleId, (effectiveTargets.get(transfer.fromRuleId) ?? 0) - transfer.count)
    effectiveTargets.set(transfer.toRuleId, (effectiveTargets.get(transfer.toRuleId) ?? 0) + transfer.count)
  }
  const counts = new Map(rules.map((rule) => [rule.id, 0]))
  for (const row of rows) if (row.quotaRuleId && counts.has(row.quotaRuleId)) {
    counts.set(row.quotaRuleId, (counts.get(row.quotaRuleId) ?? 0) + 1)
  }
  candidatePoolProgress.value = {
    batchId, count: rows.length, target, status, mode,
    sourceCounts: poolSourceCounts(rows), quotaRules: rules,
    quotaProgress: rules.map((rule) => {
      const effectiveTarget = effectiveTargets.get(rule.id) ?? rule.target
      const collected = counts.get(rule.id) ?? 0
      return {
        ruleId: rule.id, collected, target: rule.target, effectiveTarget,
        transferredIn: transfers.filter((item) => item.toRuleId === rule.id).reduce((sum, item) => sum + item.count, 0),
        transferredOut: transfers.filter((item) => item.fromRuleId === rule.id).reduce((sum, item) => sum + item.count, 0),
        deficit: Math.max(0, effectiveTarget - collected)
      }
    }),
    quotaTransfers: transfers
  }
}

const showCandidatePoolProgress = computed(() => {
  const progress = selectionProgress.value
  if (progress?.mode === 'preview') return false
  if (progress?.mode === 'batch') return batchProgressPhase.value === 'pool' || candidatePoolProgress.value !== null
  return candidatePoolProgress.value !== null && ['in_progress', 'needs_retry'].includes(recoveryBatch.value?.state ?? '')
})
const candidatePoolProgressPercent = computed(() => {
  const pool = candidatePoolProgress.value
  return pool?.count !== null && pool?.count !== undefined && pool.target > 0
    ? Math.max(0, Math.min(100, Math.floor(pool.count * 100 / pool.target))) : null
})
const candidatePoolSourceProgress = computed(() => {
  const pool = candidatePoolProgress.value
  if (!pool) return []
  const sources = ['hotSpot', 'allianceRank', 'selectionSquare']
  return sources.map((key) => ({
    key, label: localPoolSourceLabels[key] ?? key,
    collected: pool.sourceCounts[key] ?? 0,
    target: pool.mode === 'quota'
      ? pool.quotaProgress.filter((item) => pool.quotaRules.find((rule) => rule.id === item.ruleId)?.sourceKey === key)
        .reduce((sum, item) => sum + (item.effectiveTarget ?? item.target), 0) : null
  })).filter((item) => item.collected > 0 || (item.target ?? 0) > 0)
})
function candidatePoolProgressRuleLabel(ruleId: string): string {
  const rule = candidatePoolProgress.value?.quotaRules.find((item) => item.id === ruleId)
  return rule ? formatQuotaRule(rule) : ruleId
}

function workflowIndexForStage(stageValue: string, mode: SelectionProgressState['mode'], sharedPool: boolean): number {
  const stage = stageValue.replace(/^(失败于：|账号处理失败：)/, '').trim()
  if (mode === 'preview') {
    if (/candidate_fetch/.test(stage)) return 1
    if (/候选预览完成|预览结果/.test(stage)) return 3
    if (/评分|去重|筛选与互斥|类目推荐|推荐类目|统一分配/.test(stage)) return 2
    if (/准备候选预览|准备账号选品任务|检查账号设置|读取账号设置/.test(stage)) return 0
    if (/采集|候选|百应筛选|热点|联盟|榜单|来源|列表/.test(stage)) return 1
    return 0
  }
  if (sharedPool) {
    if (/video_pending|详情图|图转视频|生成视频|视频生成|恢复详情图/.test(stage)) return 4
    if (/发布|投稿|队列|上传视频|创作者中心|封面|关联百应|发布选项/.test(stage)) return 5
    if (/quota_cart|登记加车配额|记录(?:加车|商品|选品车)?配额|配额记录/.test(stage)) return 3
    if (/cart_pending|加车|核验并处理|处理选中商品|选中商品处理结果|商品数据分析|选品车|候选商品处理/.test(stage)) return 2
    if (/quota_check|账号配额预检|每日配额|剩余配额|账号配额|登录|账号资源校验/.test(stage)) return 0
    if (/candidate_fetch|candidate_refilling|评分|筛选|类目推荐|互斥|候选|准备账号选品任务|账号选品执行中|开始顺序处理账号|等待账号执行/.test(stage)) return 1
    if (/完成|处理结束/.test(stage)) return SHARED_POOL_ACCOUNT_WORKFLOW.length - 1
    return 0
  }
  if (/video_pending/.test(stage)) return 6
  if (/cart_pending/.test(stage)) return 4
  if (/quota_cart/.test(stage)) return 5
  if (/quota_check/.test(stage)) return 2
  if (/candidate_fetch/.test(stage)) return 1
  if (/准备选品批次|读取启用账号|检查百应与创作者中心登录态|复核登录状态|账号资源校验|登录状态预检/.test(stage)) return 0
  if (/详情图|图转视频|生成视频|视频生成|恢复详情图/.test(stage)) return 6
  if (/发布|投稿|队列|上传视频|创作者中心|封面|关联百应|发布选项/.test(stage)) return 7
  if (/登记加车配额|记录(?:加车|商品|选品车)?配额|配额记录/.test(stage)) return 5
  if (/加车|核验并处理|处理选中商品|商品数据分析|选品车|候选商品处理/.test(stage)) return 4
  if (/评分|筛选与互斥|类目推荐|推荐类目|互斥分配|候选分配|ID 去重|ID去重|选品登记|保存选品批次|候选计划已就绪|候选统一/.test(stage)) return 3
  if (/统一采集|百应筛选|获取候选|候选预览|查询百应候选|应用选品条件|material_list|来源|热点|联盟|榜单|商品列表/.test(stage)) return 1
  if (/账号配额预检/.test(stage)) return 0
  if (/每日配额|剩余配额|账号配额|quota_check/.test(stage)) return 2
  // 批次候选计划已在账号执行前统一采集；账号执行入口的准备/配额检查属于候选整理阶段。
  if (/准备账号选品任务|账号选品执行中|开始顺序处理账号|等待账号执行/.test(stage)) return 3
  if (/完成|处理结束/.test(stage)) return LEGACY_BATCH_SELECTION_WORKFLOW.length - 1
  return 0
}

const selectionWorkflowSteps = computed(() => {
  const progress = selectionProgress.value
  return progress?.mode === 'preview' ? PREVIEW_SELECTION_WORKFLOW
    : candidatePoolProgress.value ? SHARED_POOL_ACCOUNT_WORKFLOW : LEGACY_BATCH_SELECTION_WORKFLOW
})

const selectionWorkflowStageIndex = computed(() => {
  const progress = selectionProgress.value
  if (!progress) return 0
  return workflowIndexForStage(progress.stage, progress.mode, candidatePoolProgress.value !== null)
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
  if (progress.mode === 'batch' && candidatePoolProgress.value && progress.accountId === null) return null
  const steps = selectionWorkflowSteps.value
  const stageIndex = Math.max(0, Math.min(steps.length - 1, selectionWorkflowStageIndex.value))
  const priorWeight = steps.slice(0, stageIndex).reduce((sum, step) => sum + step.weight, 0)
  const stageWeight = steps[stageIndex]?.weight ?? 0
  const stagePercent = selectionWorkflowStagePercent.value ?? 0

  const videoIndex = candidatePoolProgress.value ? 4 : 6
  if (progress.mode === 'batch' && stageIndex >= videoIndex) {
    // 视频与队列按商品串行处理；商品序号参与计算，避免每处理下一个商品时总进度倒退。
    const itemTotal = Math.max(1, progress.itemTotal ?? 1)
    const itemIndex = Math.max(1, Math.min(itemTotal, progress.itemIndex ?? 1))
    const itemPhase = stageIndex === videoIndex
      ? 0.7 * stagePercent / 100
      : 0.7 + 0.3 * stagePercent / 100
    const completedBeforeProducts = steps.slice(0, videoIndex).reduce((sum, step) => sum + step.weight, 0)
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
  syncSelectionCooldownTicker()
}

function refreshCooldownFromDeadline(): void {
  if (!selectionCooldownArmed.value || selectionCooldownDeadline === null) return
  const remaining = Math.max(0, Math.ceil((selectionCooldownDeadline - Date.now()) / 1000))
  if (selectionCooldownRemaining.value !== remaining) selectionCooldownRemaining.value = remaining
  if (selectionCooldownRemaining.value === 0 && selectionCooldownTimer !== null) {
    window.clearInterval(selectionCooldownTimer)
    selectionCooldownTimer = null
  }
}

function stopSelectionCooldownTicker(): void {
  if (selectionCooldownTimer !== null) window.clearInterval(selectionCooldownTimer)
  selectionCooldownTimer = null
}

function syncSelectionCooldownTicker(): void {
  stopSelectionCooldownTicker()
  refreshCooldownFromDeadline()
  if (!props.active || document.visibilityState === 'hidden' || !selectionCooldownArmed.value ||
      selectionCooldownDeadline === null || selectionCooldownRemaining.value <= 0) return
  // 倒计时只用于展示；主进程负责到期续选。降低无关页面的 Vue 重渲染频率，避免长设置页滚动掉帧。
  selectionCooldownTimer = window.setInterval(refreshCooldownFromDeadline, 5000)
}

function handleSelectionViewVisibilityChange(): void {
  if (document.visibilityState === 'visible' && props.active) {
    syncSelectionCooldownTicker()
    flushSelectionProgressUpdate()
  } else {
    stopSelectionCooldownTicker()
    if (selectionProgressFlushTimer !== null) window.clearTimeout(selectionProgressFlushTimer)
    selectionProgressFlushTimer = null
  }
}

watch(() => props.active, handleSelectionViewVisibilityChange)

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
  if (props.dialogOnly || manualPausePending()) {
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
      isBaiyingCooldownClassification(toUserSelectionError(account.error).classification)
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
  if (manualPauseRequested.value || batchIsManuallyPaused(batch)) {
    stopManualPauseAutoResume()
    return false
  }
  const limitedAccount = batch?.accounts.find((account) =>
    account.state === 'needs_retry' &&
    isBaiyingCooldownClassification(toUserSelectionError(account.error).classification)
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
  const sameIntent = Boolean(currentIntent && batch && currentIntent.batchId === batch.batchId &&
    currentIntent.accountId === limitedAccount.accountId)
  const deadlineExtended = sameIntent && deadline > (currentIntent?.deadline ?? 0)
  if (!sameIntent || deadlineExtended) {
    clearAutoResumeTimer()
    cooldownAutoResumeIntent.value = {
      batchId: batch?.batchId ?? '', accountId: limitedAccount.accountId,
      deadline, attempts: 0, phase: 'pending'
    }
    completedCooldownAutoResume = null
    if (deadlineExtended && batch) traceSelectionCooldown('renderer_cooldown_deadline_extended', batch.batchId, {
      accountId: limitedAccount.accountId, previousDeadline: currentIntent?.deadline ?? null, deadline
    })
  }
  if (sameIntent && currentIntent?.phase === 'starting' && !deadlineExtended) return true
  const activeDeadline = sameIntent && !deadlineExtended
    ? Math.max(deadline, currentIntent?.deadline ?? deadline, currentIntent?.retryAt ?? 0) : deadline
  if (!selectionCooldownArmed.value || selectionCooldownDeadline !== activeDeadline) {
    armSelectionCooldown(Math.max(0, Math.ceil((activeDeadline - Date.now()) / 1000)), activeDeadline)
  } else {
    refreshCooldownFromDeadline()
  }
  return true
}

watch(notice, (message) => {
  if (!message) return
  const presentation = toUserSelectionError(message)
  if (isBaiyingCooldownClassification(presentation.classification) && !manualPausePending()) {
    if (!restoreSelectionCooldown(recoveryBatch.value)) armSelectionCooldown(presentation.cooldownSeconds ?? 60)
  }
  if (selectionProgress.value?.state !== 'running') notifyGlobal(presentation.userMessage)
})
watch(error, (message) => {
  if (!message) return
  const presentation = toUserSelectionError(message)
  if (isBaiyingCooldownClassification(presentation.classification) && !manualPausePending()) {
    if (!restoreSelectionCooldown(recoveryBatch.value)) armSelectionCooldown(presentation.cooldownSeconds ?? 60)
  }
  notifyGlobal(presentation.userMessage, 'error')
})

const recoveryBatch = ref<SelectionRecoveryBatch | null>(null)
function batchIsManuallyPaused(batch: SelectionRecoveryBatch | null): boolean {
  const value = batch?.payload.manualPause
  return Boolean(batch && ['in_progress', 'needs_retry'].includes(batch.state) &&
    value && typeof value === 'object' && (value as Record<string, unknown>).active === true)
}
function manualPausePending(): boolean {
  return manualPauseRequested.value || batchIsManuallyPaused(recoveryBatch.value)
}
async function persistManualPause(batchId: string): Promise<void> {
  const previousWrite = manualPauseWritePromise
  manualPauseSaving.value = true
  const write = (async () => {
    if (previousWrite) await previousWrite.catch(() => undefined)
    const result = await runRecovery(['batch', 'manual-pause', '--batch-id', batchId, '--state', 'request'])
    const saved = result.batch as SelectionRecoveryBatch | undefined
    if (!saved || saved.batchId !== batchId || !batchIsManuallyPaused(saved)) {
      throw new Error('手动暂停标记未能从批次检查点回读')
    }
    recoveryBatch.value = saved
  })()
  manualPauseWritePromise = write
  try {
    await write
  } finally {
    if (manualPauseWritePromise === write) {
      manualPauseWritePromise = null
      manualPauseSaving.value = false
    }
  }
}
function stopManualPauseAutoResume(): void {
  clearCandidatePoolResumeTimer()
  clearAutoResumeTimer()
  cooldownAutoResumeIntent.value = null
  autoResumeAccountId.value = null
  automaticResumeBatchId = null
  automaticResumeAccountId = null
  automaticPoolResumeBatchId = null
  void window.api.selectionCooldown.cancel().catch(() => undefined)
}
async function requestManualSelectionPause(): Promise<void> {
  if (manualPausePending() || manualPauseSaving.value) return
  if (busy.value && (selectionProgress.value?.mode !== 'batch' || selectionProgress.value?.state !== 'running')) return
  if (!busy.value && recoveryBatch.value?.state !== 'needs_retry') return
  manualPauseRequested.value = true
  stopManualPauseAutoResume()
  notice.value = busy.value
    ? '已请求安全暂停；当前平台操作完成并保存检查点后停止后续步骤。'
    : '已请求暂停自动续选；正在保存本批次暂停标记。'
  const batchId = activeSelectionRunBatchId.value ?? (!busy.value ? recoveryBatch.value?.batchId : null)
  if (!batchId) return
  try {
    await persistManualPause(batchId)
    if (!busy.value) notice.value = '本批次已手动暂停；重启和冷却到期不会自动续做，请点击“继续选品”恢复。'
  } catch (cause) {
    error.value = '当前会话已停止后续选品，但暂停标记未能落盘；重启前请核对批次状态：' + conciseFailureMessage(cause)
  }
}
async function clearManualSelectionPause(): Promise<boolean> {
  const batch = recoveryBatch.value
  if (!batch || !batchIsManuallyPaused(batch)) {
    manualPauseRequested.value = false
    return true
  }
  manualPauseSaving.value = true
  try {
    const result = await runRecovery(['batch', 'manual-pause', '--batch-id', batch.batchId, '--state', 'clear'])
    const saved = result.batch as SelectionRecoveryBatch | undefined
    if (!saved || saved.batchId !== batch.batchId || batchIsManuallyPaused(saved)) {
      throw new Error('手动暂停解除结果未能回读')
    }
    recoveryBatch.value = saved
    manualPauseRequested.value = false
    restoreSelectionCooldown(saved)
    return true
  } catch (cause) {
    error.value = '解除手动暂停失败，未开始新平台操作：' + conciseFailureMessage(cause)
    return false
  } finally {
    manualPauseSaving.value = false
  }
}
async function pauseBatchAtSafeBoundary(batchId: string, stage: string): Promise<boolean> {
  if (!manualPausePending()) return false
  stopManualPauseAutoResume()
  let persisted = false
  try {
    await persistManualPause(batchId)
    persisted = true
  } catch (cause) {
    error.value = '当前轮次已停止，但手动暂停标记未能落盘，重启前须核对批次：' + conciseFailureMessage(cause)
  }
  const updated = await runRecovery(['batch', 'update', '--batch-id', batchId, '--state', 'needs_retry'])
  const savedBatch = updated.batch as SelectionRecoveryBatch
  recoveryBatch.value = savedBatch
  showSavedPoolCheckpoint(savedBatch)
  const message = persisted
    ? '已在安全边界手动暂停；检查点保留，重启和冷却到期不会自动续做。点击“继续选品”恢复。'
    : '本轮已在安全边界停止；暂停标记落盘失败，请先核对批次，避免重启后自动续做。'
  notice.value = message
  if (selectionProgress.value) selectionProgress.value = {
    ...selectionProgress.value, state: 'finished', stage, message, percent: null
  }
  try {
    await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', '0'], {
      stage: '手动暂停', code: persisted ? 'INFO_MANUAL_PAUSE' : 'WARNING_MANUAL_PAUSE_NOT_SAVED', message
    })
  } catch { /* 暂停结果已在批次检查点和页面提示中保留。 */ }
  return true
}
const activeQuotaPool = computed(() => recoveryBatch.value?.payload.candidatePool as BatchCandidatePool | undefined)
const activePoolSearch = ref('')
const activePoolPage = ref(1)
const activePoolPageSize = 20
const activePoolFilteredItems = computed(() => {
  const keyword = activePoolSearch.value.trim().toLocaleLowerCase('zh-CN')
  const rows = activeQuotaPool.value?.rows ?? []
  return keyword ? rows.filter((item) => item.productId.includes(keyword) || item.title.toLocaleLowerCase('zh-CN').includes(keyword)) : rows
})
const activePoolPageCount = computed(() => Math.max(1, Math.ceil(activePoolFilteredItems.value.length / activePoolPageSize)))
const activePoolVisibleItems = computed(() => activePoolFilteredItems.value.slice(
  (activePoolPage.value - 1) * activePoolPageSize, activePoolPage.value * activePoolPageSize))
watch(activePoolSearch, () => { activePoolPage.value = 1 })
watch(activeQuotaPool, () => { activePoolPage.value = 1 })
const savedLocalPools = ref<SavedLocalCandidatePool[]>([])
const savedLocalPoolLoading = ref(false)
const savedPoolBrowserBatchId = ref<string | null>(null)
const savedPoolSearch = ref('')
const savedPoolAppliedSearch = ref('')
const savedPoolItems = ref<CandidateDetail[]>([])
const savedPoolItemsLoading = ref(false)
const savedPoolItemsError = ref('')
const savedPoolPage = ref(1)
const savedPoolTotal = ref(0)
const savedPoolPageSize = 20
let savedPoolItemsRequest = 0
const candidateDetails = ref<CandidateDetail | null>(null)
const candidateDetailsCloseButton = ref<HTMLButtonElement | null>(null)
let candidateDetailsReturnFocus: HTMLElement | null = null
const selectedLocalPoolBatchId = ref<string | null>(null)
const selectedLocalPoolCategoryTree = ref<CategoryNode[] | null>(null)
const localPoolSourceLabels: Record<string, string> = {
  hotSpot: '热点趋势', allianceRank: '联盟榜单', selectionSquare: '选品广场'
}
const candidateMetricLabels: Record<string, string> = {
  salesAmount: '销售额', salesCount: '销量', conversionRate: '转化率',
  views: '浏览量', commissionRate: '佣金率', experienceScore: '体验分'
}
const candidateCategoryLevelLabels: Record<CategoryLevel, string> = {
  level1: '一级', level2: '二级', level3: '三级'
}
const savedPoolPageCount = computed(() => Math.max(1, Math.ceil(savedPoolTotal.value / savedPoolPageSize)))

function candidateDetailValue(value: unknown, suffix = ''): string {
  if (value === null || value === undefined || value === '') return '未采集'
  if (typeof value === 'number') return Number.isFinite(value) ? `${value.toLocaleString('zh-CN')}${suffix}` : '未采集'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return value.length ? value.map(String).join('、') : '未采集'
  return `${String(value)}${suffix}`
}

function candidateDetailDate(value: string | undefined): string {
  if (!value) return '未采集'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '未采集' : date.toLocaleString('zh-CN')
}

const candidateDetailGroups = computed(() => {
  const item = candidateDetails.value
  if (!item) return []
  const sourceKeys = new Set<string>([item.sourceKey ?? '', ...(item.sources ?? []),
    ...(item.sourceRecords ?? []).map((record) => record.sourceKey)].filter(Boolean))
  const sources = [...sourceKeys].map((source) => localPoolSourceLabels[source] ?? source).join('、')
  const sourceRecords = (item.sourceRecords ?? []).map((record) => {
    const source = localPoolSourceLabels[record.sourceKey] ?? record.sourceKey
    const parts = [source, record.rankBoard, record.sourceCategoryName,
      record.categoryPath?.length ? record.categoryPath.join(' / ') : ''].filter(Boolean)
    return parts.join(' · ')
  }).join('；')
  const experienceScore = item.dataMetrics?.experienceScore ?? item.shopScore
  const trend = item.trendRising === undefined ? '未采集' : item.trendRising ? '走势上升' : '未确认上升'
  const selectedCategories = item.sourceCategorySelection
    ? ['level1', 'level2', 'level3'].flatMap((level) => {
      const ids = item.sourceCategorySelection?.[level as CategoryLevel] ?? []
      const label = candidateCategoryLevelLabels[level as CategoryLevel]
      return ids.length ? [`${label}: ${ids.join('、')}`] : []
    }).join('；') : ''
  const groups = [
    { title: '基本信息', fields: [
      { label: '商品名称', value: candidateDetailValue(item.title) },
      { label: '商品 ID', value: candidateDetailValue(item.productId) },
      { label: '推广 ID', value: candidateDetailValue(item.commodityId) },
      { label: '候选状态', value: candidateDetailValue(item.status) }
    ] },
    { title: '经营指标', fields: [
      { label: '价格', value: candidateDetailValue(item.price, ' 元') },
      { label: '月销量', value: candidateDetailValue(item.monthSales) },
      { label: '销售额', value: candidateDetailValue(item.saleAmount, ' 元') },
      { label: '预估销售额', value: candidateDetailValue(item.estimatedSalesAmount, ' 元') },
      { label: '佣金率', value: candidateDetailValue(item.commissionRate, '%') },
      { label: '转化率', value: candidateDetailValue(item.conversionRate) },
      { label: '浏览量', value: candidateDetailValue(item.views) },
      { label: '体验分', value: candidateDetailValue(experienceScore) },
      { label: '走势', value: trend },
      { label: '走势数据已确认', value: candidateDetailValue(item.trendKnown) },
      { label: '走势状态', value: candidateDetailValue(item.trendStatus) },
      { label: '走势斜率', value: candidateDetailValue(item.trendSlope) },
      { label: '走势数据点', value: candidateDetailValue(item.trendPoints) }
    ] },
    { title: '来源与类目', fields: [
      { label: '来源', value: candidateDetailValue(sources) },
      { label: '来源记录', value: candidateDetailValue(sourceRecords) },
      { label: '页面类目', value: candidateDetailValue(item.sourceCategoryName ?? item.sourceCategoryId) },
      { label: '页面类目 ID 路径', value: candidateDetailValue(item.sourceCategoryIds) },
      { label: '商品类目 ID 路径', value: candidateDetailValue(item.categoryIds) },
      { label: '末级类目 ID', value: candidateDetailValue(item.leafCategoryId) },
      { label: '类目状态', value: candidateDetailValue(item.categoryStatus) },
      { label: '类目依据', value: candidateDetailValue(item.categorySource) },
      { label: '页面选定类目', value: candidateDetailValue(selectedCategories) },
      { label: '配额规则 ID', value: candidateDetailValue(item.quotaRuleId) }
    ] },
    { title: '采集与分析', fields: [
      { label: '采集时间', value: candidateDetailDate(item.capturedAt) },
      { label: '采集账号 ID', value: candidateDetailValue(item.collectedByAccountId) },
      { label: '详情读取时间', value: candidateDetailDate(item.detailCapturedAt) },
      { label: '池内顺序', value: candidateDetailValue(item.poolRank) },
      { label: '统一分', value: candidateDetailValue(item.unifiedScore ?? item.analysisScore) },
      { label: '分析状态', value: candidateDetailValue(item.analysisStatus) },
      { label: '数据覆盖', value: item.analysisCoverage === undefined ? '未采集' : `${item.analysisCoverage}/${item.analysisMetricCount ?? '?'}` },
      { label: '分析依据', value: candidateDetailValue(item.analysisReasons) },
      { label: '原候选池批次', value: candidateDetailValue(item.reusedFromBatchId) }
    ] }
  ]
  if (item.dataMetrics && Object.keys(item.dataMetrics).length) {
    groups[3].fields.push(...Object.entries(item.dataMetrics).map(([label, value]) => ({
      label: `已存指标 · ${candidateMetricLabels[label] ?? label}`, value: candidateDetailValue(value)
    })))
  }
  if (item.metricSources && Object.keys(item.metricSources).length) {
    groups[3].fields.push(...Object.entries(item.metricSources).map(([label, source]) => ({
      label: `指标来源 · ${candidateMetricLabels[label] ?? label}`, value: candidateDetailValue(source)
    })))
  }
  return groups
})

function openCandidateDetails(item: CandidateDetail, event: MouseEvent): void {
  candidateDetailsReturnFocus = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  candidateDetails.value = item
  void nextTick(() => candidateDetailsCloseButton.value?.focus())
}

function closeCandidateDetails(): void {
  candidateDetails.value = null
  const returnFocus = candidateDetailsReturnFocus
  candidateDetailsReturnFocus = null
  void nextTick(() => returnFocus?.focus())
}

async function loadSavedPoolItems(page = 1): Promise<void> {
  const batchId = savedPoolBrowserBatchId.value
  if (!batchId) return
  const request = ++savedPoolItemsRequest
  const nextPage = Math.max(1, page)
  if (nextPage === 1) savedPoolAppliedSearch.value = savedPoolSearch.value.trim()
  savedPoolItemsLoading.value = true
  savedPoolItemsError.value = ''
  try {
    const result = await runRecovery(['batch', 'pool-items', '--batch-id', batchId,
      '--query', savedPoolAppliedSearch.value, '--limit', String(savedPoolPageSize),
      '--offset', String((nextPage - 1) * savedPoolPageSize)])
    if (request !== savedPoolItemsRequest || savedPoolBrowserBatchId.value !== batchId) return
    savedPoolItems.value = (Array.isArray(result.rows) ? result.rows : []).filter((item): item is CandidateDetail =>
      Boolean(item && typeof item === 'object' && typeof item.productId === 'string' && typeof item.title === 'string'))
    savedPoolTotal.value = Math.max(0, Number(result.total) || 0)
    savedPoolPage.value = nextPage
  } catch (cause) {
    if (request !== savedPoolItemsRequest || savedPoolBrowserBatchId.value !== batchId) return
    savedPoolItems.value = []
    savedPoolTotal.value = 0
    savedPoolItemsError.value = `读取候选池商品失败：${String(cause instanceof Error ? cause.message : cause)}`
  } finally {
    if (request === savedPoolItemsRequest) savedPoolItemsLoading.value = false
  }
}

function toggleSavedPoolBrowser(pool: SavedLocalCandidatePool): void {
  if (savedPoolBrowserBatchId.value === pool.batchId) {
    savedPoolBrowserBatchId.value = null
    savedPoolItemsRequest += 1
    return
  }
  savedPoolBrowserBatchId.value = pool.batchId
  savedPoolSearch.value = ''
  savedPoolAppliedSearch.value = ''
  savedPoolItems.value = []
  savedPoolTotal.value = 0
  savedPoolPage.value = 1
  void loadSavedPoolItems(1)
}

async function loadSavedLocalPools(): Promise<void> {
  if (savedLocalPoolLoading.value) return
  savedLocalPoolLoading.value = true
  try {
    const result = await runRecovery(['batch', 'pool-list', '--limit', '10'])
    savedLocalPools.value = Array.isArray(result.pools) ? result.pools as SavedLocalCandidatePool[] : []
    if (savedLocalPools.value.some((pool) => pool.recentCount > 0) &&
        error.value.includes('请先设置候选池目标数量')) {
      error.value = ''
      notice.value = '本机已有可复用候选池，可在下方查看商品并选择“使用此池开始选品”。'
    }
  } catch (cause) {
    savedLocalPools.value = []
    runLogWarning.value = `读取本机历史候选池失败：${String(cause instanceof Error ? cause.message : cause)}`
  } finally {
    savedLocalPoolLoading.value = false
  }
}
async function ensureSelectionUiData(): Promise<void> {
  if (selectionUiDataReady.value) return
  if (!selectionUiDataPromise) {
    selectionUiDataPromise = (async () => {
      const failures: string[] = []
      try {
        await loadAccountSettings()
      } catch (cause) {
        failures.push(String(cause instanceof Error ? cause.message : cause))
      }
      try {
        const results = await Promise.allSettled([reloadCatalog(), loadCachedFilterCatalog()])
        for (const result of results) {
          if (result.status === 'rejected') {
            failures.push(String(result.reason instanceof Error ? result.reason.message : result.reason))
          }
        }
        if (!props.dialogOnly) await loadSavedLocalPools()
      } finally {
        if (failures.length) error.value = failures.join('；')
        selectionUiDataReady.value = true
        selectionUiDataPromise = null
      }
    })()
  }
  await selectionUiDataPromise
}
function localPoolSourcePercent(pool: SavedLocalCandidatePool, count: number): number {
  return pool.count > 0 ? Math.max(0, Math.min(100, Math.round(count / pool.count * 100))) : 0
}
async function useSavedLocalPool(pool: SavedLocalCandidatePool): Promise<void> {
  if (busy.value || savedLocalPoolLoading.value) return
  if (recoveryBatch.value && ['in_progress', 'needs_retry'].includes(recoveryBatch.value.state)) {
    error.value = '当前仍有待恢复批次，请先处理该批次；本机历史池不会删除。'
    return
  }
  savedLocalPoolLoading.value = true
  error.value = ''
  try {
    await loadAccounts()
    const enabled = accounts.value.filter((account) => account.status === 'active' && isSelectionAccountInScope(account.id))
    if (!enabled.length) throw new Error('当前没有启用的消费账号，不能从候选池开始选品。')
    const result = await runRecovery(['batch', 'pool-settings', '--batch-id', pool.batchId])
    const currentPool = result.pool as SavedLocalCandidatePool | undefined
    const savedSettings = result.settingsSnapshot as Record<string, unknown> | undefined
    if (!currentPool || currentPool.recentCount < 1 || !savedSettings || !Array.isArray(savedSettings.categoryTree)) {
      throw new Error('所选候选池没有 24 小时内可复用的商品，请重新采集。')
    }
    const poolSettingKeys = [
      'candidatePoolTarget', 'candidatePoolMode', 'candidatePoolQuotas', 'commissionRateRule',
      'selectionMode', 'categoryMode', 'categorySelection', 'excludedCategorySelection',
      'filters', 'poolPriority', 'poolFallback', 'sources', 'thresholds', 'trendPreference',
      'bannedWords', 'bannedWordsSeedVersion'
    ]
    const editable = global as unknown as Record<string, unknown>
    for (const key of poolSettingKeys) {
      if (key in savedSettings) editable[key] = JSON.parse(JSON.stringify(savedSettings[key]))
    }
    global.candidateCollectorAccountIds = [enabled[0].id]
    bannedWordsText.value = global.bannedWords.join('\n')
    commissionRuleMigrationRequired.value = false
    await saveGlobal()
    if (error.value) return
    if (batchCandidatePoolFingerprint(global as unknown as Record<string, unknown>) !== result.filterFingerprint) {
      throw new Error('保存后的筛选条件与历史候选池不一致，已停止启动；请检查设置后重新选择候选池。')
    }
    selectedLocalPoolBatchId.value = pool.batchId
    selectedLocalPoolCategoryTree.value = savedSettings.categoryTree as CategoryNode[]
    selectionSettingsDialogOpen.value = false
    notice.value = `已选择本机候选池 ${currentPool.recentCount}/${currentPool.target}；将先按商品 ID 给当前启用账号核验，缺额再补采。`
    void startSelectionFromButton().catch((cause) => {
      error.value = String(cause instanceof Error ? cause.message : cause)
    })
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    savedLocalPoolLoading.value = false
  }
}
function formatQuotaRule(rule: CandidatePoolQuotaRule): string {
  const source = rule.sourceKey === 'allianceRank' ? `联盟榜单·${rule.rankBoard}` :
    rule.sourceKey === 'hotSpot' ? '选品热点' : '选品广场'
  if (!rule.categoryPath.length) return `${source} · 全部允许类目`
  const category = quotaCategoryOptions.value.find((item) => item.path.join('|') === rule.categoryPath.join('|'))
  return `${source} · ${category?.label ?? rule.categoryId}`
}
function quotaProgressLabel(ruleId: string): string {
  const rule = activeQuotaPool.value?.quotaRules?.find((item) => item.id === ruleId)
  return rule ? formatQuotaRule(rule) : ruleId
}
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

function clearCandidatePoolResumeTimer(): void {
  if (candidatePoolResumeTimer !== null) window.clearTimeout(candidatePoolResumeTimer)
  candidatePoolResumeTimer = null
}

function hasStaleReadyPoolAccount(batch: SelectionRecoveryBatch, pool: BatchCandidatePool): boolean {
  if (batch.state !== 'needs_retry' || pool.status !== 'ready') return false
  const availableIds = new Set(pool.rows.map((row) => row.productId))
  return batch.accounts.some((account) => {
    if (account.state !== 'needs_retry' || !account.candidates?.length) return false
    const completed = account.completed ?? []
    return completed.every((item) => item.skipped === true &&
      item.cartAttempted !== true && item.cartAdded !== true) &&
      account.candidates.every((item) => !availableIds.has(item.productId))
  })
}

function candidatePoolRateLimitRetryAt(count: number): string {
  const minutes = Math.min(15, 2 ** Math.min(Math.max(0, count - 1), 4))
  return new Date(Date.now() + minutes * 60_000).toISOString()
}

function pauseCandidatePoolAfterRateLimit(
  sourceState: BatchCandidatePool['sourceState'], collectorIds: number[]
): string {
  const count = Math.min(5, Math.max(0, ...collectorIds.map((id) =>
    sourceState.collectors[String(id)]?.rateLimitCount ?? 0
  )) + 1)
  const retryAt = candidatePoolRateLimitRetryAt(count)
  for (const id of collectorIds) {
    const collector: BatchCandidatePool['sourceState']['collectors'][string] =
      sourceState.collectors[String(id)] ?? { tabOffsets: {}, samePoolCount: 0 }
    collector.rateLimitCount = count
    collector.nextRetryAt = retryAt
    sourceState.collectors[String(id)] = collector
  }
  return retryAt
}

async function canResumePendingCandidatePool(batch: SelectionRecoveryBatch, pool: BatchCandidatePool): Promise<boolean> {
  if (pool.sourceState.autoResumeAllowed !== undefined) return pool.sourceState.autoResumeAllowed
  // 旧检查点没有此标记；只在最近一次采集失败属于可自动恢复的情况时接管。
  try {
    const result = await runRecovery(['batch', 'logs', '--batch-id', batch.batchId])
    const logs = Array.isArray(result.runLogs) ? result.runLogs as SelectionRunLog[] : []
    if (logs.at(-1)?.code !== 'WARNING_CANDIDATE_POOL_TARGET_UNMET') return false
    const failure = [...logs].reverse().find((entry) =>
      ['WARNING_CANDIDATE_COLLECTOR_SKIPPED', 'WARNING_CANDIDATE_POOL_QUOTA_READ'].includes(entry.code)
    )
    if (!failure) return true
    return !/验证码|安全验证|需要登录|未登录|环境危险|环境异常|DETAIL_ENVIRONMENT_RISK_REQUIRED/i.test(
      `${failure.message} ${failure.detail}`
    )
  } catch {
    return false
  }
}

function scheduleCandidatePoolResume(batch: SelectionRecoveryBatch, pool: BatchCandidatePool): void {
  clearCandidatePoolResumeTimer()
  const readyAccountRecovery = hasStaleReadyPoolAccount(batch, pool)
  if (props.dialogOnly || manualPauseRequested.value || batchIsManuallyPaused(batch) || batch.state !== 'needs_retry' ||
      (pool.status !== 'collecting' && !readyAccountRecovery) ||
      (pool.status === 'collecting' && pool.sourceState.autoResumeAllowed === false)) return
  const retryTimes = Object.values(pool.sourceState?.collectors ?? {}).flatMap((collector) => {
    const collectorRetry = collector.nextRetryAt ? Date.parse(collector.nextRetryAt) : NaN
    return Number.isFinite(collectorRetry) && collectorRetry > Date.now()
      ? [collector.nextRetryAt]
      : Object.values(collector.ruleStates ?? {}).map((rule) => rule.nextRetryAt)
  }).map((value) => value ? Date.parse(value) : NaN)
    .filter((value) => Number.isFinite(value) && value > Date.now())
  const retryAt = readyAccountRecovery ? Date.now() + 5_000 :
    retryTimes.length ? Math.min(...retryTimes) : Date.now() + 2 * 60_000
  const delayMs = Math.max(5_000, Math.min(30 * 60_000, retryAt - Date.now()))
  candidatePoolResumeTimer = window.setTimeout(() => {
    candidatePoolResumeTimer = null
    if (selectionViewUnmounted || recoveryBatch.value?.batchId !== batch.batchId) return
    if (loginPreflight.value) return
    if (busy.value || selectionCooldownRemaining.value > 0) {
      scheduleCandidatePoolResume(batch, pool)
      return
    }
    void (async () => {
      const latest = (await runRecovery(['batch', 'latest'])).batch as SelectionRecoveryBatch | null
      const latestPool = latest?.payload.candidatePool as BatchCandidatePool | undefined
      if (!latest || latest.batchId !== batch.batchId || latest.state !== 'needs_retry' ||
          batchIsManuallyPaused(latest) || manualPauseRequested.value ||
          !latestPool || (latestPool.status !== 'collecting' && !hasStaleReadyPoolAccount(latest, latestPool))) return
      if (latestPool.status === 'collecting' && !await canResumePendingCandidatePool(latest, latestPool)) return
      const pendingIds = latest.accounts.filter((item) => item.state !== 'completed').map((item) => item.accountId)
      if (!pendingIds.length) return
      recoveryBatch.value = latest
      showSavedPoolCheckpoint(latest)
      recoverySelectionBatchId.value = latest.batchId
      recoverySelectedAccountIds.value = pendingIds
      automaticPoolResumeBatchId = latest.batchId
      void startSelection().then((started) => {
        if (started) return
        automaticPoolResumeBatchId = null
        if (!loginPreflight.value) scheduleCandidatePoolResume(latest, latestPool)
      }).catch((cause) => {
        automaticPoolResumeBatchId = null
        error.value = `候选池自动续采启动失败：${safeCandidateCollectorErrorDetail(cause)}`
        if (!loginPreflight.value) scheduleCandidatePoolResume(latest, latestPool)
      })
    })().catch((cause) => {
      error.value = `候选池自动续采准备失败：${safeCandidateCollectorErrorDetail(cause)}`
      scheduleCandidatePoolResume(batch, pool)
    })
  }, delayMs)
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
  if (manualPausePending()) {
    clearAutoResumeTimer()
    return false
  }
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
    if (manualPausePending()) return
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

function flushSelectionProgressUpdate(): void {
  if (selectionProgressFlushTimer !== null) window.clearTimeout(selectionProgressFlushTimer)
  selectionProgressFlushTimer = null
  const update = pendingSelectionProgressUpdate
  pendingSelectionProgressUpdate = null
  if (update && props.active) update()
}

function queueSelectionProgressUpdate(update: () => void): void {
  pendingSelectionProgressUpdate = update
  if (!props.active || selectionProgressFlushTimer !== null) return
  // 多个平台进度事件合并为每 250ms 最多一次 UI 更新，减少整页虚拟 DOM 比对。
  selectionProgressFlushTimer = window.setTimeout(flushSelectionProgressUpdate, 250)
}
const candidates = ref<SelectionCandidate[]>([])
const categoryRecommendations = ref<Array<{ categoryId: string; highScoreCount: number; averageScore: number }>>([])
const previewAccountId = ref<number | null>(null)
const candidateSelectionCustomized = ref(false)
const previewCandidatePool = ref<{
  createdByAccountId: number
  filterFingerprint: string
  visibilityFingerprint: string
  dataCapturedAt: string
  plan: AccountCandidatePlan
} | null>(null)
const previewCandidatePoolCount = ref(0)
const accountId = ref<number | null>(props.initialAccountId)
const activeTab = ref<'通用设置' | '账号个性化'>(props.initialAccountId === null ? '通用设置' : '账号个性化')
const selectionSettingsDialogOpen = ref(false)
const categories = ref<CategoryNode[]>([])
const liveCategoryRoots = ref<CategoryNode[]>([])
const filterGroups = ref<Record<string, FilterField[]>>({})
const featureOptions = ref<string[]>([])
const filterCatalogData = ref<Record<string, unknown>>({})
const filterCatalogUpdatedAt = ref('')
const defaultDownloadRoot = ref('')
const browserVisible = ref(false)
const settingsScrollRef = ref<HTMLElement | null>(null)
function jumpToSelectionSection(sectionId: string): void {
  const container = settingsScrollRef.value
  const target = container?.querySelector<HTMLElement>(`#${sectionId}`)
  if (!container || !target) return
  if (target instanceof HTMLDetailsElement) target.open = true
  if (sectionId === 'account-filters') {
    const nested = target.querySelector<HTMLDetailsElement>('details.filter-card')
    if (nested) nested.open = true
  }
  container.scrollTo({
    top: container.scrollTop + target.getBoundingClientRect().top - container.getBoundingClientRect().top - 44,
    behavior: 'smooth'
  })
}
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
  excludedCategorySelection: { level1: [] as string[], level2: [] as string[], level3: [] as string[] },
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
    let timedOut = false
    let timeoutId: number | null = null
    const pendingEvents: EngineEventPayload[] = []
    let off: () => void = () => undefined
    const finish = (callback: () => void): void => {
      if (settled) return
      settled = true
      if (timeoutId !== null) window.clearTimeout(timeoutId)
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
    timeoutId = window.setTimeout(() => {
      timedOut = true
      if (taskId) void window.api.engine.cancel(taskId).catch(() => undefined)
      const operation = script === 'selection_settings.py' && args.includes('quota')
        ? '读取账号剩余配额'
        : script === 'publish_queue.py' && args.includes('product-locks')
          ? '检查商品发布记录'
          : script === 'selection_recovery.py'
            ? '读写选品批次记录'
            : script
      finish(() => reject(new Error(`${operation}超时（${Math.round(timeoutMs / 1000)} 秒）；已请求停止引擎任务，请先检查选品记录再重试。`)))
    }, timeoutMs)
    // Electron IPC 使用结构化克隆；Vue 的 reactive/ref 对象不能直接跨进程传递。
    const plainStdinJson = stdinJson === undefined ? undefined : JSON.parse(JSON.stringify(stdinJson)) as unknown
    void window.api.engine.run({ script, args, stdinJson: plainStdinJson }).then((reply) => {
      if (!reply.taskId) finish(() => reject(new Error(reply.error ?? '引擎任务启动失败')))
      else {
        taskId = reply.taskId
        if (timedOut) {
          void window.api.engine.cancel(taskId).catch(() => undefined)
          return
        }
        pendingEvents.splice(0).forEach(handleEvent)
      }
    }).catch((cause) => finish(() => reject(cause)))
  })
}

interface ProductPublishLock {
  productId: string
  productName?: string
  taskIds: number[]
  queueStatuses: string[]
  platformStatuses: string[]
}

async function loadProductPublishLocks(accountId: number, productIds: readonly string[]): Promise<Map<string, ProductPublishLock>> {
  const uniqueProductIds = [...new Set(productIds.map(String).map((value) => value.trim()).filter(Boolean))]
  if (!uniqueProductIds.length) return new Map()
  const result = await runEngine('publish_queue.py', [
    '--db', DB_PATH, 'product-locks', '--account-id', String(accountId),
    '--product-ids-json', JSON.stringify(uniqueProductIds)
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
  await recovery?.logEvents(items.map((item) => {
    const productId = String(item.productId ?? '')
    const taskIds = Array.isArray(item.publishQueueTaskIds) ? item.publishQueueTaskIds.map(String) : []
    return {
      stage: '选品去重', code: 'DOUYIN_PRODUCT_ALREADY_QUEUED',
      message: conciseFailureMessage(item.status),
      detail: `账号 ${accountId} 的商品 ${productId} 已关联发布任务：${taskIds.join(', ')}`,
      productId,
      productTitle: String(item.title ?? '')
    }
  }))
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

function normalizeExcludedCategorySelection(
  selection: { level1: string[]; level2: string[]; level3: string[] }, tree: CategoryNode[]
): { level1: string[]; level2: string[]; level3: string[] } {
  const levels = [tree, tree.flatMap((root) => root.children), tree.flatMap((root) => root.children.flatMap((second) => second.children))]
  const resolve = (id: string, options: CategoryNode[]): string => {
    const exact = options.find((item) => item.id === id)
    if (exact) return exact.id
    const matches = options.filter((item) => sameCategoryId(item.id, id))
    return matches.length === 1 ? matches[0].id : id
  }
  return {
    level1: [...new Set(selection.level1.map((id) => resolve(id, levels[0])))],
    level2: [...new Set(selection.level2.map((id) => resolve(id, levels[1])))],
    level3: [...new Set(selection.level3.map((id) => resolve(id, levels[2])))]
  }
}

function normalizeCurrentCategorySelections(): void {
  global.categorySelection = normalizeCategorySelection(global.categorySelection, categories.value)
  global.excludedCategorySelection = normalizeExcludedCategorySelection(global.excludedCategorySelection, categories.value)
  accountDraft.categorySelection = normalizeCategorySelection(accountDraft.categorySelection, categories.value)
  accountDraft.excludedCategorySelection = normalizeExcludedCategorySelection(accountDraft.excludedCategorySelection, categories.value)
}

function pruneExcludedCategorySelectionToCatalog(
  selection: { level1: string[]; level2: string[]; level3: string[] }, tree: CategoryNode[]
): { selection: { level1: string[]; level2: string[]; level3: string[] }; removed: number } {
  const normalized = normalizeExcludedCategorySelection(selection, tree)
  const levels = [tree, tree.flatMap((root) => root.children), tree.flatMap((root) => root.children.flatMap((second) => second.children))]
  const valid = (id: string, options: CategoryNode[]): boolean => options.some((item) => item.id === id)
  const next = {
    level1: normalized.level1.filter((id) => valid(id, levels[0])),
    level2: normalized.level2.filter((id) => valid(id, levels[1])),
    level3: normalized.level3.filter((id) => valid(id, levels[2]))
  }
  const before = selection.level1.length + selection.level2.length + selection.level3.length
  const after = next.level1.length + next.level2.length + next.level3.length
  return { selection: next, removed: Math.max(0, before - after) }
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
  const globalExcludedResult = pruneExcludedCategorySelectionToCatalog(global.excludedCategorySelection, categories.value)
  const accountResult = pruneCategorySelectionToCatalog(accountDraft.categorySelection, categories.value)
  const accountExcludedResult = pruneExcludedCategorySelectionToCatalog(accountDraft.excludedCategorySelection, categories.value)
  global.categorySelection = globalResult.selection
  global.excludedCategorySelection = globalExcludedResult.selection
  accountDraft.categorySelection = accountResult.selection
  accountDraft.excludedCategorySelection = accountExcludedResult.selection
  return globalResult.removed + globalExcludedResult.removed + accountResult.removed + accountExcludedResult.removed
}

async function loadGlobal(): Promise<void> {
  const result = await runEngine('selection_settings.py', ['--db', DB_PATH, 'get'])
  const settings = (result.settings ?? {}) as Record<string, unknown>
  const savedThresholds = (settings.thresholds ?? {}) as Record<string, unknown>
  commissionRuleMigrationRequired.value = savedThresholds.commissionRate != null && settings.commissionRateRule !== 'minimum'
  Object.assign(global, cloneDefault(), settings)
  global.commissionRateRule = 'minimum'
  global.candidatePoolTarget = Number.isInteger(Number(settings.candidatePoolTarget)) &&
    Number(settings.candidatePoolTarget) >= 1 && Number(settings.candidatePoolTarget) <= 2000
    ? Number(settings.candidatePoolTarget) : cloneDefault().candidatePoolTarget
  global.candidatePoolMode = settings.candidatePoolMode === 'quota' ? 'quota' : 'total'
  global.candidatePoolQuotas = Array.isArray(settings.candidatePoolQuotas)
    ? settings.candidatePoolQuotas.filter((item): item is CandidatePoolQuotaRule =>
      Boolean(item && typeof item === 'object' && typeof item.id === 'string' && typeof item.categoryId === 'string' && Array.isArray(item.categoryPath)))
    : []
  global.candidateCollectorAccountIds = Array.isArray(settings.candidateCollectorAccountIds)
    ? [...new Set(settings.candidateCollectorAccountIds.map(Number).filter((id) => Number.isSafeInteger(id) && id > 0))].slice(0, 2)
    : []
  global.selectionMode = settings.selectionMode === 'category' ? 'category' : 'general'
  global.categoryMode = settings.categoryMode === 'manual' ? 'manual' : 'auto'
  const cat = (settings.categorySelection ?? {}) as Record<string, string[]>
  global.categorySelection = {
    level1: [...(cat.level1 ?? [])], level2: [...(cat.level2 ?? [])], level3: [...(cat.level3 ?? [])]
  }
  const excludedCat = (settings.excludedCategorySelection ?? {}) as Record<string, string[]>
  global.excludedCategorySelection = {
    level1: [...(excludedCat.level1 ?? [])], level2: [...(excludedCat.level2 ?? [])], level3: [...(excludedCat.level3 ?? [])]
  }
  const savedProductFilters = { ...((settings.filters as Record<string, Record<string, unknown>> | undefined)?.product ?? {}) }
  const savedDeliveryFilters = { ...((settings.filters as Record<string, Record<string, unknown>> | undefined)?.delivery ?? {}) }
  delete savedDeliveryFilters['佣金区间']
  delete savedProductFilters['佣金区间']
  global.filters = {
    delivery: savedDeliveryFilters,
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
  global.bannedWords = Array.from(new Set([
    ...((settings.bannedWords as string[] | undefined) ?? DEFAULT_BANNED_WORDS),
    ...SUPERSTITION_BANNED_WORDS
  ]))
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

async function refreshSelectionAccounts(announce = true): Promise<void> {
  if (busy.value) return
  busy.value = true
  if (announce) error.value = ''
  try {
    await loadAccounts()
    if (announce) {
      notice.value = `账号列表已刷新：${accounts.value.length} 个。未保存的通用设置保持不变；新账号可继承通用设置，也可单独覆盖。`
    }
  } catch (cause) {
    const message = `刷新账号列表失败：${String(cause instanceof Error ? cause.message : cause)}`
    if (announce || !error.value) error.value = message
    else notice.value = message
  } finally {
    busy.value = false
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
  const excluded = (value.excludedCategorySelection ?? global.excludedCategorySelection ?? {}) as Record<string, string[]>
  accountDraft.excludedCategorySelection = {
    level1: [...(excluded.level1 ?? [])], level2: [...(excluded.level2 ?? [])], level3: [...(excluded.level3 ?? [])]
  }
  accountDraft.categorySelection = normalizeCategorySelection(accountDraft.categorySelection, categories.value)
  accountDraft.excludedCategorySelection = normalizeExcludedCategorySelection(accountDraft.excludedCategorySelection, categories.value)
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
  if (settings?.inherited && !['in_progress', 'needs_retry'].includes(recoveryBatch.value?.state ?? '')) {
    notice.value = `账号「${accounts.value.find((a) => a.id === id)?.name ?? id}」尚未单独配置，将继承通用筛选设置。`
  }
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
const quotaCategoryOptions = computed(() => {
  const options: Array<{ id: string; path: string[]; label: string }> = []
  const visit = (nodes: CategoryNode[], path: string[], names: string[]): void => {
    for (const node of nodes) {
      const nextPath = [...path, node.id]
      const nextNames = [...names, node.name]
      options.push({ id: node.id, path: nextPath, label: nextNames.join(' / ') })
      if (nextPath.length < 3) visit(node.children ?? [], nextPath, nextNames)
    }
  }
  visit(categories.value, [], [])
  return options
})
const quotaPoolTarget = computed(() => global.candidatePoolQuotas.reduce((sum, rule) => sum + Number(rule.target || 0), 0))
function quotaCategoryOptionsForSource(sourceKey: CandidatePoolQuotaRule['sourceKey']) {
  return sourceKey === 'hotSpot'
    ? quotaCategoryOptions.value.filter((category) => category.path.length === 1)
    : quotaCategoryOptions.value
}
function sourceQuotaTarget(sourceKey: CandidatePoolQuotaRule['sourceKey']): number {
  return global.candidatePoolQuotas
    .filter((rule) => rule.sourceKey === sourceKey)
    .reduce((sum, rule) => sum + Number(rule.target || 0), 0)
}
function setSourceQuotaTarget(sourceKey: CandidatePoolQuotaRule['sourceKey'], raw: string): void {
  const target = Number(raw)
  const rule = global.candidatePoolQuotas.find((item) => item.sourceKey === sourceKey && !item.categoryPath.length)
  if (!Number.isInteger(target) || target < 0 || target > 2000) {
    error.value = '来源目标须为 0–2000 的整数。'
    return
  }
  const categoryTarget = global.candidatePoolQuotas
    .filter((item) => item.sourceKey === sourceKey && item.categoryPath.length > 0)
    .reduce((sum, item) => sum + Number(item.target || 0), 0)
  if (target < categoryTarget) {
    error.value = `该来源已设置 ${categoryTarget} 件指定品类配额，来源合计不能低于此数。`
    return
  }
  error.value = ''
  const remaining = target - categoryTarget
  if (remaining === 0) {
    if (rule) global.candidatePoolQuotas.splice(global.candidatePoolQuotas.indexOf(rule), 1)
    return
  }
  if (rule) {
    rule.target = remaining
    return
  }
  global.candidatePoolQuotas.push({
    id: crypto.randomUUID(), sourceKey, rankBoard: global.sources.rankBoard,
    categoryId: '', categoryPath: [], target: remaining
  })
}
function addQuotaRule(): void {
  const category = quotaCategoryOptions.value[0]
  if (!category) {
    error.value = '请先同步百应类目目录，再添加指定品类配额。'
    return
  }
  const rule: CandidatePoolQuotaRule = {
    id: crypto.randomUUID(), sourceKey: 'allianceRank', rankBoard: '爆款榜',
    categoryId: category.id, categoryPath: category.path, target: 10
  }
  const sourceWideIndex = global.candidatePoolQuotas.findIndex((item) =>
    item.sourceKey === rule.sourceKey && !item.categoryPath.length)
  global.candidatePoolQuotas.splice(sourceWideIndex < 0 ? global.candidatePoolQuotas.length : sourceWideIndex, 0, rule)
}
function moveQuotaRule(index: number, step: number): void {
  const next = index + step
  if (next < 0 || next >= global.candidatePoolQuotas.length) return
  const [rule] = global.candidatePoolQuotas.splice(index, 1)
  global.candidatePoolQuotas.splice(next, 0, rule)
}
function setQuotaCategory(rule: CandidatePoolQuotaRule, pathKey: string): void {
  const category = quotaCategoryOptions.value.find((item) => item.path.join('|') === pathKey)
  rule.categoryId = category?.id ?? ''
  rule.categoryPath = category?.path ?? []
}
function categoryIdMatches(left: string, right: string): boolean {
  return left === right || left.split('__RC_CASCADER_SPLIT__').at(-1) === right.split('__RC_CASCADER_SPLIT__').at(-1)
}
function validateQuotaRules(
  mode: 'total' | 'quota', rules: CandidatePoolQuotaRule[],
  sourceSettings: SelectionFilterSettings['sources'], excluded: typeof DEFAULTS.excludedCategorySelection
): number {
  if (mode === 'total') return global.candidatePoolTarget
  if (!Array.isArray(rules) || !rules.length) throw new Error('按来源和品类配额模式至少需要一条规则。')
  const excludedSecondPaths = [...excluded.level2.map((id) => ({ id, level: 2 })),
    ...excluded.level3.map((id) => ({ id, level: 3 }))].map(({ id, level }) => {
    const matches = quotaCategoryOptions.value.filter((item) => item.path.length === level && categoryIdMatches(item.id, id))
    if (matches.length !== 1) throw new Error(`排除的${level}级类目 ${id} 无法唯一定位，请刷新百应类目目录。`)
    return matches[0].path.slice(0, 2)
  })
  const seen = new Set<string>()
  let total = 0
  for (const [index, rule] of rules.entries()) {
    const name = `第 ${index + 1} 条配额`
    if (!['hotSpot', 'allianceRank', 'selectionSquare'].includes(rule.sourceKey) ||
        sourceSettings[`${rule.sourceKey}Enabled` as keyof typeof sourceSettings] !== true) throw new Error(`${name}的来源未启用。`)
    if (rule.sourceKey === 'allianceRank' && !['爆款榜', '趋势榜', '带货榜', '常销榜'].includes(rule.rankBoard)) {
      throw new Error(`${name}的榜单类型无效。`)
    }
    if (!Number.isInteger(rule.target) || rule.target < 1 || rule.target > 2000) throw new Error(`${name}的目标数须为 1–2000。`)
    const sourceWide = Array.isArray(rule.categoryPath) && rule.categoryPath.length === 0 && rule.categoryId === ''
    if (rule.sourceKey === 'hotSpot' && rule.categoryPath.length > 1) {
      throw new Error(`${name}指定了热点二级或三级类目；热点页面仅能筛选一级类目。`)
    }
    const category = sourceWide ? undefined : quotaCategoryOptions.value.find((item) => item.id === rule.categoryId &&
      item.path.join('|') === rule.categoryPath?.join('|'))
    if (!sourceWide && (!category || !category.path.length)) throw new Error(`${name}缺少可核实的百应类目 ID；请刷新类目并重新选择。`)
    if (category?.path.some((id, level) => excluded[`level${level + 1}` as keyof typeof excluded]
      .some((excludedId) => categoryIdMatches(id, excludedId)))) {
      throw new Error(`${name}选择的品类已经被排除，不能计入配额。`)
    }
    if (rule.sourceKey === 'hotSpot' && category && excludedSecondPaths.some((path) =>
      categoryIdMatches(category.path[0], path[0]))) {
      throw new Error(`${name}的热点一级类目下有二级或三级排除项；热点会跳过整个一级，不能设置该配额。`)
    }
    if (category && category.path.length >= 2 && excludedSecondPaths.some((path) =>
      categoryIdMatches(category.path[0], path[0]) && categoryIdMatches(category.path[1], path[1]))) {
      throw new Error(`${name}落在已排除的二级分支，不能计入配额。`)
    }
    const key = sourceWide ? `${rule.sourceKey}|全部允许类目` :
      `${rule.sourceKey}|${rule.sourceKey === 'allianceRank' ? rule.rankBoard : ''}|${category?.path.join('|') ?? ''}`
    if (seen.has(key)) throw new Error(`${name}与前面的来源、榜单及品类重复。`)
    seen.add(key)
    total += rule.target
  }
  if (total > 2000) throw new Error('各品类目标之和不能超过 2000 件。')
  return total
}
const level2Options = computed(() => categories.value.filter((item) => global.categorySelection.level1.includes(item.id)).flatMap((item) => item.children))
const level3Options = computed(() => level2Options.value.filter((item) => global.categorySelection.level2.includes(item.id)).flatMap((item) => item.children))
const excludedBrowseRoot = reactive({ global: '', account: '' })
const excludedBrowseSecond = reactive({ global: '', account: '' })
const excludedLevel2Options = computed(() => categories.value.find((item) => item.id === excludedBrowseRoot.global)?.children ?? [])
const excludedLevel3Options = computed(() => excludedLevel2Options.value.find((item) => item.id === excludedBrowseSecond.global)?.children ?? [])
const accountLevel2Options = computed(() => categories.value.filter((item) => accountDraft.categorySelection.level1.includes(item.id)).flatMap((item) => item.children))
const accountLevel3Options = computed(() => accountLevel2Options.value.filter((item) => accountDraft.categorySelection.level2.includes(item.id)).flatMap((item) => item.children))
const accountExcludedLevel2Options = computed(() => categories.value.find((item) => item.id === excludedBrowseRoot.account)?.children ?? [])
const accountExcludedLevel3Options = computed(() => accountExcludedLevel2Options.value.find((item) => item.id === excludedBrowseSecond.account)?.children ?? [])
function showExcludedCategoryChildren(id: string, level: CategoryLevel, scope: 'global' | 'account'): void {
  if (level === 'level1') {
    excludedBrowseRoot[scope] = id
    excludedBrowseSecond[scope] = ''
  } else if (level === 'level2') {
    excludedBrowseSecond[scope] = id
  }
}
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

function toggleExcludedCategory(id: string, level: CategoryLevel, scope: 'global' | 'account', checked: boolean): void {
  const selected = scope === 'global' ? global.excludedCategorySelection : accountDraft.excludedCategorySelection
  const current = selected[level]
  selected[level] = checked
    ? current.includes(id) ? current : [...current, id]
    : current.filter((item) => item !== id)
}

function excludedCategoryAction(level: CategoryLevel, scope: 'global' | 'account', action: 'all' | 'invert' | 'clear'): void {
  const options = level === 'level1'
    ? level1Options.value
    : level === 'level2'
      ? (scope === 'global' ? excludedLevel2Options.value : accountExcludedLevel2Options.value)
      : (scope === 'global' ? excludedLevel3Options.value : accountExcludedLevel3Options.value)
  const selected = scope === 'global' ? global.excludedCategorySelection : accountDraft.excludedCategorySelection
  const current = selected[level]
  selected[level] = action === 'clear'
    ? []
    : action === 'all'
      ? options.map((item) => item.id)
      : options.map((item) => item.id).filter((id) => !current.includes(id))
}

async function activeConsumerDemand(settings: Record<string, unknown>): Promise<{ total: number; detail: string }> {
  const enabled = accounts.value.filter((account) =>
    account.status === 'active' && isSelectionAccountInScope(account.id)
  )
  let total = 0
  const detail: string[] = []
  for (const account of enabled) {
    const result = await runEngine('selection_settings.py', [
      '--db', DB_PATH, 'get', '--account-id', String(account.id)
    ])
    const saved = (result.settings ?? {}) as { account?: Record<string, unknown> }
    const accountSettings = saved.account ?? {}
    const overrides = accountSettings.filterOverrides && typeof accountSettings.filterOverrides === 'object'
      ? accountSettings.filterOverrides as Record<string, unknown> : {}
    const raw = overrides.candidateLimit ?? settings.candidateLimit ?? 5
    const target = Number.isInteger(Number(raw)) ? Math.max(1, Math.min(10, Number(raw))) : 5
    total += target
    detail.push(`${account.name} ${target}`)
  }
  return { total, detail: detail.join('、') }
}

async function saveGlobal(): Promise<void> {
  error.value = ''
  try {
    global.candidatePoolTarget = validateQuotaRules(global.candidatePoolMode, global.candidatePoolQuotas, global.sources, global.excludedCategorySelection)
    if (!Number.isInteger(global.candidatePoolTarget) || global.candidatePoolTarget < 1 || global.candidatePoolTarget > 2000) {
      throw new Error('共享候选池目标数须为 1–2000 的整数。')
    }
    const demand = await activeConsumerDemand(global)
    if (global.candidatePoolTarget < demand.total) {
      throw new Error(`候选池目标 ${global.candidatePoolTarget} 件小于已启用消费账号目标合计 ${demand.total} 件（${demand.detail || '无账号'}）。请增加池目标后保存。`)
    }
    if (global.candidateCollectorAccountIds.length > 2 || new Set(global.candidateCollectorAccountIds).size !== global.candidateCollectorAccountIds.length) {
      throw new Error('候选采集账号最多选择两个不同账号。')
    }
    if (global.selectionMode === 'category' && global.categoryMode === 'manual' && !hasSelectedCategory(global.categorySelection)) {
      throw new Error('已选择手动选类目，请至少勾选一个类目；或切回自动推荐。')
    }
    validateCommissionRange(global.thresholds.commissionRate, global.thresholds.commissionRateMax, '通用设置')
    validateCandidateSources(global.sources, '通用设置')
    validateExperienceScoreThreshold(global.thresholds.experienceScore, '通用设置')
    for (const [label, value] of Object.entries({
      '每品视频上限': global.quota.perProductVideos,
      '通用每账号商品上限': global.quota.perAccountProducts,
      '通用每账号视频上限': global.quota.perAccountVideos
    })) {
      if (!Number.isInteger(value) || value < 1) throw new Error(`${label}必须是大于 0 的整数。`)
    }
    global.execution.publishMode = '自动'
    global.commissionRateRule = 'minimum'
    global.bannedWords = Array.from(new Set([
      ...bannedWordsText.value.split(/\r?\n/).map((word) => word.trim()).filter(Boolean),
      ...SUPERSTITION_BANNED_WORDS
    ]))
    global.bannedWordsSeedVersion = 2
    bannedWordsText.value = global.bannedWords.join('\n')
    await runEngine('selection_settings.py', ['--db', DB_PATH, 'save'], JSON.parse(JSON.stringify({
      ...global, categoryTree: categories.value
    })))
    selectedLocalPoolBatchId.value = null
    selectedLocalPoolCategoryTree.value = null
    commissionRuleMigrationRequired.value = false
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

function validateCommissionRange(lower: number | null | undefined, upper: number | null | undefined, scope: string): void {
  for (const value of [lower, upper]) {
    if (value !== null && value !== undefined && (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100)) {
      throw new Error(`${scope}佣金率须为 0–100 之间的百分数。`)
    }
  }
  if (lower != null && upper != null && lower > upper) throw new Error(`${scope}最低佣金率不能超过最高佣金率。`)
}

function commissionBounds(settings: Record<string, unknown>): [number | null, number | null] {
  const thresholds = (settings.thresholds ?? {}) as Record<string, unknown>
  const normalize = (value: unknown): number | null => {
    if (value === null || value === undefined || value === '') return null
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return [normalize(thresholds.commissionRate), normalize(thresholds.commissionRateMax)]
}

function commissionBoundsEqual(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const [leftMin, leftMax] = commissionBounds(left)
  const [rightMin, rightMax] = commissionBounds(right)
  return leftMin === rightMin && leftMax === rightMax
}

function validateCandidateSources(sources: SelectionFilterSettings['sources'], scope: string): void {
  if (!sources.hotSpotEnabled && !sources.allianceRankEnabled && !sources.selectionSquareEnabled) throw new Error(`${scope}至少启用一个候选来源。`)
  if (!['不限', '1-10', '10-50', '50-100', '100-500', '500-5000', '5000+'].includes(sources.rankPriceRange)) throw new Error(`${scope}榜单售价区间无效。`)
  if (!Array.isArray(sources.rankCategoryPath) || sources.rankCategoryPath.length > 3 ||
      (sources.rankCategoryPath.length > 0 && !quotaCategoryOptions.value.some((category) => category.path.join('|') === sources.rankCategoryPath.join('|')))) {
    throw new Error(`${scope}榜单分类 ID 无效；请同步类目后重新选择。`)
  }
  for (const value of [sources.hotSpotMinExposure, sources.hotSpotMinRelatedProducts]) {
    if (value != null && (!Number.isFinite(value) || value < 0)) throw new Error(`${scope}热点条件须为非负数。`)
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
  error.value = '账号路径还没保存，选品没有启动。请展开“调整筛选条件与账号设置”，点击“保存账号设置”；如果浏览器资料路径填的是视频文件夹，请先点该路径旁的“恢复默认”。'
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

async function saveAccount(): Promise<boolean> {
  if (accountId.value === null || busy.value) return false
  const targetAccountId = accountId.value
  if (!isSelectionAccountInScope(targetAccountId)) {
    error.value = '所选账号已不存在，账号设置未保存。'
    return false
  }
  busy.value = true
  error.value = ''
  try {
    if (accountDraft.browserProfilePath) {
      const directoryValidation = await window.api.dir.validateProfileDir(accountDraft.browserProfilePath)
      if (!directoryValidation.ok) {
        throw new Error(directoryValidation.error ?? '浏览器登录资料文件夹不能使用。请点“恢复默认”；视频保存位置请填写“视频保存位置”。')
      }
      const profileValidation = await window.api.browser.validateProfilePath({
        profileKey: 'account-' + targetAccountId,
        profilePath: directoryValidation.path ?? accountDraft.browserProfilePath
      })
      if (!profileValidation.ok || !profileValidation.path) {
        throw new Error(`浏览器登录资料文件夹不能使用：${profileValidation.error ?? '请点“恢复默认”后重试'}`)
      }
      accountDraft.browserProfilePath = profileValidation.path
    }

    if (accountDraft.downloadPath.trim()) {
      const downloadValidation = await window.api.dir.canonicalizePath(accountDraft.downloadPath)
      if (!downloadValidation.ok || !downloadValidation.path) {
        throw new Error(`视频保存位置不能使用：${downloadValidation.error ?? '请重新选择文件夹或点“恢复默认”'}`)
      }
      accountDraft.downloadPath = downloadValidation.path
    }
    if (accountFilterOverridesEnabled.value && (!Number.isInteger(accountFilterDraft.value.candidateLimit) || accountFilterDraft.value.candidateLimit < 1 || accountFilterDraft.value.candidateLimit > 10)) {
      throw new Error('每轮每账号新增商品目标数须为 1–10 的整数。')
    }
    if (accountFilterOverridesEnabled.value) {
      validateCommissionRange(accountFilterDraft.value.thresholds.commissionRate, accountFilterDraft.value.thresholds.commissionRateMax, '账号个性化')
      validateCandidateSources(accountFilterDraft.value.sources, '账号个性化')
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
      excludedCategorySelection: JSON.parse(JSON.stringify(accountDraft.excludedCategorySelection)),
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
        ? { filterOverrides: { ...createSelectionFilterSettings(accountFilterDraft.value, globalFilterSettings.value), commissionRateRule: 'minimum' } }
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
    return true
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
    return false
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

function openSelectionSettingsDialog(): void {
  selectionSettingsDialogOpen.value = true
  void refreshSelectionAccounts(false)
  void loadSavedLocalPools()
}

function closeSelectionSettingsDialog(): void {
  if (!busy.value) selectionSettingsDialogOpen.value = false
}

async function saveSelectionSettingsAndClose(): Promise<void> {
  if (busy.value) return
  if (activeTab.value === '通用设置') {
    await saveGlobal()
    if (!error.value) selectionSettingsDialogOpen.value = false
    return
  }
  const saved = await saveAccount()
  if (saved) selectionSettingsDialogOpen.value = false
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
  if (!accountKey.trim()) throw new Error('账号标识无效，无法读取选品登记历史。')
  const snapshot = await window.api.browser.getSelectionRegistrySnapshot(accountKey, rows.map((candidate) => ({
    productId: candidate.productId,
    leafCategoryId: candidateLeafCategoryId(candidate)
  })))
  return filterRowsByLocalRegistry(rows, snapshot, targetCount, selectionMode)
}

function filterRowsByLocalRegistry(
  rows: SelectionCandidate[],
  snapshot: {
    usedProductIds: string[]
    accountUsedCategoryIds: string[]
    categoryUsage: Record<string, number>
    mode: 'local' | 'server'
  },
  targetCount: number,
  selectionMode: 'general' | 'category',
  deferCategoryToConsumer = false
): {
  candidates: SelectionCandidate[]
  excluded: Array<{ candidate: SelectionCandidate; reason: string }>
  recommendations: Array<{ categoryId: string; highScoreCount: number; averageScore: number }>
} {
  const categoryMode = selectionMode === 'category'
  const { eligible, excluded } = filterCandidatesByRegistry(rows, snapshot, deferCategoryToConsumer, categoryMode)
  if (!categoryMode || deferCategoryToConsumer) {
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
    hotSpot: '选品热点',
    allianceRank: '带货商品榜',
    selectionSquare: '选品广场'
  }
  return (candidate.sources ?? []).map((source) => labels[source]).filter(Boolean).join('、')
}

function registryAccountKey(accountId: number): string {
  const douyinId = String(accounts.value.find((account) => account.id === accountId)?.douyin_id ?? '').trim()
  return /^[-\w.]{1,96}$/.test(douyinId) ? `local:${accountId}|douyin:${douyinId}` : `local:${accountId}`
}

async function occupiedPoolProductIds(rows: SelectionCandidate[], collectorId: number, batchId?: string): Promise<Set<string>> {
  if (!rows.length) return new Set()
  const snapshot = await window.api.browser.getSelectionRegistrySnapshot(registryAccountKey(collectorId), rows.map((row) => ({
    productId: row.productId,
    leafCategoryId: candidateLeafCategoryId(row)
  })))
  if (batchId) {
    const status = snapshot as typeof snapshot & {
      warning?: unknown
      syncConflicts?: Array<{ productId?: unknown; reason?: unknown }>
    }
    if (status.warning) {
      await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', '0'], {
        stage: '选品登记同步', code: 'WARNING_REGISTRY_OFFLINE_MODE',
        message: '共享登记服务暂不可达，候选池使用本机登记结果继续。',
        detail: String(status.warning)
      })
    }
    if (status.syncConflicts?.length) {
      const conflicts = status.syncConflicts.slice(0, 50)
      await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', '0'], {
        stage: '选品登记同步', code: 'WARNING_REGISTRY_OFFLINE_CONFLICT',
        message: `服务器回读到 ${status.syncConflicts.length} 个跨客户端商品冲突，已从本批候选池剔除。`,
        detail: conflicts.map((item) => `${String(item.productId ?? '')}：${String(item.reason ?? 'SERVER_CONFLICT')}`).join('；')
      })
    }
  }
  return new Set(snapshot.usedProductIds.map(String))
}

function resetHotSpotRootOnlyCursors(
  sourceState: BatchCandidatePool['sourceState'], hotRuleIds: Set<string>
): boolean {
  let changed = false
  for (const collector of Object.values(sourceState.collectors)) {
    collector.scrollPositions ??= {}
    if (collector.scrollPositions['hotSpot:rootOnlyPlanVersion'] === 1) continue
    changed = true
    collector.tabOffsets ??= {}
    delete collector.tabOffsets.hotSpot
    for (const key of Object.keys(collector.scrollPositions)) {
      if (key.startsWith('hotSpot:')) delete collector.scrollPositions[key]
    }
    collector.scrollPositions['hotSpot:rootOnlyPlanVersion'] = 1
    for (const ruleId of hotRuleIds) {
      const state = collector.ruleStates?.[ruleId]
      if (!state) continue
      state.categoryTabOffset = 0
      state.scrollPosition = 0
      state.detailAttemptedIds = []
      state.samePoolCount = 0
      state.exhausted = false
      state.nextRetryAt = undefined
    }
  }
  return changed
}

function batchCandidatePoolFingerprint(snapshot: Record<string, unknown>): string {
  if (snapshot.candidatePoolMode === 'quota') {
    return stableJson({
      mode: 'quota', rules: snapshot.candidatePoolQuotas, sources: snapshot.sources,
      thresholds: snapshot.thresholds, filters: snapshot.filters, bannedWords: snapshot.bannedWords,
      excludedCategorySelection: snapshot.excludedCategorySelection
    })
  }
  return stableJson({
    sources: snapshot.sources, thresholds: snapshot.thresholds, filters: snapshot.filters,
    bannedWords: snapshot.bannedWords, selectionMode: snapshot.selectionMode,
    categoryMode: snapshot.categoryMode, categorySelection: snapshot.categorySelection,
    excludedCategorySelection: snapshot.excludedCategorySelection,
    candidatePoolMode: snapshot.candidatePoolMode, candidatePoolQuotas: snapshot.candidatePoolQuotas
  })
}

async function collectBatchCandidatePoolByQuota(
  batch: SelectionRecoveryBatch,
  contexts: Map<number, AccountRunContext>
): Promise<{ batch: SelectionRecoveryBatch; pool: BatchCandidatePool; ready: boolean; autoResumeAllowed?: boolean }> {
  const snapshot = batch.payload.settingsSnapshot as Record<string, unknown>
  const rules = snapshot.candidatePoolQuotas as CandidatePoolQuotaRule[]
  const target = Number(snapshot.candidatePoolTarget)
  const collectorIds = (snapshot.candidateCollectorAccountIds as number[]).map(Number)
  if (!Array.isArray(rules) || !rules.length || rules.reduce((sum, rule) => sum + rule.target, 0) !== target ||
      ![1, 2].includes(collectorIds.length) || new Set(collectorIds).size !== collectorIds.length) {
    throw new Error('批次品类配额与采集账号快照无效，未开始消费账号选品。')
  }
  if (rules.some((rule) => rule.sourceKey === 'hotSpot' && rule.categoryPath.length > 1)) {
    throw new Error('旧批次包含热点二级或三级配额；热点页面只有一级入口，不能按旧配额继续采集。请新建符合当前设置的批次。')
  }
  const filterFingerprint = batchCandidatePoolFingerprint(snapshot)
  const oldPool = batch.payload.candidatePool as BatchCandidatePool | undefined
  const matches = oldPool?.mode === 'quota' && oldPool.target === target &&
    oldPool.collectorAccountIds?.join(',') === collectorIds.join(',') &&
    oldPool.filterFingerprint === filterFingerprint
  const quotaTransfers = matches && Array.isArray(oldPool?.quotaTransfers) ? structuredClone(oldPool.quotaTransfers) : []
  const effectiveTargets = new Map(rules.map((rule) => [rule.id, rule.target]))
  for (const transfer of quotaTransfers) {
    effectiveTargets.set(transfer.fromRuleId, (effectiveTargets.get(transfer.fromRuleId) ?? 0) - transfer.count)
    effectiveTargets.set(transfer.toRuleId, (effectiveTargets.get(transfer.toRuleId) ?? 0) + transfer.count)
  }
  const excludedProductIds = new Set<string>(matches && Array.isArray(oldPool?.excludedProductIds) ? oldPool.excludedProductIds : [])
  const rowsById = new Map<string, SelectionCandidate>()
  if (matches) for (const row of oldPool?.rows ?? []) {
    if (/^\d+$/.test(row.productId) && !excludedProductIds.has(row.productId) && rules.some((rule) => rule.id === row.quotaRuleId)) rowsById.set(row.productId, row)
  }
  const occupiedOldIds = await occupiedPoolProductIds([...rowsById.values()], collectorIds[0], batch.batchId)
  for (const productId of occupiedOldIds) rowsById.delete(productId)
  for (const productId of occupiedOldIds) excludedProductIds.add(productId)
  const sourceState: BatchCandidatePool['sourceState'] = matches && oldPool?.sourceState?.collectors
    ? structuredClone(oldPool.sourceState) : { collectors: {} }
  sourceState.autoResumeAllowed = true
  const legacyCategoryCursors = matches && Object.values(sourceState.collectors).some((collector) =>
    collector.scrollPositions?.['selectionSquare:categoryPlanVersion'] !== 2
  )
  if (legacyCategoryCursors) for (const collector of Object.values(sourceState.collectors)) {
    collector.tabOffsets = {}
    collector.scrollPositions = { 'selectionSquare:categoryPlanVersion': 2 }
    collector.nextRetryAt = undefined
    collector.samePoolCount = 0
    for (const state of Object.values(collector.ruleStates ?? {})) {
      state.exhausted = false
      state.nextRetryAt = undefined
      state.samePoolCount = 0
      state.scrollPosition = 0
      state.categoryTabOffset = 0
    }
  }
  const legacyHotSpotCursors = matches && resetHotSpotRootOnlyCursors(
    sourceState, new Set(rules.filter((rule) => rule.sourceKey === 'hotSpot').map((rule) => rule.id))
  )
  const countFor = (ruleId: string): number => [...rowsById.values()].filter((row) => row.quotaRuleId === ruleId).length
  const ready = (): boolean => rules.every((rule) => countFor(rule.id) >= (effectiveTargets.get(rule.id) ?? rule.target))
  const savePool = async (newRows: SelectionCandidate[], collectorId: number): Promise<void> => {
    const capturedAt = new Date().toISOString()
    const saved = await runRecovery(['batch', 'pool-save', '--batch-id', batch.batchId], {
      mode: 'quota', quotaRules: rules, quotaTransfers, target, collectorAccountIds: collectorIds,
      collectorAccountId: collectorId, capturedAt, filterFingerprint,
      rows: newRows.map((row) => ({
        ...row, collectedByAccountId: row.collectedByAccountId ?? collectorId,
        capturedAt: row.capturedAt ?? capturedAt
      })),
      excludedProductIds: [...excludedProductIds], sourceState
    })
    const result = saved.pool as { count?: number; status?: string } | undefined
    if (Number(result?.count) !== rowsById.size) throw new Error('品类配额候选池落盘数量不一致，未进入消费阶段。')
    showConfirmedPoolSave(batch.batchId, target, 'quota', result?.status === 'ready' ? 'ready' : 'collecting',
      [...rowsById.values()], rules, quotaTransfers)
  }
  if (occupiedOldIds.size || legacyCategoryCursors || legacyHotSpotCursors) await savePool([], collectorIds[0])
  if (!ready() && collectorIds.some((id) => !contexts.has(id))) {
    throw new Error('采集账号缺少可用的本地资源，已保留品类配额检查点。')
  }
  let lastError = ''
  let transferApplied = false
  let rateLimited = false
  for (const collectorId of collectorIds) {
    if (ready() || rateLimited || manualPausePending()) break
    const context = contexts.get(collectorId)!
    const collectorState = sourceState.collectors[String(collectorId)] ?? { tabOffsets: {}, samePoolCount: 0, ruleStates: {} }
    collectorState.ruleStates ??= {}
    collectorState.scrollPositions ??= {}
    collectorState.scrollPositions['selectionSquare:categoryPlanVersion'] = 2
    collectorState.scrollPositions['hotSpot:rootOnlyPlanVersion'] = 1
    sourceState.collectors[String(collectorId)] = collectorState
    let browserUsed = false
    try {
      const schedule = Array.from({ length: 3 }, (_, pass) => rules.map((rule) => ({ rule, pass }))).flat()
      for (const { rule, pass } of schedule) {
        if (manualPausePending() || rateLimited || (collectorState.nextRetryAt && Date.parse(collectorState.nextRetryAt) > Date.now())) break
        const ruleTarget = effectiveTargets.get(rule.id) ?? rule.target
        if (countFor(rule.id) >= ruleTarget) continue
        const state: NonNullable<BatchCandidatePool['sourceState']['collectors'][string]['ruleStates']>[string] =
          collectorState.ruleStates[rule.id] ?? { samePoolCount: 0 }
        collectorState.ruleStates[rule.id] = state
        if (state.exhausted) continue
        if (state.nextRetryAt && Date.parse(state.nextRetryAt) > Date.now()) continue
        for (let round = pass; round < pass + 1 && countFor(rule.id) < (effectiveTargets.get(rule.id) ?? rule.target); round += 1) {
          if (manualPausePending()) break
          const newRows: SelectionCandidate[] = []
          const priorRows = new Map<string, SelectionCandidate | undefined>()
          let newlyAssignedCount = 0
          let reassignedCount = 0
          let persisted = false
          const priorTabOffset = Number(state.categoryTabOffset ?? 0)
          reportSelectionProgress(collectorId, '按品类只读采集共享候选池', {
            itemIndex: countFor(rule.id), itemTotal: effectiveTargets.get(rule.id) ?? rule.target,
            message: `${formatQuotaRule(rule)}：${countFor(rule.id)}/${effectiveTargets.get(rule.id) ?? rule.target}，第 ${round + 1} 轮。`
          })
          try {
            browserUsed = true
            const sources = snapshot.sources as SelectionFilterSettings['sources']
            const selection = {
              level1: rule.categoryPath.slice(0, 1), level2: rule.categoryPath.slice(1, 2),
              level3: rule.categoryPath.slice(2, 3)
            }
            const preview = await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
              profileKey: context.profileKey, profilePath: context.profilePath,
              accountId: collectorId, downloadPath: context.downloadPath,
              settings: {
                ...context.settings, sources: {
                  ...sources, hotSpotEnabled: rule.sourceKey === 'hotSpot',
                  allianceRankEnabled: rule.sourceKey === 'allianceRank',
                  selectionSquareEnabled: rule.sourceKey === 'selectionSquare', rankBoard: rule.rankBoard
                },
                thresholds: snapshot.thresholds, filters: snapshot.filters, bannedWords: snapshot.bannedWords,
                excludedCategorySelection: snapshot.excludedCategorySelection,
                selectionMode: rule.sourceKey === 'selectionSquare' && rule.categoryPath.length ? 'category' : 'general',
                categoryMode: rule.sourceKey === 'selectionSquare' && rule.categoryPath.length ? 'manual' : 'auto',
                categorySelection: selection,
                _quotaCategoryPath: rule.categoryPath, _visibleSourcesOnly: true, _candidatePoolCollectOnly: true,
                _collectorSourceKey: rule.sourceKey,
                _candidatePoolLimit: Math.min(2000, Math.max(1, effectiveTargets.get(rule.id) ?? rule.target)),
                _candidateDesiredPoolCount: Math.min(2000, Math.max(1, effectiveTargets.get(rule.id) ?? rule.target)),
                _candidatePoolRawLimit: state.rawLimit ?? Math.min(2000, Math.max(40, (effectiveTargets.get(rule.id) ?? rule.target) * 8)),
                _candidatePoolScrollSteps: Math.min(120, Math.max(
                  state.scrollSteps ?? 0,
                  6,
                  Math.ceil((effectiveTargets.get(rule.id) ?? rule.target) / 20)
                )),
                _candidatePoolScrollPosition: state.scrollPosition ?? 0,
                _categoryDetailSkipIds: state.detailAttemptedIds ?? [],
                _sourceCategoryTabOffsets: { [rule.sourceKey]: priorTabOffset }
              }, previewOnly: true, closeAfterAutomation: true
            }))
            if (preview.ok !== true) throw new Error(String(preview.error ?? '品类采集失败'))
            const capturedRows = Array.isArray(preview.candidates) ? preview.candidates as SelectionCandidate[] : []
            const occupiedNewIds = await occupiedPoolProductIds(capturedRows, collectorId, batch.batchId)
            for (const productId of occupiedNewIds) excludedProductIds.add(productId)
            for (const row of capturedRows) {
              if (!/^\d+$/.test(String(row.productId ?? '')) || !String(row.title ?? '').trim() ||
                  occupiedNewIds.has(row.productId)) continue
              const sourceRecord = {
                sourceKey: rule.sourceKey,
                categoryPath: [...rule.categoryPath],
                ...(rule.sourceKey === 'allianceRank' ? { rankBoard: rule.rankBoard } : {}),
                ...(row.sourceCategoryId ? { sourceCategoryId: String(row.sourceCategoryId) } : {}),
                ...(row.sourceCategoryName ? { sourceCategoryName: String(row.sourceCategoryName) } : {})
              }
              const existing = rowsById.get(row.productId)
              if (existing) {
                if (!priorRows.has(row.productId)) priorRows.set(row.productId, existing)
                const sources = [...new Set([...(existing.sources ?? []), ...(row.sources ?? []), rule.sourceKey])]
                const sourceRecords = [...(existing.sourceRecords ?? [])]
                if (!sourceRecords.some((item) => item.sourceKey === sourceRecord.sourceKey &&
                    item.categoryPath?.join('/') === sourceRecord.categoryPath.join('/') && item.rankBoard === sourceRecord.rankBoard)) {
                  sourceRecords.push(sourceRecord)
                }
                const currentRuleIndex = rules.findIndex((item) => item.id === rule.id)
                const assignedRuleIndex = rules.findIndex((item) => item.id === existing.quotaRuleId)
                const reassignToEarlierDeficit = assignedRuleIndex > currentRuleIndex &&
                  countFor(rule.id) < (effectiveTargets.get(rule.id) ?? rule.target)
                const merged = {
                  ...existing, sources, sourceRecords,
                  ...(reassignToEarlierDeficit ? {
                    quotaRuleId: rule.id, sourceKey: rule.sourceKey,
                    sourceCategoryIds: row.sourceCategoryIds, sourceCategoryId: row.sourceCategoryId,
                    sourceCategoryName: row.sourceCategoryName, sourceCategorySelection: row.sourceCategorySelection,
                    categoryIds: row.categoryIds, categoryStatus: row.categoryStatus,
                    categorySource: row.categorySource, leafCategoryId: row.leafCategoryId
                  } : {})
                }
                rowsById.set(row.productId, merged)
                newRows.push(merged)
                if (reassignToEarlierDeficit) reassignedCount += 1
                continue
              }
              if (countFor(rule.id) >= (effectiveTargets.get(rule.id) ?? rule.target)) continue
              const assigned = {
                ...row, quotaRuleId: rule.id,
                sources: [...new Set([...(row.sources ?? []), rule.sourceKey])],
                sourceRecords: [...(row.sourceRecords ?? []), sourceRecord]
              }
              priorRows.set(row.productId, undefined)
              rowsById.set(row.productId, assigned)
              newRows.push(assigned)
              newlyAssignedCount += 1
            }
            const sourceResults = Array.isArray(preview.prioritySourceResults)
              ? preview.prioritySourceResults as PrioritySourceReadResult[] : []
            const sourceResult = sourceResults.find((result) => result.sourceKey === rule.sourceKey)
            if (sourceResult?.scrollPosition && typeof sourceResult.scrollPosition === 'object') {
              const position = Number(sourceResult.scrollPosition.top)
              if (Number.isFinite(position) && position >= 0) state.scrollPosition = position
            } else if (Number.isFinite(Number(sourceResult?.scrollPosition)) && Number(sourceResult?.scrollPosition) >= 0) {
              state.scrollPosition = Number(sourceResult?.scrollPosition)
            }
            const enrichment = preview.categoryEnrichment && typeof preview.categoryEnrichment === 'object'
              ? preview.categoryEnrichment as { detailAttemptedProductIds?: unknown } : undefined
            if (Array.isArray(enrichment?.detailAttemptedProductIds)) {
              state.detailAttemptedIds = [...new Set([
                ...(state.detailAttemptedIds ?? []),
                ...enrichment.detailAttemptedProductIds.map(String).filter((id) => /^\d+$/.test(id))
              ])].slice(-2000)
            }
            const sourceReadLimitReached = sourceResult?.sourceReadLimitReached === true ||
              sourceResult?.categoryTabReadLimitReached === true
            if (Number.isInteger(sourceResult?.categoryTabNextOffset)) {
              state.categoryTabOffset = Number(sourceResult.categoryTabNextOffset)
              collectorState.tabOffsets[rule.sourceKey] = state.categoryTabOffset
            }
            const offsetAdvanced = Number(state.categoryTabOffset ?? priorTabOffset) > priorTabOffset
            if (sourceReadLimitReached) {
              state.scrollSteps = Math.min(120, Math.max(state.scrollSteps ?? 0, 6, Math.ceil(ruleTarget / 20)) + 24)
              state.rawLimit = Math.min(2000, Math.max(
                state.rawLimit ?? Math.min(2000, Math.max(40, ruleTarget * 8)),
                Math.min(2000, (state.rawLimit ?? Math.min(2000, Math.max(40, ruleTarget * 8))) + 200)
              ))
            }
            state.samePoolCount = newlyAssignedCount || reassignedCount ? 0 : state.samePoolCount + 1
            if (newlyAssignedCount || reassignedCount || offsetAdvanced) {
              state.exhausted = false
              state.nextRetryAt = undefined
            } else {
              const backoffWasElapsed = Boolean(state.nextRetryAt && Date.parse(state.nextRetryAt) <= Date.now())
              const readComplete = sourceResult !== undefined &&
                (sourceResult.status === 'read' || sourceResult.status === 'empty') &&
                !sourceReadLimitReached &&
                Number(sourceResult.categoryTabNextOffset ?? 0) >= Number(sourceResult.categoryTabTotal ?? 0)
              state.exhausted = backoffWasElapsed && readComplete
              state.nextRetryAt = state.exhausted ? undefined :
                new Date(Date.now() + Math.min(30, 5 * 2 ** (state.samePoolCount - 1)) * 60_000).toISOString()
            }
            collectorState.nextRetryAt = undefined
            await savePool(newRows, collectorId)
            persisted = true
            if (!newlyAssignedCount && !reassignedCount && (offsetAdvanced || sourceReadLimitReached)) continue
            await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
              stage: '候选池采集', code: 'INFO_CANDIDATE_POOL_QUOTA',
              message: `${formatQuotaRule(rule)} ${countFor(rule.id)}/${effectiveTargets.get(rule.id) ?? rule.target}，本轮新增唯一商品 ${newlyAssignedCount} 件、改归属 ${reassignedCount} 件。`,
              detail: `只读采集，未加车；已登记 ID 入池前排除 ${occupiedNewIds.size} 件，商品 ID 跨规则去重；本条规则类目游标 ${priorTabOffset}→${state.categoryTabOffset ?? priorTabOffset}。`
            })
            if (!newlyAssignedCount && !reassignedCount) break
          } catch (cause) {
            if (!persisted) for (const [productId, original] of priorRows) {
              if (original) rowsById.set(productId, original)
              else rowsById.delete(productId)
            }
            const rawError = String(cause instanceof Error ? cause.message : cause)
            const isRateLimit = /11001|RATE_LIMIT|限流/i.test(rawError)
            const requiresAttention = /验证码|安全验证|需要登录|未登录|环境危险|环境异常|DETAIL_ENVIRONMENT_RISK_REQUIRED/i.test(rawError)
            state.exhausted = false
            if (isRateLimit) {
              state.nextRetryAt = pauseCandidatePoolAfterRateLimit(sourceState, collectorIds)
              rateLimited = true
            } else {
              state.nextRetryAt = new Date(Date.now() + 5 * 60_000).toISOString()
            }
            if (requiresAttention) sourceState.autoResumeAllowed = false
            lastError = String(toUserSelectionError(cause).classification || 'COLLECTOR_READ_FAILED')
            await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
              stage: '候选池采集', code: 'WARNING_CANDIDATE_POOL_QUOTA_READ',
              message: `${formatQuotaRule(rule)} 本轮未能读取。`,
              detail: `${lastError}；${safeCandidateCollectorErrorDetail(rawError)}`
            })
            await savePool([], collectorId)
            if (requiresAttention) throw cause
            break
          }
        }
        const deficit = Math.max(0, (effectiveTargets.get(rule.id) ?? rule.target) - countFor(rule.id))
        const confirmedExhausted = collectorIds.every((id) =>
          sourceState.collectors[String(id)]?.ruleStates?.[rule.id]?.exhausted === true
        )
        if (deficit > 0 && confirmedExhausted) {
          const receivers = rules.filter((candidateRule) => candidateRule.id !== rule.id)
          const receiver = receivers.find((candidateRule) => candidateRule.id !== rule.id && collectorIds.some((id) =>
            sourceState.collectors[String(id)]?.ruleStates?.[candidateRule.id]?.exhausted !== true
          ))
          if (receiver) {
            effectiveTargets.set(rule.id, (effectiveTargets.get(rule.id) ?? rule.target) - deficit)
            effectiveTargets.set(receiver.id, (effectiveTargets.get(receiver.id) ?? receiver.target) + deficit)
            quotaTransfers.push({ fromRuleId: rule.id, toRuleId: receiver.id, count: deficit, transferredAt: new Date().toISOString() })
            transferApplied = true
            await savePool([], collectorId)
            await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
              stage: '候选池缺额转移', code: 'INFO_CANDIDATE_POOL_QUOTA_TRANSFER',
              message: `${formatQuotaRule(rule)} 确认无新合格商品，缺额 ${deficit} 件转给 ${formatQuotaRule(receiver)}。`,
              detail: '接收规则继续使用自身来源、类目、统一筛选和排除条件；没有放宽条件，候选仍按商品 ID 全池去重。'
            })
          }
        }
      }
    } finally {
      if (browserUsed) {
        try {
          const closed = await window.api.browser.closeAccount({ accountId: collectorId, profilePath: context.profilePath })
          if (closed.ok !== true) throw new Error('采集账号浏览器关闭未确认')
        } catch {
          sourceState.autoResumeAllowed = false
          await savePool([], collectorId)
          await runRecovery(['batch', 'update', '--batch-id', batch.batchId, '--state', 'needs_retry'])
          throw new Error(`采集账号 ${collectorId} 浏览器关闭未确认，已停止后续账号。`)
        }
      }
    }
  }
  if (transferApplied && !manualPausePending()) {
    const refreshed = await runRecovery(['batch', 'latest'])
    const currentBatch = refreshed.batch as SelectionRecoveryBatch | null
    if (!currentBatch || currentBatch.batchId !== batch.batchId) throw new Error('品类缺额转移后批次检查点发生变化。')
    return collectBatchCandidatePoolByQuota(currentBatch, contexts)
  }
  if (!matches && rowsById.size === 0) await savePool([], collectorIds[0])
  const refreshed = await runRecovery(['batch', 'latest'])
  const currentBatch = refreshed.batch as SelectionRecoveryBatch | null
  if (!currentBatch || currentBatch.batchId !== batch.batchId) throw new Error('品类配额检查点发生变化。')
  const pool = currentBatch.payload.candidatePool as BatchCandidatePool
  if (!pool || pool.status !== 'ready') {
    const deficit = pool?.quotaProgress?.filter((item) => item.deficit > 0)
      .map((item) => `${formatQuotaRule(rules.find((rule) => rule.id === item.ruleId)!)} 缺 ${item.deficit} 件`).join('；') ?? ''
    await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
      stage: '候选池采集', code: 'WARNING_CANDIDATE_POOL_TARGET_UNMET',
      message: `品类配额未达标：${deficit}。消费账号尚未开始。`,
      detail: lastError || '已保存逐行配额及商品 ID；等待来源产生可核实的新商品。'
    })
    const updated = await runRecovery(['batch', 'update', '--batch-id', batch.batchId, '--state', 'needs_retry'])
    return { batch: updated.batch as SelectionRecoveryBatch, pool, ready: false }
  }
  if (currentBatch.state === 'needs_retry') {
    const updated = await runRecovery(['batch', 'update', '--batch-id', batch.batchId, '--state', 'in_progress'])
    return { batch: updated.batch as SelectionRecoveryBatch, pool, ready: true }
  }
  return { batch: currentBatch, pool, ready: true }
}

async function collectBatchCandidatePool(
  batch: SelectionRecoveryBatch,
  contexts: Map<number, AccountRunContext>
): Promise<{ batch: SelectionRecoveryBatch; pool: BatchCandidatePool; ready: boolean; autoResumeAllowed?: boolean }> {
  const snapshot = batch.payload.settingsSnapshot as Record<string, unknown> | undefined
  if (snapshot && !batch.payload.candidatePool) {
    const selectedSourceBatchId = String(batch.payload.candidatePoolReuseBatchId ?? '')
    const reused = await runRecovery(['batch', 'pool-reuse', '--batch-id', batch.batchId], {
      filterFingerprint: batchCandidatePoolFingerprint(snapshot),
      sourceBatchId: selectedSourceBatchId
    })
    const summary = reused.pool as { count?: number; sourceBatchId?: string; detail?: string } | undefined
    if (selectedSourceBatchId && Number(summary?.count) < 1) {
      throw new Error(`所选本机候选池已过期或与本次选品条件不符${summary?.detail ? `：${summary.detail}` : ''}；新批次检查点已保存，未重新抓取来源或启动消费账号。`)
    }
    if (Number(summary?.count) > 0) {
      const refreshed = await runRecovery(['batch', 'latest'])
      const checkpoint = refreshed.batch as SelectionRecoveryBatch | null
      if (!checkpoint || checkpoint.batchId !== batch.batchId) {
        throw new Error('本机候选池复用后批次检查点发生变化，已停止消费阶段。')
      }
      batch = checkpoint
      showSavedPoolCheckpoint(batch)
      await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
        stage: '候选池采集', code: 'INFO_LOCAL_CANDIDATE_POOL_REUSED',
        message: `从本机历史批次复用 ${summary?.count} 件候选商品；消费账号仍将按 ID 重新核验。`,
        detail: `来源批次 ${summary?.sourceBatchId ?? '未知'}；历史采集游标及账号加车结果未复用。`
      })
    }
  }
  if (snapshot?.candidatePoolMode === 'quota') return collectBatchCandidatePoolByQuota(batch, contexts)
  const target = Number(snapshot?.candidatePoolTarget)
  const collectorIds = Array.isArray(snapshot?.candidateCollectorAccountIds)
    ? (snapshot?.candidateCollectorAccountIds as unknown[]).map(Number) : []
  const filterFingerprint = batchCandidatePoolFingerprint(snapshot ?? {})
  const oldPool = batch.payload.candidatePool as BatchCandidatePool | undefined
  const poolConfigMatches = oldPool?.target === target && oldPool.collectorAccountIds?.join(',') === collectorIds.join(',') &&
    oldPool.filterFingerprint === filterFingerprint
  if (!Number.isInteger(target) || target < 1 || target > 2000 || ![1, 2].includes(collectorIds.length) ||
      new Set(collectorIds).size !== collectorIds.length) {
    throw new Error('批次候选池缺少有效的目标数量或 1–2 个已启用采集账号；未开始消费账号选品。')
  }
  const rowsById = new Map<string, SelectionCandidate>()
  const excludedProductIds = new Set<string>(poolConfigMatches && Array.isArray(oldPool?.excludedProductIds)
    ? oldPool.excludedProductIds : [])
  if (poolConfigMatches && oldPool) {
    for (const row of oldPool.rows ?? []) {
      if (/^\d+$/.test(String(row.productId ?? '')) && !excludedProductIds.has(row.productId)) rowsById.set(row.productId, row)
    }
  }
  const occupiedOldIds = await occupiedPoolProductIds([...rowsById.values()], collectorIds[0], batch.batchId)
  for (const productId of occupiedOldIds) { rowsById.delete(productId); excludedProductIds.add(productId) }
  const sourceState: BatchCandidatePool['sourceState'] = oldPool && poolConfigMatches && oldPool.sourceState?.collectors
    ? structuredClone(oldPool.sourceState) : { collectors: {} }
  sourceState.autoResumeAllowed = true
  const legacyCategoryCursors = poolConfigMatches && Object.values(sourceState.collectors).some((collector) =>
    collector.scrollPositions?.['selectionSquare:categoryPlanVersion'] !== 2
  )
  if (legacyCategoryCursors) for (const collector of Object.values(sourceState.collectors)) {
    collector.tabOffsets = {}
    collector.scrollPositions = { 'selectionSquare:categoryPlanVersion': 2 }
    collector.samePoolCount = 0
    collector.nextRetryAt = undefined
  }
  const legacyHotSpotCursors = poolConfigMatches && resetHotSpotRootOnlyCursors(sourceState, new Set())
  const savePool = async (rows: SelectionCandidate[], collectorAccountId: number): Promise<void> => {
    const capturedAt = new Date().toISOString()
    const saved = await runRecovery(['batch', 'pool-save', '--batch-id', batch.batchId], {
      target, collectorAccountIds: collectorIds, collectorAccountId, capturedAt, filterFingerprint,
      rows: rows.map((row) => ({ ...row, collectedByAccountId: collectorAccountId, capturedAt })),
      excludedProductIds: [...excludedProductIds], sourceState
    })
    const summary = saved.pool as { count?: number; status?: string } | undefined
    if (Number(summary?.count) !== rowsById.size) {
      throw new Error('候选池落盘数量与本轮内存商品 ID 数不一致；已停止进入消费阶段。')
    }
    showConfirmedPoolSave(batch.batchId, target, 'total', summary?.status === 'ready' ? 'ready' : 'collecting',
      [...rowsById.values()])
  }
  if (occupiedOldIds.size || legacyCategoryCursors || legacyHotSpotCursors) await savePool([], collectorIds[0])
  if (rowsById.size < target && collectorIds.some((id) => !contexts.has(id))) {
    throw new Error('剔除已登记商品后候选池未达目标，采集账号资源不可用；已保留检查点。')
  }
  let lastError = ''
  let autoResumeAllowed = true
  let rateLimited = false
  const configuredSources = snapshot?.sources && typeof snapshot.sources === 'object'
    ? snapshot.sources as Record<string, unknown> : {}
  const requestedSourceOrder = Array.isArray(configuredSources.priority)
    ? configuredSources.priority.map(String) : []
  const activeSourceOrder = [...new Set([...requestedSourceOrder, 'hotSpot', 'allianceRank', 'selectionSquare'])]
    .filter((key) => ['hotSpot', 'allianceRank', 'selectionSquare'].includes(key) && configuredSources[`${key}Enabled`] !== false)
  if (!activeSourceOrder.length) throw new Error('候选池没有已启用的来源；未开始采集。')
  if (rowsById.size < target) {
    for (const collectorId of collectorIds) {
      if (manualPausePending() || rateLimited || !autoResumeAllowed) break
      const context = contexts.get(collectorId)!
      const collectorState: BatchCandidatePool['sourceState']['collectors'][string] =
        sourceState.collectors[String(collectorId)] ?? { tabOffsets: {}, samePoolCount: 0 }
      collectorState.scrollPositions ??= {}
      collectorState.scrollPositions['selectionSquare:categoryPlanVersion'] = 2
      collectorState.scrollPositions['hotSpot:rootOnlyPlanVersion'] = 1
      sourceState.collectors[String(collectorId)] = collectorState
      if (collectorState.nextRetryAt && new Date(collectorState.nextRetryAt).getTime() > Date.now()) continue
      const finishedSources = new Set<string>()
      // 来源仍有未读分类或新商品时沿持久化游标续采；上限只防止页面异常造成无界循环。
      for (let round = 0; round < 1000 && rowsById.size < target; round += 1) {
        if (manualPausePending()) break
        const sourceKey = activeSourceOrder[round % activeSourceOrder.length]
        if (finishedSources.has(sourceKey)) continue
        const before = rowsById.size
        const priorOffsets = JSON.stringify(collectorState.tabOffsets)
        const newRows: SelectionCandidate[] = []
        let persisted = false
        reportSelectionProgress(collectorId, '只读采集共享候选池', {
          itemIndex: before, itemTotal: target,
          message: `采集账号 ${collectorId}：候选池 ${before}/${target}，第 ${round + 1} 轮读取 ${sourceKey}。`
        })
        try {
        try {
          const preview = await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
            profileKey: context.profileKey, profilePath: context.profilePath,
            accountId: collectorId, downloadPath: context.downloadPath,
            settings: {
              ...context.settings,
              sources: snapshot?.sources, thresholds: snapshot?.thresholds,
              filters: snapshot?.filters, bannedWords: snapshot?.bannedWords,
              selectionMode: snapshot?.selectionMode, categoryMode: snapshot?.categoryMode,
              categorySelection: snapshot?.categorySelection,
              excludedCategorySelection: snapshot?.excludedCategorySelection,
              _visibleSourcesOnly: true, _candidatePoolCollectOnly: true,
              _collectorSourceKey: sourceKey,
              _candidatePoolLimit: Math.min(2000, target),
              _candidateDesiredPoolCount: Math.min(2000, target),
              _candidatePoolRawLimit: collectorState.rawLimit ?? Math.min(2000, Math.max(40, target * 8)),
              _candidatePoolScrollSteps: Math.min(120, Math.max(
                collectorState.scrollSteps ?? 0, 6, Math.ceil(target / 20)
              )),
              _candidatePoolScrollPositions: collectorState.scrollPositions ?? {},
              _categoryDetailSkipIds: collectorState.detailAttemptedIds ?? [],
              _sourceCategoryTabOffsets: collectorState.tabOffsets
            },
            previewOnly: true, closeAfterAutomation: true
          }))
          if (preview.ok !== true) throw new Error(String(preview.error ?? '只读采集失败'))
          const capturedRows = Array.isArray(preview.candidates) ? preview.candidates as SelectionCandidate[] : []
          const occupiedNewIds = await occupiedPoolProductIds(capturedRows, collectorId, batch.batchId)
          for (const productId of occupiedNewIds) excludedProductIds.add(productId)
          for (const row of capturedRows) {
            if (!/^\d+$/.test(String(row.productId ?? '')) || !String(row.title ?? '').trim() ||
                excludedProductIds.has(row.productId) || rowsById.has(row.productId) || occupiedNewIds.has(row.productId)) continue
            rowsById.set(row.productId, row)
            newRows.push(row)
          }
          const results = Array.isArray(preview.prioritySourceResults)
            ? preview.prioritySourceResults as PrioritySourceReadResult[] : []
          const currentSourceResult = results.find((result) => result.sourceKey === sourceKey)
          collectorState.scrollPositions ??= {}
          for (const result of results) {
            if (result.scrollPositions && typeof result.scrollPositions === 'object') {
              for (const [key, value] of Object.entries(result.scrollPositions)) {
                const position = Number(value)
                if (Number.isFinite(position) && position >= 0) collectorState.scrollPositions[key] = position
              }
            }
            const rawPosition = result.scrollPosition && typeof result.scrollPosition === 'object'
              ? result.scrollPosition.top : result.scrollPosition
            const position = Number(rawPosition)
            if (Number.isFinite(position) && position >= 0 && collectorState.scrollPositions[result.sourceKey] === undefined) {
              collectorState.scrollPositions[result.sourceKey] = position
            }
          }
          const sourceSummary = results.map((result) => {
            if (result.status === 'disabled') return `${result.label}关闭`
            const failure = result.status === 'skipped'
              ? `，原因：${safeCandidateCollectorErrorDetail(result.detail || result.sourceOutcome || result.pageListDiagnosis).slice(0, 200) || '来源读取未完成'}` : ''
            const remaining = Number.isInteger(result.categoryTabTotal) && Number.isInteger(result.categoryTabNextOffset)
              ? `，分类 ${result.categoryTabNextOffset}/${result.categoryTabTotal}` : ''
            return `${result.label}读取${result.productCount}、来源过滤${result.sourceFilterRejected ?? 0}${remaining}${failure}`
          }).join('；')
          const rejectedRows = Array.isArray(preview.rejectedCandidates) ? preview.rejectedCandidates as SelectionCandidate[] : []
          const rejectedCount = rejectedRows.length
          const rejectionCounts = new Map<string, number>()
          for (const row of rejectedRows) {
            const reason = row.thresholdReasons?.[0] ?? row.analysisStatus ?? '条件未通过'
            rejectionCounts.set(reason, (rejectionCounts.get(reason) ?? 0) + 1)
          }
          const rejectionSummary = [...rejectionCounts].sort((a, b) => b[1] - a[1]).slice(0, 3)
            .map(([reason, count]) => `${reason} ${count} 件`).join('；')
          for (const result of results) {
            if (Number.isInteger(result.categoryTabNextOffset)) {
              collectorState.tabOffsets[result.sourceKey] = Number(result.categoryTabNextOffset)
            }
          }
          const incompleteSourceRead = results.some((result) =>
            result.sourceReadLimitReached === true || result.categoryTabReadLimitReached === true
          )
          if (incompleteSourceRead) {
            collectorState.scrollSteps = Math.min(120, Math.max(
              collectorState.scrollSteps ?? 0, 6, Math.ceil(target / 20)
            ) + 24)
            collectorState.rawLimit = Math.min(2000, Math.max(
              collectorState.rawLimit ?? Math.min(2000, Math.max(40, target * 8)),
              Math.min(2000, (collectorState.rawLimit ?? Math.min(2000, Math.max(40, target * 8))) + 200)
            ))
          }
          const enrichment = preview.categoryEnrichment && typeof preview.categoryEnrichment === 'object'
            ? preview.categoryEnrichment as { detailAttemptedProductIds?: unknown } : undefined
          if (Array.isArray(enrichment?.detailAttemptedProductIds)) {
            collectorState.detailAttemptedIds = [...new Set([
              ...(collectorState.detailAttemptedIds ?? []),
              ...enrichment.detailAttemptedProductIds.map(String).filter((id) => /^\d+$/.test(id))
            ])].slice(-2000)
          }
          const sourceAdvanced = newRows.length > 0 || priorOffsets !== JSON.stringify(collectorState.tabOffsets)
          collectorState.samePoolCount = sourceAdvanced ? 0 : collectorState.samePoolCount + 1
          if (sourceAdvanced) collectorState.nextRetryAt = undefined
          if (!sourceAdvanced || currentSourceResult?.status === 'skipped' ||
              (Number(currentSourceResult?.categoryTabTotal ?? 0) > 0 &&
               Number(currentSourceResult?.categoryTabNextOffset ?? 0) >= Number(currentSourceResult?.categoryTabTotal) &&
               currentSourceResult?.sourceReadLimitReached !== true)) finishedSources.add(sourceKey)
          if (finishedSources.size >= activeSourceOrder.length) {
            const minutes = Math.min(30, 5 * 2 ** Math.max(0, collectorState.samePoolCount - 1))
            collectorState.nextRetryAt = new Date(Date.now() + minutes * 60_000).toISOString()
          }
          await savePool(newRows, collectorId)
          persisted = true
          await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
            stage: '候选池采集', code: 'INFO_CANDIDATE_POOL_COLLECTED',
            message: `采集账号 ${collectorId} 本轮新增 ${newRows.length} 件，池内 ${rowsById.size}/${target}。`,
            detail: `${sourceSummary}；已登记 ID 入池前排除 ${occupiedNewIds.size} 件；统一条件淘汰或待核验 ${rejectedCount} 件：${rejectionSummary}。来源分类游标已保存；只读采集，未加车或计入账号配额。`
          })
          if (finishedSources.size >= activeSourceOrder.length) break
          if ((round + 1) % 3 === 0 && rowsById.size < target) {
            await new Promise<void>((resolve) => window.setTimeout(resolve, 2_000))
          }
        } catch (cause) {
          if (!persisted) for (const row of newRows) rowsById.delete(row.productId)
          const rawError = String(cause instanceof Error ? cause.message : cause)
          lastError = String(toUserSelectionError(cause).classification || 'COLLECTOR_READ_FAILED')
          await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
            stage: '候选池采集', code: 'WARNING_CANDIDATE_COLLECTOR_SKIPPED',
            message: `采集账号 ${collectorId} 本轮未完成读取。`,
            detail: `${lastError}；${safeCandidateCollectorErrorDetail(rawError)}`
          })
          if (/验证码|安全验证|需要登录|未登录|环境危险|环境异常|DETAIL_ENVIRONMENT_RISK_REQUIRED/i.test(rawError)) {
            autoResumeAllowed = false
            sourceState.autoResumeAllowed = false
            await savePool([], collectorId)
            break
          }
          if (/11001|RATE_LIMIT|限流/i.test(rawError)) {
            pauseCandidatePoolAfterRateLimit(sourceState, collectorIds)
            rateLimited = true
            await savePool([], collectorId)
            break
          }
          finishedSources.add(sourceKey)
          if (finishedSources.size >= activeSourceOrder.length) break
        }
        } finally {
          try {
            const closed = await window.api.browser.closeAccount({ accountId: collectorId, profilePath: context.profilePath })
            if (closed.ok !== true) throw new Error('客户端未确认采集账号浏览器关闭')
          } catch {
            sourceState.autoResumeAllowed = false
            await savePool([], collectorId)
            await runRecovery(['batch', 'update', '--batch-id', batch.batchId, '--state', 'needs_retry'])
            throw new Error(`采集账号 ${collectorId} 浏览器关闭未确认；为避免同时打开两个账号，已停止后续采集。`)
          }
        }
      }
    }
  }
  if (!oldPool && rowsById.size === 0) await savePool([], collectorIds[0])
  const refreshed = await runRecovery(['batch', 'latest'])
  const currentBatch = refreshed.batch as SelectionRecoveryBatch | null
  if (!currentBatch || currentBatch.batchId !== batch.batchId) throw new Error('候选池采集后批次检查点发生变化；已停止消费阶段。')
  const pool = currentBatch.payload.candidatePool as BatchCandidatePool
  if (!pool || pool.rows.length < target) {
    await runRecovery(['log', 'append', '--batch-id', batch.batchId, '--account-id', '0'], {
      stage: '候选池采集', code: 'WARNING_CANDIDATE_POOL_TARGET_UNMET',
      message: `共享候选池 ${pool?.rows.length ?? 0}/${target}，未达到设定目标，消费账号尚未开始。`,
      detail: lastError || '已保留采集游标和商品 ID，等待来源产生新商品后续读。'
    })
    const updated = await runRecovery(['batch', 'update', '--batch-id', batch.batchId, '--state', 'needs_retry'])
    return { batch: updated.batch as SelectionRecoveryBatch, pool, ready: false,
      autoResumeAllowed: autoResumeAllowed && sourceState.autoResumeAllowed !== false }
  }
  if (currentBatch.state === 'needs_retry') {
    const updated = await runRecovery(['batch', 'update', '--batch-id', batch.batchId, '--state', 'in_progress'])
    return { batch: updated.batch as SelectionRecoveryBatch, pool, ready: true }
  }
  return { batch: currentBatch, pool, ready: true }
}


async function prepareBatchCandidatePlans(
  targets: AccountRow[],
  contexts: Map<number, AccountRunContext>,
  reviewedAccountId?: number | null,
  reviewedProducts?: ReadonlySet<string>,
  accountSwitchFailures?: Map<number, AccountSwitchFailure>,
  seededPreviewPlans?: ReadonlyMap<number, AccountCandidatePlan>,
  batchSeed: string = window.crypto.randomUUID(),
  keepBrowserOpen = false,
  collectorPool?: BatchCandidatePool
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
  const sharedPools = new Map<string, {
    rows: SelectionCandidate[]
    createdByAccountId: number
    filterFingerprint: string
    visibilityFingerprint: string
    dataCapturedAt: string
    rejectedCandidates: SelectionCandidate[]
    prioritySourceResults: PrioritySourceReadResult[]
    warnings: string[]
    categoryEnrichment?: AccountCandidatePlan['categoryEnrichment']
  }>()
  const poolReuseMetaByAccount = new Map<number, AccountCandidatePlan['candidatePoolMeta']>()
  const sharedPoolRowsByAccount = new Map<number, SelectionCandidate[]>()
  const pendingByAccount = new Map<number, {
    account: AccountRow
    settings: Record<string, unknown>
    preview: Record<string, unknown>
    quota: Record<string, unknown>
    rows: SelectionCandidate[]
    targetCount: number
  }>()

  const orderedTargets = [...targets].sort((left, right) => left.id - right.id)
  for (const [targetIndex, account] of orderedTargets.entries()) {
    const context = contexts.get(account.id)
    if (!context) throw new Error(`账号「${account.name}」缺少运行设置，未开始批次候选采集。`)
    const settings: Record<string, unknown> = {
      ...context.settings,
      _visibleSourcesOnly: true,
      _candidatePoolLimit: 200
    }
    const filterFingerprint = selectionFilterFingerprint(settings)
    const visibilityFingerprint = selectionPoolVisibilityFingerprint(
      context.resolvedProfilePath ?? context.profilePath
    )
    const samePoolAccountCount = visibilityFingerprint
      ? orderedTargets.filter((other) => {
          const otherContext = contexts.get(other.id)
          return otherContext &&
            selectionFilterFingerprint(otherContext.settings) === filterFingerprint &&
            selectionPoolVisibilityFingerprint(otherContext.resolvedProfilePath ?? otherContext.profilePath) === visibilityFingerprint
        }).length
      : 1
    const configuredTarget = Number(context.settings.candidateLimit ?? 5)
    settings._candidateDesiredPoolCount = Math.min(200,
      Math.max(1, Number.isFinite(configuredTarget) ? configuredTarget : 5) * samePoolAccountCount)
    const sharedPoolKey = `${filterFingerprint}\u0000${visibilityFingerprint}`
    const sameFilterDifferentVisibility = [...sharedPools.values()].some((pool) =>
      pool.filterFingerprint === filterFingerprint && pool.visibilityFingerprint !== visibilityFingerprint
    )
    const seededPlan = seededPreviewPlans?.get(account.id)
    const seededRows = seededPlan?.candidatePool
    const seededSharedRows = seededPlan?.sharedCandidatePool
    const seededMeta = seededPlan?.candidatePoolMeta
    const reusableSharedPool = collectorPool?.status === 'ready' && collectorPool.rows.length >= collectorPool.target
      ? {
          rows: collectorPool.rows,
          createdByAccountId: collectorPool.collectorAccountIds[0],
          filterFingerprint,
          visibilityFingerprint,
          dataCapturedAt: collectorPool.capturedAt,
          rejectedCandidates: [] as SelectionCandidate[],
          prioritySourceResults: [] as PrioritySourceReadResult[],
          warnings: ['本批共用只读采集池；当前账号仍须按精确商品 ID 核验详情、实际筛选条件和加车结果。'],
          categoryEnrichment: undefined
        }
      : seededSharedRows?.length
      ? {
          rows: seededSharedRows,
          createdByAccountId: seededMeta?.createdByAccountId ?? account.id,
          filterFingerprint: seededMeta?.filterFingerprint ?? filterFingerprint,
          visibilityFingerprint: seededMeta?.visibilityFingerprint ?? visibilityFingerprint,
          dataCapturedAt: seededMeta?.dataCapturedAt ?? new Date().toISOString(),
          rejectedCandidates: seededPlan?.rejectedCandidates ?? [],
          prioritySourceResults: seededPlan?.prioritySourceResults ?? [],
          warnings: seededPlan?.warnings ?? [],
          categoryEnrichment: seededPlan?.categoryEnrichment
        }
      : sharedPools.get(sharedPoolKey)
    const reuseAllowed = Boolean(reusableSharedPool && reusableSharedPool.rows.length && (
      collectorPool?.status === 'ready' || (
        reusableSharedPool.filterFingerprint === filterFingerprint &&
        reusableSharedPool.visibilityFingerprint === visibilityFingerprint &&
        Boolean(visibilityFingerprint)
      )
    ))
    const poolReuseReason = reuseAllowed
      ? collectorPool
        ? `复用采集账号只读收集的 ${collectorPool.rows.length} 个商品 ID；当前账号按自身登录态和设置重新核验。`
        : `条件指纹 ${filterFingerprint.slice(0, 12)}、来源可见范围指纹一致，复用账号 ${reusableSharedPool!.createdByAccountId} 的候选数据（采集于 ${reusableSharedPool!.dataCapturedAt}）。`
      : sameFilterDifferentVisibility
        ? '筛选条件一致但来源账号权限或可见范围指纹不同，未复用候选池。'
        : !visibilityFingerprint
          ? '来源账号可见范围未确认，未复用候选池。'
          : seededRows?.length
            ? '预览候选池缺少可验证的来源指纹，未跨账号复用。'
            : '当前批次没有可复用的同条件候选池，创建新池。'
    reportSelectionProgress(account.id, '统一采集候选池', {
      accountIndex: targetIndex + 1,
      accountTotal: orderedTargets.length,
      completedAccounts: targetIndex,
      message: reuseAllowed
        ? `复用账号 ${reusableSharedPool!.createdByAccountId} 的统一候选池，并重新检查本账号登记、配额和商品状态（${poolReuseReason}）`
        : `只读采集热点趋势、联盟榜单和选品广场并统一筛选（${poolReuseReason}）`
    })
    let preview: Record<string, unknown>
    let quota: Record<string, unknown>
    try {
      // 先完成本地配额查询，再启动较慢的百应页面采集。并行时若本地命令
      // 30 秒超时，浏览器仍可能采集到“候选预览完成”，导致界面误报整轮失败。
      quota = await runEngine('selection_settings.py', [
        '--db', DB_PATH, 'quota', 'get', '--account-id', String(account.id)
      ])
      const previewRequest: Promise<Record<string, unknown>> = reuseAllowed
        ? Promise.resolve({
            ok: true,
            candidates: reusableSharedPool!.rows,
            rejectedCandidates: reusableSharedPool!.rejectedCandidates,
            warnings: [...reusableSharedPool!.warnings, poolReuseReason],
            prioritySourceResults: reusableSharedPool!.prioritySourceResults,
            recommendedCategories: seededPlan?.recommendedCategories ?? [],
            categoryEnrichment: reusableSharedPool!.categoryEnrichment
          })
        : window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
            profileKey: context.profileKey,
            profilePath: context.profilePath,
            settings,
            accountId: account.id,
            downloadPath: context.downloadPath,
            previewOnly: true,
            closeAfterAutomation: !keepBrowserOpen
          }))
      preview = await previewRequest
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
    const rows = Array.isArray(preview.candidates) ? preview.candidates as SelectionCandidate[] : []
    if (!reuseAllowed && rows.length) {
      sharedPools.set(sharedPoolKey, {
        rows: rows.map((row) => ({ ...row })),
        createdByAccountId: account.id,
        filterFingerprint,
        visibilityFingerprint,
        dataCapturedAt: new Date().toISOString(),
        rejectedCandidates: Array.isArray(preview.rejectedCandidates) ? preview.rejectedCandidates as SelectionCandidate[] : [],
        prioritySourceResults: Array.isArray(preview.prioritySourceResults) ? preview.prioritySourceResults as PrioritySourceReadResult[] : [],
        warnings: Array.isArray(preview.warnings) ? preview.warnings.map(String) : [],
        categoryEnrichment: preview.categoryEnrichment && typeof preview.categoryEnrichment === 'object'
          ? preview.categoryEnrichment as AccountCandidatePlan['categoryEnrichment'] : undefined
      })
    }
    const sharedPool = reuseAllowed ? reusableSharedPool! : sharedPools.get(sharedPoolKey)
    sharedPoolRowsByAccount.set(account.id, sharedPool?.rows ?? rows)
    poolReuseMetaByAccount.set(account.id, {
      createdByAccountId: sharedPool?.createdByAccountId ?? account.id,
      filterFingerprint,
      visibilityFingerprint,
      dataCapturedAt: sharedPool?.dataCapturedAt ?? new Date().toISOString(),
      reuseStatus: reuseAllowed ? 'reused' : 'created',
      reuseReason: poolReuseReason
    })
    collectedSourceIdsByAccount.set(account.id, new Map(
      [...new Set(rows.flatMap((candidate) => candidate.sources ?? []))].map((sourceKey) => [
        sourceKey,
        new Set(rows.filter((candidate) => candidate.sources?.includes(sourceKey)).map((candidate) => candidate.productId))
      ])
    ))
    const rawTarget = Number(settings.candidateLimit ?? 5)
    const targetCount = Number.isFinite(rawTarget) ? Math.max(1, Math.min(10, rawTarget)) : 5
    pendingByAccount.set(account.id, { account, settings, preview, quota, rows, targetCount })
  }

  if (pendingByAccount.size) {
    for (const account of pendingByAccount.keys()) {
      reportSelectionProgress(account, '核对选品登记记录', {
        accountIndex: orderedTargets.findIndex((item) => item.id === account) + 1,
        accountTotal: orderedTargets.length,
        message: `正在查询 ${pendingByAccount.size} 个账号的商品 ID 登记。`
      })
    }
  }
  const registrySnapshots = await window.api.browser.getSelectionRegistryBatchSnapshot(
    [...pendingByAccount.values()].map(({ account, rows }) => ({
      accountKey: registryAccountKey(account.id),
      candidates: rows.map((candidate) => ({
        productId: candidate.productId,
        leafCategoryId: candidateLeafCategoryId(candidate)
      }))
    }))
  )

  for (const [targetIndex, account] of orderedTargets.entries()) {
    const pending = pendingByAccount.get(account.id)
    if (!pending) continue
    const { settings, preview, quota, rows: rawRows, targetCount } = pending
    const snapshot = registrySnapshots[registryAccountKey(account.id)]
    if (!snapshot) throw new Error(`账号「${account.name}」缺少本机商品 ID 查重结果；已停止本轮选品。`)
    const registry = filterRowsByLocalRegistry(
      rawRows,
      snapshot,
      targetCount,
      settings.selectionMode === 'category' ? 'category' : 'general',
      Boolean(collectorPool)
    )
    let rows = registry.candidates
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
    const publishLocks = await loadProductPublishLocks(account.id, rows.map((candidate) => candidate.productId))
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
    const registryStatus = snapshot as unknown as {
      warning?: unknown
      syncConflicts?: Array<{ productId?: unknown; reason?: unknown }>
    }
    const registryWarnings = [
      ...(registryStatus.warning ? [String(registryStatus.warning)] : []),
      ...(registryStatus.syncConflicts?.length
        ? [`服务器同步发现 ${registryStatus.syncConflicts.length} 个跨客户端冲突；保留本机已确认加车事实并从后续候选排除。商品 ID：${registryStatus.syncConflicts.slice(0, 20).map((item) => String(item.productId ?? '')).join('、')}`] : [])
    ]
    warningsByAccount.set(account.id, [
      ...(Array.isArray(preview.warnings) ? preview.warnings.map(String) : []),
      ...registryWarnings
    ])
    categoryEnrichmentByAccount.set(account.id, preview.categoryEnrichment && typeof preview.categoryEnrichment === 'object'
      ? preview.categoryEnrichment as AccountCandidatePlan['categoryEnrichment'] : undefined)
    recommendationsByAccount.set(account.id, Array.isArray(preview.recommendedCategories)
      ? preview.recommendedCategories as AccountCandidatePlan['recommendedCategories'] : registry.recommendations)
    capacities.set(account.id, remainingCapacity)
    reportSelectionProgress(account.id, '统一采集候选池', {
      accountIndex: targetIndex + 1,
      accountTotal: orderedTargets.length,
      completedAccounts: targetIndex + 1,
      message: `账号 ${targetIndex + 1}/${orderedTargets.length}：来源候选 ${rawRows.length} 个；本机/任务/配额排除 ${excluded.length} 个；可分配容量 ${remainingCapacity}`
    })
  }

  const scoredPools = scoreUnifiedCandidatePools(poolRows)
  const rankedPools = new Map([...scoredPools].map(([accountId, candidates]) => [
    accountId,
    accountId === reviewedAccountId && reviewedProducts !== undefined
      ? candidates
      : rankCandidatesForBatch(candidates, batchSeed)
  ] as const))
  const successfulTargets = targets.filter((account) => !accountSwitchFailures?.has(account.id))
  for (const account of successfulTargets) {
    const context = contexts.get(account.id)
    const settings = context?.settings ?? {}
    const requestedLimit = Number(settings.candidateLimit ?? 5)
    const targetCount = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(10, requestedLimit)) : 5
    const ranked = rankByRecommendedCategories(rankedPools.get(account.id) ?? [], {}, targetCount)
    recommendationsByAccount.set(account.id, ranked.recommendations)
  }
  const allocations = allocateExclusiveCandidates([...rankedPools.entries()].map(([accountId, candidates]) => {
    const targetCapacity = capacities.get(accountId) ?? 0
    const manuallyReviewed = accountId === reviewedAccountId && reviewedProducts !== undefined
    // 本地快照过滤后才做批次随机排序；自动模式保留最多 50 个候选供最终事务冲突时补位。
    // 人工勾选仍按原有分数顺序和目标三倍上限处理，不随机替用户改选。
    return {
      accountId,
      accountName: names.get(accountId) ?? `账号 ${accountId}`,
      capacity: Math.min(candidates.length, manuallyReviewed
        ? targetCapacity * 3
        : targetCapacity > 0 ? BATCH_FALLBACK_CANDIDATE_LIMIT : 0),
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
    winners.sort((left, right) => {
      const categoryOrder = (recommendationOrder.get(candidateLeafCategoryId(left)) ?? Number.MAX_SAFE_INTEGER) -
        (recommendationOrder.get(candidateLeafCategoryId(right)) ?? Number.MAX_SAFE_INTEGER)
      if (categoryOrder) return categoryOrder
      const leftRandomKey = Number(left.batchRandomKey)
      const rightRandomKey = Number(right.batchRandomKey)
      if (Number.isFinite(leftRandomKey) && Number.isFinite(rightRandomKey)) return leftRandomKey - rightRandomKey
      return Number(right.unifiedScore ?? 0) - Number(left.unifiedScore ?? 0)
    })
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
      const eligibleRows = (rankedPools.get(account.id) ?? []).filter((candidate) => ids.has(candidate.productId))
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
      candidates: collectorPool
        ? winners.map((candidate, index) => ({ ...candidate, cardIndex: index }))
        : winners,
      candidatePool: rankedPools.get(account.id) ?? [],
      sharedCandidatePool: sharedPoolRowsByAccount.get(account.id) ?? [],
      candidatePoolMeta: poolReuseMetaByAccount.get(account.id),
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

function reusablePreviewCandidatePlans(
  targets: readonly AccountRow[],
  contexts: ReadonlyMap<number, AccountRunContext>
): Map<number, AccountCandidatePlan> {
  const cache = previewCandidatePool.value
  if (!cache) return new Map()
  const matching = targets.filter((account) => {
    const context = contexts.get(account.id)
    if (!context) return false
    return selectionFilterFingerprint(context.settings) === cache.filterFingerprint &&
      selectionPoolVisibilityFingerprint(context.resolvedProfilePath ?? context.profilePath) === cache.visibilityFingerprint &&
      Boolean(cache.visibilityFingerprint) &&
      Date.now() - Date.parse(cache.dataCapturedAt) <= 10 * 60 * 1000
  })
  if (!matching.length) return new Map()
  return new Map(matching.map((account) => [account.id, {
    ...cache.plan,
    candidatePoolMeta: {
      ...(cache.plan.candidatePoolMeta ?? {
        createdByAccountId: cache.createdByAccountId,
        filterFingerprint: cache.filterFingerprint,
        visibilityFingerprint: cache.visibilityFingerprint,
        dataCapturedAt: cache.dataCapturedAt,
        reuseStatus: 'created' as const
      }),
      createdByAccountId: cache.createdByAccountId,
      filterFingerprint: cache.filterFingerprint,
      visibilityFingerprint: cache.visibilityFingerprint,
      dataCapturedAt: cache.dataCapturedAt,
      reuseStatus: account.id === cache.createdByAccountId ? 'created' : 'reused',
      reuseReason: account.id === cache.createdByAccountId
        ? '复用当前账号刚建立的预览候选池。'
        : `复用账号 ${cache.createdByAccountId} 的预览候选池。`
    }
  }]))
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
  const globalThresholds = (globalSnapshot.thresholds ?? {}) as Record<string, unknown>
  if (globalThresholds.commissionRate != null && globalSnapshot.commissionRateRule !== 'minimum') {
    throw new Error('旧批次佣金率曾按上限保存；请核对新最低值并新建批次，旧检查点已保留。')
  }
  const categoryTree = JSON.parse(JSON.stringify(categories.value)) as CategoryNode[]
  const globalCategory = (globalSnapshot.categorySelection ?? {}) as Record<string, string[]>
  const globalExcludedCategory = (globalSnapshot.excludedCategorySelection ?? {}) as Record<string, string[]>
  const storedCategory = (accountSettings.categorySelection ?? {}) as Record<string, string[]>
  const storedExcludedCategory = (accountSettings.excludedCategorySelection ?? {}) as Record<string, string[]>
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
  const normalizedGlobalExcludedCategory = normalizeExcludedCategorySelection({
    level1: [...(globalExcludedCategory.level1 ?? [])],
    level2: [...(globalExcludedCategory.level2 ?? [])],
    level3: [...(globalExcludedCategory.level3 ?? [])]
  }, categoryTree)
  const normalizedAccountExcludedCategory = normalizeExcludedCategorySelection({
    level1: [...(storedExcludedCategory.level1 ?? [])],
    level2: [...(storedExcludedCategory.level2 ?? [])],
    level3: [...(storedExcludedCategory.level3 ?? [])]
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
  settings.excludedCategorySelection = accountSelectionMode === 'inherit'
    ? normalizedGlobalExcludedCategory
    : (Object.prototype.hasOwnProperty.call(accountSettings, 'excludedCategorySelection')
      ? normalizedAccountExcludedCategory
      : normalizedGlobalExcludedCategory)
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
    const override = filterOverrides as Record<string, unknown>
    const overrideThresholds = (override.thresholds ?? {}) as Record<string, unknown>
    if (overrideThresholds.commissionRate != null && override.commissionRateRule !== 'minimum') {
      throw new Error('该账号旧佣金率按上限保存；请在账号个性化设置中核对最低值并重新保存。')
    }
    Object.assign(settings, createSelectionFilterSettings(filterOverrides, createSelectionFilterSettings(globalSnapshot)))
  }
  validateCommissionRange(
    ((settings.thresholds ?? {}) as Record<string, number | null>).commissionRate,
    ((settings.thresholds ?? {}) as Record<string, number | null>).commissionRateMax,
    '账号运行设置'
  )
  validateCandidateSources(createSelectionFilterSettings(settings).sources, '账号运行设置')
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
  const rawDownloadPath = configuredDownloadPath || defaultRoot + '\\' + defaultAccountDownloadFolderName(targetAccountId)
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
  candidatePlan?: AccountCandidatePlan,
  poolConsumerOnly = false
): Promise<{ status: '完成' | '部分完成'; message?: string; continueBatch?: boolean }> {
  let stage = '读取账号设置'
  let recoveryStage = recovery?.state.stage ?? 'quota_check'
  let categoryWarnings: string[] = []
  let consumer11001Reason = ''
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
    const stopCurrentAccountForPause = async (checkpointStage: string): Promise<{
      status: '部分完成'; message: string; continueBatch: false
    } | null> => {
      if (!recovery || !manualPausePending()) return null
      if (consumer11001Reason) {
        const history = await runRecovery(['batch', 'logs', '--batch-id', recovery.batchId])
        const { code, label, cooldownSeconds } = consumer11001RetryPlan(
          consumer11001Reason, history.runLogs, targetAccountId
        )
        const reason = `${label}；客户端自动续选冷却约 ${cooldownSeconds} 秒。手动暂停后需点击“继续选品”；${consumer11001Reason}`
        const retained = completed.filter((item) => item.cartAdded === true ||
          (item.cartAttempted === true && item.skipped !== true))
        await recovery.logEvent({
          stage: '消费账号商品搜索', code,
          message: `${label}；手动暂停，冷却约 ${cooldownSeconds} 秒后仍须用户继续。`,
          detail: consumer11001Reason
        })
        await recovery.persist({
          state: 'needs_retry', stage: 'cart_pending', error: reason,
          candidates: [], selectedIndexes: [], completed: retained
        })
        accountStatuses[targetAccountId] = `手动暂停（11001 等待 ${cooldownSeconds} 秒）`
        return { status: '部分完成', message: reason, continueBatch: false }
      }
      const message = '已手动暂停；当前操作结果和账号检查点已保存，等待点击“继续选品”。'
      await recovery.persist({
        state: 'needs_retry', stage: checkpointStage, error: message,
        candidates: fetched, selectedIndexes, completed
      })
      accountStatuses[targetAccountId] = '手动暂停（待继续）'
      return { status: '部分完成', message, continueBatch: false }
    }
    const savedStage = recovery?.state.stage ?? ''
    const recoveryMode = getSelectionRecoveryMode(
      savedStage,
      fetched.length > 0 && selectedIndexes.length > 0,
      completed
    )
    if (poolConsumerOnly && candidatePlan && !recoveryMode.resumeCart && !recoveryMode.resumeAfterCart) {
      // 旧恢复路径曾误入三来源预览，把缺类目候选记为“已跳过”。这些商品尚未
      // 用消费账号按 ID 打开详情，不能让旧来源判断代替本账号的详情核验。
      const before = completed.length
      completed = completed.filter((item) => !(
        item.analysisRejected === true && item.skipped === true &&
        item.cartAttempted !== true && item.cartAdded !== true &&
        Array.isArray(item.thresholdReasons) && item.thresholdReasons.some((reason) =>
          String(reason).startsWith('类目未知：已尝试来源的详情入口或类目字段连续抽样后仍无法核实')
        )
      ))
      if (before !== completed.length) {
        await recovery?.logEvent({
          stage: '候选池恢复', code: 'INFO_POOL_SOURCE_REJECTIONS_RECHECKED',
          message: `旧来源类目判断涉及 ${before - completed.length} 个商品；保留原过程日志，改由当前账号按商品 ID 核验详情。`
        })
        await recovery?.persist({ completed })
      }
    }
    const accountLabel = accounts.value.find((account) => account.id === targetAccountId)?.name ?? `账号 ${targetAccountId}`
    reportSelectionProgress(targetAccountId, '准备账号选品任务', {
      percent: candidatePlan ? 100 : 0,
      message: candidatePlan ? '已复用统一候选、筛选和账号互斥计划；准备执行配额复核。' : '检查账号设置和本地类目计划'
    })
    let settings = recovery
      ? settingsForRecoveryCheckpoint(context.settings, recovery.state, accountLabel)
      : context.settings
    settings = { ...settings, _visibleSourcesOnly: true, _poolConsumerOnly: poolConsumerOnly }
    if (!previewOnly) {
      const blockedCardIndexes = new Set<number>()
      const loggedTitleIds = new Set<string>()
      const newlyBlockedCompleted: Array<{ item: Record<string, unknown>; word: string }> = []
      completed = completed.map((item) => {
        const word = matchedSelectionTitleBannedWord(item.title, settings)
        if (!word) return item
        const productId = String(item.productId ?? '')
        if (item.titleFilterExcluded !== true) newlyBlockedCompleted.push({ item, word })
        return {
          ...item,
          titleBannedWord: word,
          titleFilterExcluded: true,
          cartAdded: item.cartAdded === true,
          skipped: true,
          status: `商品标题命中屏蔽词「${word}」；已停止本轮商品恢复处理。`
        }
      })
      for (const { item, word } of newlyBlockedCompleted) {
        const productId = String(item.productId ?? '')
        loggedTitleIds.add(productId)
        await recovery?.logEvent({
          stage: '商品标题屏蔽', code: 'WARNING_TITLE_BANNED_WORD',
          message: `${String(item.title ?? productId)}：商品标题命中屏蔽词「${word}」，已跳过。`,
          detail: word, productId, productTitle: String(item.title ?? '')
        })
      }
      const allowedCandidates: SelectionCandidate[] = []
      for (const candidate of fetched) {
        const word = matchedSelectionTitleBannedWord(candidate.title, settings)
        if (!word) {
          allowedCandidates.push(candidate)
          continue
        }
        if (Number.isInteger(candidate.cardIndex)) blockedCardIndexes.add(candidate.cardIndex)
        const productId = String(candidate.productId ?? '')
        const prior = completed.find((item) => String(item.productId ?? '') === productId)
        const blocked = {
          ...prior,
          ...candidate,
          titleBannedWord: word,
          titleFilterExcluded: true,
          cartAdded: prior?.cartAdded === true || candidate.cartAdded === true,
          skipped: true,
          status: `商品标题命中屏蔽词「${word}」；已停止本轮商品处理。`
        }
        if (prior) {
          completed = completed.map((item) => String(item.productId ?? '') === productId ? blocked : item)
        } else {
          completed.push(blocked)
        }
        if (!loggedTitleIds.has(productId)) {
          loggedTitleIds.add(productId)
          await recovery?.logEvent({
            stage: '商品标题屏蔽', code: 'WARNING_TITLE_BANNED_WORD',
            message: `${String(candidate.title ?? productId)}：商品标题命中屏蔽词「${word}」，已跳过。`,
            detail: word, productId, productTitle: String(candidate.title ?? '')
          })
        }
      }
      fetched = allowedCandidates
      selectedIndexes = selectedIndexes.filter((index) => !blockedCardIndexes.has(index))
    }
    notice.value = settings.selectionMode === 'category' && settings.categoryMode === 'manual'
      ? '选品仍只读取百应当前可见商品，并按手动选择的类目范围筛选。'
      : settings.selectionMode === 'category'
        ? '三个百应来源统一筛选；品类模式沿用自动推荐逻辑。'
        : '选品广场、热点趋势和联盟榜单按同一套数据门槛统一筛选；默认全部类目，并应用通用设置中的排除类目。'
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
      const candidatePool = previewPlan.candidatePool ?? found
      previewCandidatePoolCount.value = candidatePool.length
      previewCandidatePool.value = candidatePool.length
        ? {
            createdByAccountId: targetAccountId,
            filterFingerprint: selectionFilterFingerprint(context.settings),
            visibilityFingerprint: selectionPoolVisibilityFingerprint(context.resolvedProfilePath ?? context.profilePath),
            dataCapturedAt: new Date().toISOString(),
            plan: {
              ...previewPlan,
              sharedCandidatePool: previewPlan.sharedCandidatePool ?? candidatePool,
              candidatePoolMeta: previewPlan.candidatePoolMeta ?? {
                createdByAccountId: targetAccountId,
                filterFingerprint: selectionFilterFingerprint(context.settings),
                visibilityFingerprint: selectionPoolVisibilityFingerprint(context.resolvedProfilePath ?? context.profilePath),
                dataCapturedAt: new Date().toISOString(),
                reuseStatus: 'created'
              }
            }
          }
        : null
      categoryRecommendations.value = previewPlan.recommendedCategories
      if (accountId.value === targetAccountId) {
        candidates.value = candidatePool.map((candidate) => ({ ...candidate, selected: true }))
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
        warnings.unshift(`选品登记、配额或商品 ID 互斥过滤 ${locallySkipped} 个候选${skippedDetails ? `；示例：${skippedDetails}` : ''}。`)
      }
      const rejected = previewPlan.rejectedCandidates
      const excludedByCategoryCount = rejected.filter((candidate) =>
        candidate.thresholdReasons?.includes('命中当前设置中排除的类目')
      ).length
      const titleFilteredCount = rejected.filter((candidate) =>
        candidate.titleFilterExcluded === true || Boolean(candidate.titleBannedWord)
      ).length
      const thresholdRejectedCount = Math.max(0, rejected.length - excludedByCategoryCount - titleFilteredCount)
      const firstThresholdReason = rejected.find((candidate) =>
        !candidate.thresholdReasons?.includes('命中当前设置中排除的类目') &&
        candidate.titleFilterExcluded !== true && !candidate.titleBannedWord
      )?.thresholdReasons?.[0]
      const firstTitleFilter = rejected.find((candidate) =>
        candidate.titleFilterExcluded === true || Boolean(candidate.titleBannedWord)
      )
      const rejectionSummary = [
        titleFilteredCount
          ? `标题屏蔽排除 ${titleFilteredCount} 个候选${firstTitleFilter?.titleBannedWord ? `，示例命中词：${firstTitleFilter.titleBannedWord}` : ''}`
          : '',
        thresholdRejectedCount
          ? `另有 ${thresholdRejectedCount} 个候选因列表实测数据未达阈值被排除${firstThresholdReason ? `，示例：${firstThresholdReason}` : ''}`
          : '',
        excludedByCategoryCount ? `排除类目过滤 ${excludedByCategoryCount} 个候选` : ''
      ].filter(Boolean).map((item) => `；${item}`).join('')
      const previewSourceResults = previewPlan.prioritySourceResults
      const categorySummary = previewPlan.recommendedCategories.length
        ? `重点类目推荐依据：${previewPlan.recommendedCategories.slice(0, 8).map((row) =>
            `${categoryName(row.categoryId)}（高分商品 ${row.highScoreCount} 个，平均统一分 ${row.averageScore}）`
          ).join('；')}`
        : '无可推荐类目：当前通过筛选的候选没有可识别完整三级类目 ID。类目未知商品不会加车。'
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
      const candidateReadFingerprint = (candidatesRead: unknown, rejectedRead: unknown): string => {
        const rows = [
          ...(Array.isArray(candidatesRead) ? candidatesRead : []),
          ...(Array.isArray(rejectedRead) ? rejectedRead : [])
        ]
        return [...new Set(rows.map((row) =>
          row && typeof row === 'object' ? String((row as { productId?: unknown }).productId ?? '').trim() : ''
        ).filter(Boolean))].sort().join('|')
      }
      let lastReadFingerprint = candidateReadFingerprint(fetched, completed)
      let sourcePoolState: NonNullable<SelectionRecoveryAccount['sourcePoolState']> =
        recovery?.state.sourcePoolState ?? { sources: {}, samePoolCount: 0 }
      const cooldownRemaining = (): number => sourcePoolState.nextRetryAt
        ? Math.max(0, new Date(sourcePoolState.nextRetryAt).getTime() - Date.now()) : 0
      const resetExpiredPool = async (): Promise<void> => {
        if (!sourcePoolState.nextRetryAt || cooldownRemaining() > 0) return
        sourcePoolState = { sources: {}, samePoolCount: 0, detailAttemptedIds: [], tabOffsets: {} }
        await recovery?.persist({ sourcePoolState })
      }
      const checkpointSourcePool = async (
        results: PrioritySourceReadResult[], unchanged: boolean,
        enrichment?: { detailUnresolvedProductIds?: string[]; deferredCount?: number }
      ): Promise<void> => {
        const sources = Object.fromEntries(results
          .filter((item) => item.status === 'read' || item.status === 'empty')
          .map((item) => [item.sourceKey, {
            fingerprint: String(item.productIdFingerprint || ''),
            productCount: Math.max(0, Number(item.productCount) || 0),
            readAt: String(item.readAt || new Date().toISOString()),
            pagePath: String(item.pagePath || ''),
            scrollPosition: Number(typeof item.scrollPosition === 'object'
              ? item.scrollPosition?.top : item.scrollPosition) || 0
          }]))
        const prior = sourcePoolState.sources ?? {}
        const sameSources = Object.keys(sources).length > 0 &&
          Object.keys(sources).every((key) => sources[key].fingerprint &&
            sources[key].fingerprint === prior[key]?.fingerprint)
        const tabOffsets = { ...(sourcePoolState.tabOffsets ?? {}) }
        for (const item of results) {
          if (Number.isInteger(item.categoryTabNextOffset)) tabOffsets[item.sourceKey] = Number(item.categoryTabNextOffset)
        }
        const detailAttemptedIds = [...new Set([
          ...(sourcePoolState.detailAttemptedIds ?? []),
          ...(Array.isArray(enrichment?.detailUnresolvedProductIds) ? enrichment.detailUnresolvedProductIds : [])
        ].map(String).filter((id) => /^\d+$/.test(id)))].slice(-200)
        const hasPendingWork = Number(enrichment?.deferredCount ?? 0) > 0 || results.some((item) =>
          item.status === 'skipped' || Number(item.categoryTabNextOffset ?? 0) < Number(item.categoryTabTotal ?? 0)
        )
        const samePoolCount = unchanged && sameSources && !hasPendingWork ? sourcePoolState.samePoolCount + 1 : 0
        const nextRetryAt = samePoolCount > 0
          ? new Date(Date.now() + Math.min(30, 5 * 2 ** (samePoolCount - 1)) * 60_000).toISOString()
          : undefined
        sourcePoolState = { sources, samePoolCount, nextRetryAt, detailAttemptedIds, tabOffsets }
        await recovery?.persist({ sourcePoolState })
      }
      const refillCandidatePool = async (force = false): Promise<void> => {
        if (poolConsumerOnly) return
        if (hasExplicitPreviewSelection || (candidatePlan && !force) || (!force && fetched.length >= candidateLimit)) return
        if (cooldownRemaining() > 0) return
        await resetExpiredPool()
        const refillGoal = force ? fetched.length + candidateLimit : candidateLimit
        const retryDelays = [1_000, 2_500, 6_000]
        for (let attempt = 0; attempt < retryDelays.length && fetched.length < refillGoal; attempt += 1) {
          const delayMs = retryDelays[attempt]
          accountStatuses[targetAccountId] = `candidate_refilling（${fetched.length}/${candidateLimit}）`
          reportSelectionProgress(targetAccountId, 'candidate_refilling', {
            itemIndex: fetched.length,
            itemTotal: candidateLimit,
            message: `账号 ${targetAccountId}：已找到 ${fetched.length}/${candidateLimit}，第 ${attempt + 1} 次补充候选；${delayMs}ms 后继续读取已启用来源`
          })
          await recovery?.persist({ state: 'in_progress', stage: 'candidate_refilling', error: '', candidates: fetched })
          await recovery?.logEvent({
            stage: '候选补充', code: 'INFO_CANDIDATE_REFILLING',
            message: `候选不足 ${fetched.length}/${candidateLimit}，第 ${attempt + 1} 次补充；等待 ${delayMs}ms。`,
            detail: `沿用当前账号页面和筛选指纹；不放宽条件，不推进下一个账号。`
          })
          await new Promise((resolve) => setTimeout(resolve, delayMs))
          const refill = await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
            ...profile,
            settings: {
              ...settings, _candidatePoolLimit: Math.min(200, candidateLimit + (attempt + 1) * 40),
              _categoryDetailSkipIds: sourcePoolState.detailAttemptedIds ?? [],
              _sourceCategoryTabOffsets: sourcePoolState.tabOffsets ?? {}
            },
            accountId: targetAccountId,
            downloadPath: context.downloadPath,
            previewOnly: true,
            closeAfterAutomation: !recovery
          }))
          if (refill.ok !== true) continue
          const queriedRows = Array.isArray(refill.candidates) ? refill.candidates as SelectionCandidate[] : []
          const registryRefill = await filterByLocalRegistry(
            registryAccountKey(targetAccountId), queriedRows, candidateLimit,
            settings.selectionMode === 'category' ? 'category' : 'general'
          )
          const newRows = registryRefill.candidates
          const readFingerprint = candidateReadFingerprint(refill.candidates, refill.rejectedCandidates)
          const refillSources = Array.isArray(refill.prioritySourceResults)
            ? refill.prioritySourceResults as PrioritySourceReadResult[] : []
          const refillEnrichment = refill.categoryEnrichment && typeof refill.categoryEnrichment === 'object'
            ? refill.categoryEnrichment as AccountCandidatePlan['categoryEnrichment'] : undefined
          const previousCount = fetched.length
          const byProduct = new Map(fetched.map((row) => [row.productId, row]))
          for (const row of newRows) if (!byProduct.has(row.productId)) byProduct.set(row.productId, row)
          fetched = [...byProduct.values()].map((row, index) => ({ ...row, cardIndex: index }))
          const hasPendingWork = Number(refillEnrichment?.deferredCount ?? 0) > 0 || refillSources.some((item) =>
            item.status === 'skipped' || Number(item.categoryTabNextOffset ?? 0) < Number(item.categoryTabTotal ?? 0)
          )
          const sameSourceFingerprints = refillSources.some((item) => Boolean(item.productIdFingerprint)) &&
            refillSources.filter((item) => item.status === 'read' || item.status === 'empty').every((item) =>
              Boolean(item.productIdFingerprint) &&
              item.productIdFingerprint === sourcePoolState.sources?.[item.sourceKey]?.fingerprint
            )
          const unchanged = fetched.length === previousCount && !hasPendingWork &&
            (Boolean(readFingerprint) && readFingerprint === lastReadFingerprint || sameSourceFingerprints)
          await checkpointSourcePool(refillSources, unchanged, refillEnrichment)
          await recovery?.logEvent({
            stage: '候选补充', code: 'INFO_CANDIDATE_REFILL_RESULT',
            message: `第 ${attempt + 1} 次补充后候选 ${fetched.length}/${candidateLimit}。`,
            detail: `本次新读取 ${queriedRows.length} 条，选品登记过滤 ${registryRefill.excluded.length} 条，按商品 ID 去重后 ${fetched.length} 条。`
          })
          if (unchanged) {
            await recovery?.logEvent({
              stage: '候选补充', code: 'WARNING_CANDIDATE_POOL_UNCHANGED',
              message: '再次读取仍是同一批商品 ID，已停止本轮无效补读。',
              detail: `当前可执行候选 ${fetched.length}/${candidateLimit}；保留缺额和检查点，不放宽条件。下次重查 ${sourcePoolState.nextRetryAt || '待手动恢复'}。`
            })
            break
          }
          if (readFingerprint) lastReadFingerprint = readFingerprint
        }
        if (fetched.length < candidateLimit) {
          await recovery?.persist({ state: 'in_progress', stage: 'candidate_refilling', error: `候选补充后仍只有 ${fetched.length}/${candidateLimit}，未放宽筛选条件。`, candidates: fetched })
        }
      }
      if (resumeCart || fetched.length > 0) await refillCandidatePool()
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
        const productLocks = await loadProductPublishLocks(
          targetAccountId,
          remainingCandidates.map((candidate) => candidate.productId)
        )
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
        // candidateLimit controls candidate discovery; an account task must drain
        // its complete remaining snapshot before the batch advances to the next account.
        const remainingBatchTarget = poolConsumerOnly
          ? Math.max(0, Math.min(remainingAccountQuota, candidateLimit - alreadyAddedCount))
          : Math.max(0, remainingAccountQuota)
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

        if (cooldownRemaining() > 0 && fetched.length < candidateLimit) {
          const reason = `来源连续返回同一商品池；保留 ${fetched.length}/${candidateLimit} 缺额，${sourcePoolState.nextRetryAt} 后再查。`
          await recovery?.persist({ state: 'needs_retry', stage: 'candidate_refilling', error: reason, candidates: fetched })
          return { status: '部分完成', message: reason, continueBatch: false }
        }
        await resetExpiredPool()

        await persistStage('candidate_fetch')
        stage = '百应筛选并获取候选'
        accountStatuses[targetAccountId] = '获取候选中'
        const candidateQuerySettings = hasExplicitPreviewSelection
          ? settings
          : {
              ...settings, _candidatePoolLimit: 200,
              _categoryDetailSkipIds: sourcePoolState.detailAttemptedIds ?? [],
              _sourceCategoryTabOffsets: sourcePoolState.tabOffsets ?? {}
            }
        const preview = candidatePlan
          ? {
              ok: true,
              candidates: candidatePlan.candidates,
              rejectedCandidates: candidatePlan.rejectedCandidates,
              warnings: candidatePlan.warnings,
              prioritySourceResults: candidatePlan.prioritySourceResults,
              recommendedCategories: candidatePlan.recommendedCategories,
              categoryEnrichment: candidatePlan.categoryEnrichment
            }
          : await window.api.browser.runBaiyingSelection(toPlainSelectionPayload({
              ...profile, settings: candidateQuerySettings, accountId: targetAccountId,
              downloadPath: context.downloadPath, previewOnly: true, closeAfterAutomation: !recovery
            }))
        if (preview.ok !== true) throw new Error(String(preview.error ?? '百应候选获取失败'))
        const previousReadFingerprint = lastReadFingerprint
        lastReadFingerprint = candidateReadFingerprint(preview.candidates, preview.rejectedCandidates)
        fetched = Array.isArray(preview.candidates) ? preview.candidates as SelectionCandidate[] : []
        const stableAccountKey = registryAccountKey(targetAccountId)
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
        }
        const dedupeEvents: SelectionRunLogInput[] = allRegistryExcluded.map(({ candidate, reason }) => ({
            stage: '选品登记', code: 'INFO_REGISTRY_CANDIDATE_EXCLUDED',
            message: `${String(candidate.title ?? candidate.productId)}：${reason}`,
            detail: reason, productId: String(candidate.productId ?? ''), productTitle: String(candidate.title ?? '')
        }))
        for (const candidate of candidatePlan?.mutualExclusionSkipped ?? []) {
          const reason = String(candidate.mutualExclusionReason || '商品已由统一分更高的账号占用')
          completed.push({ ...candidate, cartAdded: false, skipped: true, mutualExclusionSkipped: true, status: reason })
          dedupeEvents.push({
            stage: '跨账号商品互斥', code: 'INFO_CROSS_ACCOUNT_PRODUCT_SKIPPED',
            message: `${String(candidate.title ?? candidate.productId)}：${reason}`,
            detail: reason, productId: String(candidate.productId ?? ''), productTitle: String(candidate.title ?? '')
          })
        }
        await recovery?.logEvents(dedupeEvents)
        if (fetched.length && fetched[0]) {
          const topCategory = candidateLeafCategoryId(fetched[0])
          notice.value = topCategory
            ? `本机去重后保留 ${fetched.length} 个候选；系统已按高分商品数量、平均分和跨账号类目错开偏好排序，重点末级类目 ID：${topCategory}。`
            : `本机去重后保留 ${fetched.length} 个候选；类目由系统自动推荐。`
        }
        const prioritySourceResults = Array.isArray(preview.prioritySourceResults)
          ? preview.prioritySourceResults as PrioritySourceReadResult[] : []
        await checkpointSourcePool(
          prioritySourceResults,
          Boolean(previousReadFingerprint && lastReadFingerprint === previousReadFingerprint && fetched.length < candidateLimit),
          preview.categoryEnrichment && typeof preview.categoryEnrichment === 'object'
            ? preview.categoryEnrichment as AccountCandidatePlan['categoryEnrichment'] : undefined
        )
        const prioritySourceMatchStatus = String(preview.prioritySourceMatchStatus || 'complete')
        const listRejected = Array.isArray(preview.rejectedCandidates)
          ? preview.rejectedCandidates as SelectionCandidate[] : []
        const registryWarnings = Array.isArray(preview.warnings)
          ? preview.warnings.map(String).filter((warning) =>
            warning.startsWith('共享选品登记服务暂时不可达') || warning.startsWith('服务器同步发现'))
          : []
        for (const warning of registryWarnings) {
          await recovery?.logEvent({
            stage: '选品登记同步',
            code: warning.startsWith('服务器同步发现') ? 'WARNING_REGISTRY_OFFLINE_CONFLICT' : 'WARNING_REGISTRY_OFFLINE_MODE',
            message: warning,
            detail: '本机商品 ID 登记继续生效；离线期间无法保证其他客户端实时占用互斥，联网后会同步并排除冲突商品。'
          })
        }
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
        const poolMeta = candidatePlan?.candidatePoolMeta
        if (poolMeta) {
          const poolMessage = poolMeta.reuseStatus === 'reused'
            ? `复用账号 ${poolMeta.createdByAccountId} 创建的候选池；筛选条件指纹 ${poolMeta.filterFingerprint.slice(0, 12)}；数据时间 ${poolMeta.dataCapturedAt}。`
            : `账号 ${poolMeta.createdByAccountId} 创建候选池；筛选条件指纹 ${poolMeta.filterFingerprint.slice(0, 12)}；数据时间 ${poolMeta.dataCapturedAt}。`
          await recovery?.logEvent({
            stage: '候选池复用',
            code: poolMeta.reuseStatus === 'reused' ? 'INFO_CANDIDATE_POOL_REUSED' : 'INFO_CANDIDATE_POOL_CREATED',
            message: poolMessage,
            detail: [
              `来源可见范围指纹 ${poolMeta.visibilityFingerprint ? poolMeta.visibilityFingerprint.slice(-24) : '未确认'}`,
              poolMeta.reuseReason || ''
            ].filter(Boolean).join('；')
          })
        }
        for (const candidate of listRejected) {
          const productId = String(candidate.productId ?? '')
          if (!productId || completed.some((item) => String(item.productId ?? '') === productId)) continue
          const thresholdReasons = Array.isArray(candidate.thresholdReasons)
            ? candidate.thresholdReasons.map(String) : []
          const titleBannedWord = String(candidate.titleBannedWord ?? '').trim()
          if (titleBannedWord) {
            const reason = `商品标题命中屏蔽词「${titleBannedWord}」`
            completed.push({
              ...candidate,
              titleFilterExcluded: true,
              analysisStatus: '商品标题命中屏蔽词',
              thresholdReasons,
              cartAdded: false,
              skipped: true,
              status: reason
            })
            await recovery?.logEvent({
              stage: '商品标题屏蔽', code: 'WARNING_TITLE_BANNED_WORD',
              message: `${String(candidate.title ?? productId)}：${reason}，已排除且未加车。`,
              detail: reason, productId, productTitle: String(candidate.title ?? '')
            })
            continue
          }
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
        const productLocks = await loadProductPublishLocks(
          targetAccountId,
          fetched.filter((candidate) => chosenProducts.has(candidate.productId)).map((candidate) => candidate.productId)
        )
        const lockedCandidates = fetched.filter((candidate) =>
          chosenProducts.has(candidate.productId) && productLocks.has(candidate.productId)
        )
        const duplicateSkips = duplicateProductSkipItems(lockedCandidates, productLocks)
        await logDuplicateProductSkips(targetAccountId, duplicateSkips, recovery)
        completed = [...completed.filter((item) =>
          !duplicateSkips.some((skipped) => String(item.productId ?? '') === skipped.productId)
        ), ...duplicateSkips]
        // Run every eligible product for this account in one account task. The
        // account quota remains the hard upper bound; candidateLimit only limits
        // how many candidates are discovered upstream.
        const previouslyAddedCount = completed.filter((item) => item.cartAdded === true).length
        maxSelectedProducts = poolConsumerOnly
          ? Math.max(0, Math.min(maxProducts, candidateLimit - previouslyAddedCount))
          : maxProducts
        eligible = fetched.filter((candidate) =>
          chosenProducts.has(candidate.productId) && !productLocks.has(candidate.productId) &&
          (usage[candidate.productId] ?? 0) < quotaSettings.perProductVideos
        )
        if (eligible.length < candidateLimit && !hasExplicitPreviewSelection && !poolConsumerOnly) {
          await refillCandidatePool(true)
          const eligibleProductIds = new Set([...chosenProducts, ...fetched.map((candidate) => candidate.productId)])
          eligible = fetched.filter((candidate) =>
            eligibleProductIds.has(candidate.productId) && !productLocks.has(candidate.productId) &&
            (usage[candidate.productId] ?? 0) < quotaSettings.perProductVideos
          )
        }
        if (!eligible.length) {
          const titleFilterSkipped = completed.filter((item) => item.titleFilterExcluded === true)
          const analysisSkipped = completed.filter((item) => item.analysisRejected === true)
          const globallySkipped = completed.filter((item) =>
            item.mutualExclusionSkipped === true || item.registryExcluded === true
          )
          const categoryUnknownCount = analysisSkipped.filter((item) =>
            Array.isArray(item.thresholdReasons) && item.thresholdReasons.some((reason) => String(reason).startsWith('类目未知'))
          ).length
          const confirmedCartCount = completed.filter((item) => item.cartAdded === true).length
          const reason = [
            `当前可执行候选 0/${candidateLimit}，确认加车 ${confirmedCartCount}/${candidateLimit}；当前账号未达目标，保留检查点，不推进下一个账号。`,
            categoryUnknownCount ? `类目无法核实 ${categoryUnknownCount} 个` : '',
            analysisSkipped.length > categoryUnknownCount ? `其他数据筛选排除 ${analysisSkipped.length - categoryUnknownCount} 个` : '',
            titleFilterSkipped.length ? `标题屏蔽 ${titleFilterSkipped.length} 个` : '',
            globallySkipped.length ? `登记或互斥跳过 ${globallySkipped.length} 个` : '',
            duplicateSkips.length ? `重复发布锁跳过 ${duplicateSkips.length} 个` : '',
            categoryWarnings.length ? `来源读取问题 ${categoryWarnings.length} 项` : ''
          ].filter(Boolean).join('；')
          accountStatuses[targetAccountId] = `needs_retry（候选 0/${candidateLimit}，加车 ${confirmedCartCount}/${candidateLimit}）`
          await recovery?.logEvent({
            stage: '候选补充', code: 'WARNING_CANDIDATE_TARGET_UNMET',
            message: `当前账号没有可执行候选，确认加车 ${confirmedCartCount}/${candidateLimit}，已保留缺额。`,
            detail: reason
          })
          await recovery?.persist({
            state: 'needs_retry', stage: 'candidate_refilling', error: reason,
            candidates: fetched, selectedIndexes: [], completed
          })
          return { status: '部分完成', message: reason, continueBatch: false }
        }
        selectedIndexes = eligible.map((candidate) => candidate.cardIndex)
        selectedIndexesToExecute = selectedIndexes
        reportSelectionProgress(targetAccountId, '准备逐个处理选中商品', {
          itemIndex: 0,
          itemTotal: selectedIndexesToExecute.length,
          message: hasExplicitPreviewSelection
            ? `人工勾选候选 ${selectedIndexesToExecute.length} 个；本轮最多加入 ${maxSelectedProducts} 个`
            : `主候选 ${selectedIndexesToExecute.length} 个；登记冲突时自动从筛选池补位，最多加入 ${maxSelectedProducts} 个`
        })
        await persistStage('cart_pending', {
          candidates: fetched,
          selectedIndexes,
          completed: completed.length ? completed : null
        })
      }

      await persistStage('cart_pending', { candidates: fetched, selectedIndexes, completed: completed.length ? completed : null })
      const pausedBeforeCart = await stopCurrentAccountForPause('cart_pending')
      if (pausedBeforeCart) return pausedBeforeCart
      if (selectedIndexesToExecute.length) {
        await recovery?.logEvent({
          stage: '候选池准备', code: 'INFO_CANDIDATE_POOL_READY',
          message: hasExplicitPreviewSelection
            ? `已找到 ${selectedIndexesToExecute.length} 个用户勾选候选（含所选备用项）；登记冲突会跳过并尝试其他勾选项，最多加入 ${maxSelectedProducts} 个。`
            : `已找到 ${selectedIndexesToExecute.length} 个主候选；遇到登记冲突会按本批随机排序从候选快照补位，单轮最多检查 ${BATCH_FALLBACK_CANDIDATE_LIMIT} 个候选、加入 ${maxSelectedProducts} 个。`,
          detail: hasExplicitPreviewSelection
            ? '本次优先使用人工勾选的候选；选品登记拒绝时仍会尝试其他已选候选。'
            : '先按选品登记、商品锁和配额过滤，再按批次 ID 对合格商品加权随机排序；候选池按全账号商品 ID 互斥分配。正式原子预留遇到并发冲突时会继续检查备用候选；筛选质量、账号归属和最终事务保护不变。'
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
          accountKey: registryAccountKey(targetAccountId),
          downloadPath: context.downloadPath,
          selectedIndexes: selectedIndexesToExecute,
          candidateSnapshot: executionCandidates,
          allowCandidateFallback: !hasExplicitPreviewSelection && !resumeCart,
          refreshCandidatesById: resumeCart,
          analyzeBeforeCart: !resumeAfterCart,
          maxSelectedProducts,
          previewOnly: false,
          closeAfterAutomation: !recovery
        }))
        if (result.ok !== true) throw new Error(String(result.error ?? '百应选品执行失败'))
        consumer11001Reason = poolConsumerOnly ? String(result.poolConsumerRateLimit ?? '') : ''

        const registryRejected = Array.isArray(result.registryRejected)
          ? result.registryRejected as Array<{ productId: string; reason: string }> : []
        for (const rejected of registryRejected) {
          await recovery?.logEvent({
            stage: '选品登记', code: 'WARNING_REGISTRY_CONFLICT',
            message: `商品 ${rejected.productId} 未获预留，已跳过：${rejected.reason}`,
            detail: rejected.reason, productId: rejected.productId
          })
        }
        const registryReplacementCount = Math.max(0, Number(result.registryReplacementCount) || 0)
        if (registryReplacementCount) {
          await recovery?.logEvent({
            stage: '选品登记', code: 'INFO_REGISTRY_CANDIDATE_REPLACED',
            message: `已自动从后续候选补位 ${registryReplacementCount} 个，继续完成本轮选品。`,
            detail: '前序候选因本机重复登记或类目冲突未能预留；补位商品仍经过同一筛选和加车核验。'
          })
        }
        const settlementWarnings = Array.isArray(result.registrySettlementWarnings)
          ? result.registrySettlementWarnings.map(String) : []
        for (const warning of settlementWarnings) {
          await recovery?.logFailure({
            stage: '选品登记', code: 'REGISTRY_SETTLEMENT_PENDING',
            message: '百应结果已返回，但选品登记结算尚未确认；相关商品保持锁定待核查。',
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
            stage: '选品登记',
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
          const titleFilterExcluded = item.titleFilterExcluded === true
          const productId = String(item.productId ?? '')
          const productTitle = String(item.title ?? '')
          if (titleFilterExcluded) {
            const reason = String(item.status || `商品标题命中屏蔽词「${String(item.titleBannedWord ?? '')}」`)
            await recovery?.logEvent({
              stage: '商品标题屏蔽', code: 'WARNING_TITLE_BANNED_WORD',
              message: `${productTitle || productId}：${reason}`,
              detail: Array.isArray(item.thresholdReasons) ? item.thresholdReasons.map(String).join('\n') : reason,
              productId, productTitle
            })
          } else if (analysisRejected) {
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
        const mergedReturned = returned.map((item) => {
          const prior = priorById.get(String(item.productId ?? ''))
          // 重新通过筛选的商品不能继承上次“类目未知”等淘汰标记。
          return { ...(prior?.skipped === true ? {} : prior ?? {}), ...item }
        })
        completed = [...completed.filter((item) => !returnedIds.has(String(item.productId ?? ''))), ...mergedReturned]
        const returnedByProductId = new Map<string, Record<string, unknown>>(returned
          .filter((item) => String(item.productId ?? ''))
          .map((item) => [String(item.productId), item] as const))
        fetched = fetched.map((candidate) => {
          const analysis = returnedByProductId.get(candidate.productId)
          return analysis ? { ...candidate, ...analysis } as SelectionCandidate : candidate
        })
        const consumerRejectedIds = poolConsumerOnly
          ? [...new Set(returned.filter((item) => item.analysisRejected === true || item.titleFilterExcluded === true)
            .map((item) => String(item.productId ?? '').trim()).filter((id) => /^\d+$/.test(id)))]
          : []
        if (consumerRejectedIds.length && recovery) {
          const latest = await runRecovery(['batch', 'latest'])
          const latestBatch = latest.batch as SelectionRecoveryBatch | null
          const currentPool = latestBatch?.payload.candidatePool as BatchCandidatePool | undefined
          const settingsSnapshot = latestBatch?.payload.settingsSnapshot as Record<string, unknown> | undefined
          if (!latestBatch || latestBatch.batchId !== recovery.batchId || !currentPool || !settingsSnapshot || !currentPool.collectorAccountIds.length) {
            throw new Error('消费账号淘汰商品后无法读取候选池检查点；已保留账号商品结果并暂停批次。')
          }
          const allExcluded = new Set([...(currentPool.excludedProductIds ?? []), ...consumerRejectedIds])
          const poolSave = await runRecovery(['batch', 'pool-save', '--batch-id', recovery.batchId], {
            mode: currentPool.mode ?? 'total',
            quotaRules: currentPool.quotaRules ?? [],
            quotaTransfers: currentPool.quotaTransfers ?? [],
            target: currentPool.target,
            collectorAccountIds: currentPool.collectorAccountIds,
            collectorAccountId: currentPool.collectorAccountIds[0],
            capturedAt: new Date().toISOString(),
            filterFingerprint: currentPool.filterFingerprint,
            rows: [],
            excludedProductIds: [...allExcluded],
            sourceState: currentPool.sourceState
          })
          const poolResult = poolSave.pool as { count?: number } | undefined
          const expectedPoolCount = currentPool.rows.filter((row) => !allExcluded.has(row.productId)).length
          if (Number(poolResult?.count) !== expectedPoolCount) {
            throw new Error('消费淘汰商品后候选池落盘数量不一致；已暂停批次并保留检查点。')
          }
          for (const item of completed.filter((row) => row.cartAdded === true && String(row.productId ?? ''))) {
            await runRecovery([
              'quota-record-once', '--batch-id', recovery.batchId, '--account-id', String(targetAccountId),
              '--product-id', String(item.productId), '--event', 'cart', '--videos', '0'
            ])
          }
          const reason = `消费账号详情核验淘汰 ${consumerRejectedIds.length} 个商品，已从本批候选池全局排除；采集账号需按原规则补足后再继续。`
          await recovery.logEvent({
            stage: '候选池补采', code: 'WARNING_CONSUMER_DETAIL_REJECTED',
            message: reason,
            detail: `全局排除商品 ID：${consumerRejectedIds.join('、')}；剩余池内 ${poolResult?.count ?? 0}/${currentPool.target}。`,
            productId: consumerRejectedIds[0]
          })
          await recovery.persist({
            state: 'needs_retry', stage: 'candidate_refilling', error: reason,
            candidates: [], selectedIndexes: [], completed
          })
          await runRecovery(['batch', 'update', '--batch-id', recovery.batchId, '--state', 'needs_retry'])
          return { status: '部分完成', message: reason, continueBatch: false }
        }
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
                // 登录失效不是商品本身失败；保留候选检查点，登录后可继续，避免整批被误标为已完成。
                state: normalizedClassification === 'ACCOUNT_SWITCH_LOGIN' ? 'needs_retry' : 'failed',
                stage: failedStage, error: reason,
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
    const pausedBeforeVideo = await stopCurrentAccountForPause('video_pending')
    if (pausedBeforeVideo) return pausedBeforeVideo

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
      const pausedBetweenProducts = await stopCurrentAccountForPause('video_pending')
      if (pausedBetweenProducts) return pausedBetweenProducts
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
            accountKey: registryAccountKey(targetAccountId),
            downloadPath: context.downloadPath,
            selectedIndexes: [candidate.cardIndex],
            candidateSnapshot: fetched.map((row) => row.productId === productId
              ? { ...row, cartAdded: true }
              : row),
            recoverExistingCart: true,
            refreshCandidatesById: true,
            previewOnly: false,
            closeAfterAutomation: !recovery
          }))
          if (detail.ok !== true) throw new Error(String(detail.error ?? '百应商品详情采集失败'))
          if (detail.poolConsumerRateLimit) throw new Error(String(detail.poolConsumerRateLimit))
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
          const pausedAfterDetail = await stopCurrentAccountForPause('video_pending')
          if (pausedAfterDetail) return pausedAfterDetail
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
        const pausedAfterVideo = await stopCurrentAccountForPause('video_pending')
        if (pausedAfterVideo) return pausedAfterVideo
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

    const requestedCount = Number(settings.candidateLimit ?? 5)
    const batchTarget = Number.isFinite(requestedCount) ? Math.max(1, Math.min(10, requestedCount)) : 5
    const hasSkippedProduct = completed.some((item) => item.skipped === true)
    const targetProductCount = batchTarget
    const publishableItems = completed.filter((item) =>
      item.skipped !== true && item.cartAdded === true && typeof item.video9x16 === 'string' && String(item.productId ?? '')
    )
    const publishedConfirmedCount = publishableItems.filter((item) => item.publishQueueStatus === 'published').length
    const hasIncompletePublish = publishableItems.length < targetProductCount || publishableItems.some((item) => item.publishQueueStatus !== 'published')
    const hasIncompleteProduct = hasQuotaFailure || hasQueueFailure || completed.some((item) =>
      item.skipped !== true && (item.cartAdded !== true || typeof item.video9x16 !== 'string')
    ) || hasIncompletePublish
    const dedupeSkipped = completed.filter((item) => item.skipped === true && (
      item.registryExcluded === true || item.mutualExclusionSkipped === true || Array.isArray(item.publishQueueTaskIds)
    ))
    const dedupeSkippedIds = new Set(dedupeSkipped.map((item) => String(item.productId ?? '')))
    const productSummary = completed.filter((item) => !dedupeSkippedIds.has(String(item.productId ?? ''))).map((item) =>
      String(item.title ?? item.productId) + '：' + String(item.status ?? '已完成')
    ).join('；')
    const dedupeSummary = dedupeSkipped.length
      ? `商品 ID 防重跳过 ${dedupeSkipped.length} 个，逐项原因见选品日志`
      : ''
    const categorySummary = categoryWarnings.length
      ? `跳过可恢复的类目 / 来源读取问题 ${categoryWarnings.length} 项（原因已记录）`
      : ''
    const publishSummary = `抖音发布确认 ${publishedConfirmedCount}/${targetProductCount}`
    const summary = [categorySummary, dedupeSummary, publishSummary, productSummary, videoResults.length ? '处理结果：' + videoResults.join('；') : '']
      .filter(Boolean).join('；')
    if (consumer11001Reason && recovery) {
      // 搜索 11001 发生在本商品加车之前。先完成本轮已确认商品的配额、视频与发布，
      // 再保留这些结果并清除未触碰的候选快照；冷却后从现有池重新分配。
      const history = await runRecovery(['batch', 'logs', '--batch-id', recovery.batchId])
      const { code, label, cooldownSeconds } = consumer11001RetryPlan(
        consumer11001Reason, history.runLogs, targetAccountId
      )
      const reason = `${label}；客户端自动续选冷却约 ${cooldownSeconds} 秒。已保留确认结果，冷却后从现有候选池重新分配；${consumer11001Reason}`
      const retained = completed.filter((item) => item.cartAdded === true ||
        (item.cartAttempted === true && item.skipped !== true))
      await recovery.logEvent({
        stage: '消费账号商品搜索', code,
        message: `${label}；约 ${cooldownSeconds} 秒后自动续选。`,
        detail: `本轮确认加车 ${quotaProducts.length}/${batchTarget}；未触碰商品已尝试释放预占，结算未确认的仍保持锁定；${consumer11001Reason}`
      })
      await recovery.persist({
        state: 'needs_retry', stage: 'cart_pending', error: reason,
        candidates: [], selectedIndexes: [], completed: retained
      })
      accountStatuses[targetAccountId] = `needs_retry（加车 ${quotaProducts.length}/${batchTarget}，等待 ${cooldownSeconds} 秒）`
      return { status: '部分完成', message: reason, continueBatch: false }
    }
    if (recovery) {
      if (hasCartFailure) {
        await recovery.persist({ state: 'needs_retry', stage: 'cart_pending', error: summary, candidates: fetched, selectedIndexes, completed })
      } else if (hasIncompleteProduct) {
        await recovery.persist({ state: 'needs_retry', stage: 'video_pending', error: summary, completed })
      } else {
        await recovery.persist({ state: 'completed', stage: 'completed', error: '', completed })
      }
    }
    const partial = hasIncompleteProduct || hasSkippedProduct || categoryWarnings.length > 0 || quotaProducts.length < batchTarget
    accountStatuses[targetAccountId] = partial
      ? `部分完成（选品 ${publishableItems.length}/${targetProductCount}，发布 ${publishedConfirmedCount}/${targetProductCount}）`
      : `完成（选品 ${targetProductCount}/${targetProductCount}，发布 ${publishedConfirmedCount}/${targetProductCount}）`
    reportSelectionProgress(targetAccountId, partial ? '当前账号处理结束，商品数量未补齐或存在跳过、待恢复商品' : '当前账号选品完成', {
      itemIndex: completed.length,
      itemTotal: completed.length,
      percent: partial ? null : 100,
      message: `本轮目标 ${batchTarget} 个，已加车 ${quotaProducts.length} 个。${summary}`
    })
    return {
      status: partial ? '部分完成' : '完成',
      message: `本轮目标 ${batchTarget} 个，已加车 ${quotaProducts.length} 个。${summary}`,
      continueBatch: !hasIncompleteProduct && partial
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
  previewCandidatePool.value = null
  previewCandidatePoolCount.value = 0
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
      '」预览完成：已建立 ' + previewCandidatePoolCount.value + ' 个去重候选商品池；来源包含选品广场、热点趋势和联盟榜单。账号设置和路径未变化时，正式批次会复用此池并再次排除已登记商品。来源统计可在下方进度详情中查看。'
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

function safePathSegment(value: unknown, fallback: string): string {
  const segment = String(value ?? '').trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
    .replace(/[. ]+$/g, '')
    .slice(0, 80)
  return segment && segment !== '.' && segment !== '..' ? segment : fallback
}

function defaultAccountDownloadFolderName(accountId: number): string {
  const accountName = accounts.value.find((account) => account.id === accountId)?.name
  return safePathSegment(accountName, `账号-${accountId}`)
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
      const savedThresholds = (context.settings?.thresholds ?? {}) as Record<string, unknown>
      if (savedThresholds.commissionRate != null && context.settings?.commissionRateRule !== 'minimum') {
        throw new Error('旧检查点佣金率按上限保存，不能按新最低值规则自动恢复。')
      }
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

async function resolveSavedEffectiveDownloadPath(accountId: number): Promise<string> {
  const saved = await runEngine('selection_settings.py', [
    '--db', DB_PATH, 'get', '--account-id', String(accountId)
  ])
  const settingsRoot = (saved.settings ?? {}) as { account?: Record<string, unknown> }
  const configuredPath = String(settingsRoot.account?.downloadPath ?? '').trim()
  const defaultRoot = (await window.api.dir.getDownloadDir()).replace(/[\\/]+$/, '')
  const rawPath = configuredPath || `${defaultRoot}\\${defaultAccountDownloadFolderName(accountId)}`
  const result = await window.api.dir.canonicalizePath(rawPath)
  if (!result.ok || !result.path) {
    throw new Error(result.error ?? `账号 ${accountId} 的当前下载目录无法解析`)
  }
  return result.path
}

async function loadRecoveryStatus(): Promise<void> {
  const result = await runRecovery(['batch', 'latest'])
  const activeBatch = result.batch as SelectionRecoveryBatch | null
  const recentResult = activeBatch ? null : await runRecovery(['batch', 'recent'])
  const batch = activeBatch ?? (recentResult?.batch as SelectionRecoveryBatch | null)
  recoveryBatch.value = batch
  if (batch) showSavedPoolCheckpoint(batch)
  else candidatePoolProgress.value = null
  if (!selectionProgress.value) batchProgressPhase.value = candidatePoolProgress.value?.status === 'ready' ? 'account' : 'pool'
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
): Promise<number> {
  const previousLogs = await runRecovery(['batch', 'logs', '--batch-id', batchId])
  const priorRateLimitCount = Array.isArray(previousLogs.runLogs)
    ? previousLogs.runLogs.filter((entry) => {
      const log = entry && typeof entry === 'object' ? entry as Record<string, unknown> : {}
      return Number(log.accountId) === failedAccountId && String(log.code ?? '') === 'BAIYING_RATE_LIMIT'
    }).length
    : 0
  // 百应持续返回 11001 时避免每分钟重复请求。单账号阶梯等待，最高 15 分钟；
  // 次数保存在批次日志里，应用重启后也能延续退避，不影响人工手动恢复。
  const cooldownSeconds = Math.min(15 * 60, 60 * (2 ** Math.min(priorRateLimitCount, 4)))
  const checkpointReason = `客户端自动续选冷却约 ${cooldownSeconds} 秒；${reason}`
  for (const account of pendingAccounts) {
    const isFailedAccount = account.accountId === failedAccountId
    await runRecovery([
      'account', 'update', '--batch-id', batchId, '--account-id', String(account.accountId)
    ], {
      state: isFailedAccount ? 'needs_retry' : 'queued',
      stage: 'candidate_fetch',
      error: isFailedAccount ? checkpointReason : ''
    })
  }
  await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', String(failedAccountId)], {
    stage: '获取候选',
    code: 'BAIYING_RATE_LIMIT',
    message: toUserSelectionMessage(checkpointReason),
    detail: reason
  })
  await runRecovery(['batch', 'update', '--batch-id', batchId, '--state', 'needs_retry'])
  const latest = await runRecovery(['batch', 'latest'])
  recoveryBatch.value = latest.batch as SelectionRecoveryBatch | null
  if (recoveryBatch.value) showSavedPoolCheckpoint(recoveryBatch.value)
  restoreSelectionCooldown(recoveryBatch.value)
  return cooldownSeconds
}

function markSelectionWaitingForBaiyingRetry(cooldownSeconds: number): void {
  error.value = ''
  notice.value = `百应候选暂不可用。批次检查点已保存，约 ${cooldownSeconds} 秒后自动重试；连续失败会逐步延长等待时间。`
  if (!selectionProgress.value) return
  selectionProgress.value = {
    ...selectionProgress.value,
    state: 'finished',
    stage: '等待百应恢复后自动续选',
    message: notice.value,
    itemIndex: null,
    itemTotal: null,
    percent: null
  }
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
  if (busy.value || scheduledRetryWaiting.value || !batch || batch.state !== 'needs_retry') return
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
    stopManualPauseAutoResume()
    manualPauseRequested.value = false
    recoveryBatch.value = null
    candidatePoolProgress.value = null
    selectionProgress.value = null
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

function appendUncheckedLoginTargets(
  results: LoginPreflightAccount[],
  targets: LoginPreflightTarget[],
  startIndex: number,
  reason: string
): void {
  for (const target of targets.slice(startIndex)) {
    results.push({
      ...target,
      baiying: { state: 'unknown', message: reason },
      creatorCenter: { state: 'unknown', message: reason }
    })
  }
}

async function checkLoginTargets(
  targets: LoginPreflightTarget[],
  closeAfterAutomation = true
): Promise<LoginPreflightAccount[]> {
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
      appendUncheckedLoginTargets(
        results,
        targets,
        targetIndex + 1,
        `因账号「${target.accountName}」的浏览器配置无法确认，此账号尚未检查。`
      )
      reportSelectionProgress(target.accountId, '等待账号浏览器配置处理', {
        accountIndex: targetIndex + 1,
        accountTotal: targets.length,
        completedAccounts: targetIndex,
        message: `账号「${target.accountName}」的浏览器配置无法确认；已暂停后续账号检查。`
      })
      return results
    }
    const checkOnce = async (): Promise<LoginPreflightAccount> => {
      try {
        const response = await window.api.browser.checkAccountLogins({
          accountId: target.accountId,
          profilePath: target.profilePath,
          closeAfterAutomation
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
    if (!loginTargetsPassed([checked.value])) {
      appendUncheckedLoginTargets(
        results,
        targets,
        targetIndex + 1,
        `因账号「${target.accountName}」需要登录或验证，此账号尚未检查。`
      )
      reportSelectionProgress(target.accountId, '等待账号登录或验证', {
        accountIndex: targetIndex + 1,
        accountTotal: targets.length,
        completedAccounts: targetIndex,
        message: `账号「${target.accountName}」需要登录或验证；已暂停检查后续账号，窗口保留供处理。`
      })
      return results
    }
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
  if (isBaiyingCooldownClassification(messageClassification)) return null
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
  scopeFingerprint?: string,
  closeAfterAutomation = true,
  batchAlreadyCreated = false
): Promise<Map<number, AccountSwitchFailure>> {
  const fingerprint = loginPreflightFingerprint(batchId, targets)
  if (verifiedLoginPreflight?.fingerprint === fingerprint && verifiedLoginPreflight.expiresAt > Date.now()) {
    verifiedLoginPreflight = null
    return new Map()
  }
  verifiedLoginPreflight = null
  const results = await checkLoginTargets(targets, closeAfterAutomation)
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
        message: batchAlreadyCreated
          ? `账号「${unconfirmed}」的登录状态暂时无法确认；批次已保存，当前账号暂停，后续账号未启动。请处理后重新检查。`
          : `账号「${unconfirmed}」页面已打开但工作台未能确认；请在账号专属浏览器核实后重新检查或手动确认。尚未创建或修改批次。`,
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
        message: batchAlreadyCreated
          ? `账号「${blockedAccounts}」的百应或创作者中心需要登录/验证；批次已保存，当前账号暂停，后续账号未启动。请先处理后重新检查。`
          : `账号「${blockedAccounts}」的百应或创作者中心需要登录/验证；尚未创建或修改批次。请先处理后重新检查。`,
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
  if (automaticScheduledStartActive) return true
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
      notice.value = current.batchId
        ? '当前账号的百应和创作者中心均已确认登录。点击「继续选品」后会从该账号继续，其他账号仍按顺序等待。'
        : '所选账号的百应和创作者中心均已确认登录。请点击「继续选品」重新开始；客户端会再次展示账号范围确认。'
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
  const requestedPoolResumeBatchId = automaticPoolResumeBatchId
  automaticResumeBatchId = null
  automaticResumeAccountId = null
  automaticPoolResumeBatchId = null
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
  clearCandidatePoolResumeTimer()
  if (!requestedAutoResumeBatchId && !requestedPoolResumeBatchId && !ensureAccountPathsSaved()) return
  if ((requestedAutoResumeBatchId || requestedPoolResumeBatchId) && accountPathsHaveUnsavedChanges.value) {
    traceSelectionCooldown('automatic_recovery_unsaved_path_drift_ignored', requestedAutoResumeBatchId || requestedPoolResumeBatchId!, {
      source: 'selection_settings_database', accountId: requestedAutoResumeAccountId
    })
  }
  busy.value = true
  activeSelectionRunBatchId.value = null
  error.value = ''
  notice.value = ''
  runLogWarning.value = ''
  candidatePoolProgress.value = null
  batchProgressPhase.value = 'pool'
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
    if (batch && batchIsManuallyPaused(batch)) {
      recoveryBatch.value = batch
      notice.value = '本批次已由用户手动暂停；只有点击“继续选品”才会恢复。'
      selectionProgress.value = {
        ...selectionProgress.value!, state: 'finished', stage: '手动暂停中',
        message: notice.value, percent: null
      }
      return
    }
    if (batch) {
      activeSelectionRunBatchId.value = batch.batchId
      if (await pauseBatchAtSafeBoundary(batch.batchId, '手动暂停于批次准备阶段')) return
    }
    if (requestedAutoResumeBatchId) {
      const limitedAccount = batch?.accounts.find((item) =>
        item.accountId === requestedAutoResumeAccountId && item.state === 'needs_retry' &&
        isBaiyingCooldownClassification(toUserSelectionError(item.error).classification)
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
    if (requestedPoolResumeBatchId) {
      const pool = batch?.payload.candidatePool as BatchCandidatePool | undefined
      if (!batch || batch.batchId !== requestedPoolResumeBatchId || batch.state !== 'needs_retry' ||
          !pool || (pool.status !== 'collecting' && !hasStaleReadyPoolAccount(batch, pool))) {
        notice.value = '候选池自动续采检查点已变化，未启动其他批次。'
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
      const isAutomaticRecovery = requestedAutoResumeBatchId === batch.batchId ||
        requestedPoolResumeBatchId === batch.batchId
      if (requestedAutoResumeBatchId === batch.batchId &&
          !selectedPendingAccounts.some((item) => item.accountId === requestedAutoResumeAccountId)) {
        notice.value = '冷却结束，但限流账号未包含在恢复范围内；请确认待恢复账号后继续。'
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '自动恢复范围已变化',
          message: notice.value, percent: null
        }
        return
      }
      recoveryAccountIds = new Set(selectedPendingAccounts.map((item) => item.accountId))

      const refreshableStage = !requestedPoolResumeBatchId && selectedPendingAccounts.length > 0 && selectedPendingAccounts.every((item) =>
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
        : { ...savedAccountContexts }
      const recoveryDownloadPathUpdates: Record<string, string> = {}
      for (const item of selectedPendingAccounts) {
        const currentPath = await resolveSavedEffectiveDownloadPath(item.accountId)
        const savedPath = displayedContexts[String(item.accountId)]?.downloadPath ?? ''
        if (savedPath && normalizedResourcePath(savedPath) !== normalizedResourcePath(currentPath)) {
          recoveryDownloadPathUpdates[String(item.accountId)] = currentPath
          const savedContext = displayedContexts[String(item.accountId)]
          if (savedContext) displayedContexts[String(item.accountId)] = { ...savedContext, downloadPath: currentPath }
        }
      }
      const recoveryPathSummary = selectedPendingAccounts.map((item) => {
        const effectivePath = displayedContexts[String(item.accountId)]?.downloadPath
        return `${currentById.get(item.accountId)?.name ?? item.accountName}：${effectivePath || '沿用检查点中已保存的商品目录'}`
      }).join('\n')
      const recoverySummary = [
        `本次恢复：${selectedSummary}`,
        completedSummary ? `已完成并跳过：${completedSummary}` : '',
        preservedSummary ? `未勾选，保留原检查点：${preservedSummary}` : '',
        `素材保存路径${Object.keys(recoveryDownloadPathUpdates).length
          ? '（确认后更新本批次未完成账号）'
          : refreshableUpdate?.settingsChanged ? '（候选尚未查询，将按当前已保存设置刷新）' : '（当前设置与批次一致）'}：\n${recoveryPathSummary}`,
        '未完成商品的详情图补采和新生成视频将使用上方目录；已有详情图、已生成视频及其他素材不会搬迁。'
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
      if (Object.keys(recoveryDownloadPathUpdates).length) {
        const updated = await runRecovery([
          'batch', 'update-download-paths', '--batch-id', batch.batchId
        ], { downloadPaths: recoveryDownloadPathUpdates })
        batch = updated.batch as SelectionRecoveryBatch
        recoveryBatch.value = batch
        notice.value = '已将所选待恢复账号的详情图补采和新视频输出目录更新为当前设置；已有素材文件保留原位。'
      }
      if (refreshableUpdate?.settingsChanged) {
        const payload = {
          ...batch.payload,
          settingsSnapshot: refreshableUpdate.currentSnapshot,
          accountContexts: refreshableUpdate.freshContexts,
          reviewedAccountId: null,
          reviewedProductIds: null,
          candidatePlan: {}
        }
        const refreshed = await runRecovery([
          'batch', 'refresh-candidates', '--batch-id', batch.batchId
        ], payload)
        batch = refreshed.batch as SelectionRecoveryBatch
        previewCandidatePool.value = null
        previewCandidatePoolCount.value = 0
        notice.value = '上次失败发生在候选查询，尚未加车；已按当前设置刷新检查点。账号浏览器会在轮到该账号时再打开。'
      }
      snapshot = batch.payload.settingsSnapshot as Record<string, unknown>
      if (!snapshot || typeof snapshot !== 'object') throw new Error('恢复批次缺少设置快照，任务已保留。')
      const savedThresholds = (snapshot.thresholds ?? {}) as Record<string, unknown>
      if (savedThresholds.commissionRate != null && snapshot.commissionRateRule !== 'minimum') {
        throw new Error('旧批次佣金率按上限保存，不能按新最低值规则自动恢复；检查点已保留。')
      }
      const currentPersistedResult = await runEngine('selection_settings.py', ['--db', DB_PATH, 'get'])
      const currentPersistedSettings = (currentPersistedResult.settings ?? {}) as Record<string, unknown>
      validateCommissionRange(
        commissionBounds(currentPersistedSettings)[0],
        commissionBounds(currentPersistedSettings)[1],
        '当前已保存设置'
      )
      if (!commissionBoundsEqual(snapshot, currentPersistedSettings)) {
        const [oldMin, oldMax] = commissionBounds(snapshot)
        const [currentMin, currentMax] = commissionBounds(currentPersistedSettings)
        throw new Error(
          `旧批次佣金快照为 ${oldMin ?? '不限'}%–${oldMax ?? '不限'}%，` +
          `当前已保存设置为 ${currentMin ?? '不限'}%–${currentMax ?? '不限'}%；` +
          '为避免新旧佣金规则混用，未恢复旧批次，请新建批次。'
        )
      }
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
      const persistedBeforeCreate = await runEngine('selection_settings.py', ['--db', DB_PATH, 'get'])
      const persistedBeforeCreateSettings = (persistedBeforeCreate.settings ?? {}) as Record<string, unknown>
      validateCommissionRange(
        commissionBounds(persistedBeforeCreateSettings)[0],
        commissionBounds(persistedBeforeCreateSettings)[1],
        '当前已保存设置'
      )
      const uiSettings = JSON.parse(JSON.stringify(global)) as Record<string, unknown>
      if (!commissionBoundsEqual(uiSettings, persistedBeforeCreateSettings)) {
        const [uiMin, uiMax] = commissionBounds(uiSettings)
        const [savedMin, savedMax] = commissionBounds(persistedBeforeCreateSettings)
        throw new Error(
          `当前页面佣金设置为 ${uiMin ?? '不限'}%–${uiMax ?? '不限'}%，` +
          `但已保存设置为 ${savedMin ?? '不限'}%–${savedMax ?? '不限'}%；` +
          '请先保存通用选品设置，未创建新批次。'
        )
      }
      if (!selectionUiDataReady.value) {
        if (props.active || props.dialogOnly) await ensureSelectionUiData()
        else await reloadCatalog()
      }
      snapshot = JSON.parse(JSON.stringify(global)) as Record<string, unknown>
      snapshot.categoryTree = JSON.parse(JSON.stringify(
        selectedLocalPoolBatchId.value && selectedLocalPoolCategoryTree.value
          ? selectedLocalPoolCategoryTree.value : categories.value
      ))
      if (commissionRuleMigrationRequired.value) throw new Error('旧佣金设置按上限保存；请核对最低佣金率并保存通用设置后再新建批次。')
      const collectorIds = Array.isArray(snapshot.candidateCollectorAccountIds)
        ? snapshot.candidateCollectorAccountIds.map(Number) : []
      const selectedPoolTarget = validateQuotaRules(
        snapshot.candidatePoolMode === 'quota' ? 'quota' : 'total',
        snapshot.candidatePoolQuotas as CandidatePoolQuotaRule[],
        snapshot.sources as SelectionFilterSettings['sources'],
        snapshot.excludedCategorySelection as typeof DEFAULTS.excludedCategorySelection
      )
      snapshot.candidatePoolTarget = selectedPoolTarget
      if (!Number.isInteger(Number(snapshot.candidatePoolTarget)) ||
          Number(snapshot.candidatePoolTarget) < 1 || Number(snapshot.candidatePoolTarget) > 2000 ||
          ![1, 2].includes(collectorIds.length) || new Set(collectorIds).size !== collectorIds.length ||
          collectorIds.some((id) => !enabled.some((account) => account.id === id))) {
        throw new Error('请先设置候选池目标数量，并从已启用账号中指定 1–2 个采集账号。')
      }
      const consumerDemand = await activeConsumerDemand(snapshot)
      if (Number(snapshot.candidatePoolTarget) < consumerDemand.total) {
        throw new Error(`候选池目标 ${snapshot.candidatePoolTarget} 件小于本批消费账号目标合计 ${consumerDemand.total} 件（${consumerDemand.detail}）；已停止创建批次。`)
      }
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
      notice.value = '选品登记记录可用；重点类目按百应当前可见商品数据自动推荐。'
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
        `${selectedLocalPoolBatchId.value ? `先读取已选的本机候选池 ${selectedLocalPoolBatchId.value}；` : '先尝试复用本机 24 小时内同条件、同采集账号的候选商品；'}缺额再由采集账号 ${collectorIds.join('、')} 顺序只读采集，${snapshot.candidatePoolMode === 'quota' ? '每条品类配额分别达标' : `候选池达到 ${snapshot.candidatePoolTarget} 件`}后才开始消费账号。每次只启动当前账号的浏览器；消费账号按池内商品 ID 用自身百应登录态核验详情和加车，完成选品发布并关闭浏览器后才会启动下一个账号。`
      )) {
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '已取消启动',
          message: '未创建批次或发起平台请求。', percent: null
        }
        return
      }
      // 本次批次使用的通用设置先落盘，重启和自动恢复才能沿用相同采集账号与池目标。
      const savedSelectionSettings = await runEngine(
        'selection_settings.py', ['--db', DB_PATH, 'save'], snapshot
      )
      if (savedSelectionSettings.ok !== true) {
        throw new Error('通用选品设置保存失败，未创建批次。')
      }
      const savedSnapshotSettings = (savedSelectionSettings.settings ?? {}) as Record<string, unknown>
      if (!commissionBoundsEqual(snapshot, savedSnapshotSettings) || savedSnapshotSettings.commissionRateRule !== 'minimum') {
        throw new Error('佣金范围快照保存后校验不一致，未创建批次。')
      }
      reviewedAccountId = null
      reviewedProducts = reviewedAccountId !== null
        ? new Set(candidates.value.filter((candidate) => candidate.selected).map((candidate) => candidate.productId))
        : undefined
      if (manualPauseRequested.value) {
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '手动暂停于新批次创建前',
          message: '已停止本次选品，尚未创建批次或启动消费账号。', percent: null
        }
        manualPauseRequested.value = false
        return
      }
      const batchId = window.crypto.randomUUID()
      const payload: Record<string, unknown> = {
        settingsSnapshot: snapshot,
        candidatePoolReuseBatchId: selectedLocalPoolBatchId.value,
        accountContexts: Object.fromEntries([...contexts].map(([id, context]) => [String(id), context])),
        reviewedAccountId,
        reviewedProductIds: reviewedProducts ? [...reviewedProducts] : null,
        candidatePlan: {},
        accounts: enabled.map((account) => ({ id: account.id, name: account.name }))
      }
      selectionProgress.value = {
        ...selectionProgress.value!,
        stage: '保存选品批次',
         message: '账号和资源配置已核对；先复用本机近期候选，缺额补采，达到目标后再处理消费账号。'
      }
      const created = await runRecovery(['batch', 'create', '--batch-id', batchId], payload)
      batch = created.batch as SelectionRecoveryBatch
      recoveryBatch.value = batch
      activeSelectionRunBatchId.value = batch.batchId
      selectedLocalPoolBatchId.value = null
      selectedLocalPoolCategoryTree.value = null
    }

    if (await pauseBatchAtSafeBoundary(batch.batchId, '手动暂停于候选池准备阶段')) return
    showSavedPoolCheckpoint(batch)
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
      message: `本轮将${resumePriorityAccountId !== null ? '优先恢复限流账号、再按账号 ID 顺序处理' : '按账号 ID 顺序处理'} ${activeTaskAccounts.length} 个启用账号；只启动当前账号浏览器，完成选品发布并关闭后再切换。`,
      itemIndex: null,
      itemTotal: null,
      percent: null
    }
    for (const item of persistedAccounts) {
      if (item.state === 'completed') accountStatuses[item.accountId] = '完成'
      else if (activeById.get(item.accountId)?.status !== 'active') accountStatuses[item.accountId] = '待恢复（账号未启用）'
    }
    const batchId = batch.batchId
    let batchPayload = batch.payload
    const poolMode = Array.isArray((batchPayload.settingsSnapshot as Record<string, unknown> | undefined)?.candidateCollectorAccountIds) &&
      ((batchPayload.settingsSnapshot as Record<string, unknown>).candidateCollectorAccountIds as unknown[]).length > 0
    if (poolMode) {
      selectionProgress.value = {
        ...selectionProgress.value!, stage: '建立共享候选池',
        message: '先复用本机符合条件的候选商品，缺额由采集账号顺序补采；池达标后才开始消费账号。'
      }
    } else {
      batchProgressPhase.value = 'account'
    }
    let collectorPool: BatchCandidatePool | undefined
    if (poolMode) {
      const collected = await collectBatchCandidatePool(batch, contexts)
      batch = collected.batch
      recoveryBatch.value = batch
      showSavedPoolCheckpoint(batch)
      batchPayload = batch.payload
      collectorPool = collected.pool
      if (await pauseBatchAtSafeBoundary(batchId, '手动暂停于候选池采集后')) return
      if (!collected.ready) {
        const autoResumePool = collected.autoResumeAllowed !== false && collectorPool?.status === 'collecting'
        if (autoResumePool && collectorPool) scheduleCandidatePoolResume(batch, collectorPool)
        const quotaDeficits = collectorPool?.mode === 'quota'
          ? collectorPool.quotaProgress?.filter((item) => item.deficit > 0)
            .map((item) => `${quotaProgressLabel(item.ruleId)} 缺 ${item.deficit} 件`).join('；') : ''
        notice.value = (quotaDeficits
          ? `候选池品类配额尚未达标：${quotaDeficits}。已保存检查点，消费账号未启动。`
          : `候选池 ${collectorPool?.rows.length ?? 0}/${collectorPool?.target ?? '?'}，尚未达到设定目标；已保存检查点，消费账号未启动。`) +
          (autoResumePool ? '客户端将按退避时间自动续采，重启后也会恢复等待。' : '请检查来源失败原因后手动继续。')
        selectionProgress.value = {
          ...selectionProgress.value!, state: 'finished', stage: '候选池待补充',
          message: notice.value, percent: null
        }
        return
      }
    }
    if (collectorPool) {
      let occupied = await occupiedPoolProductIds(
        collectorPool.rows, collectorPool.collectorAccountIds[0], batchId
      )
      while (occupied.size) {
        const allExcluded = new Set(collectorPool.excludedProductIds ?? [])
        const newlyOccupied = [...occupied].filter((productId) => !allExcluded.has(productId))
        if (!newlyOccupied.length) break
        for (const productId of newlyOccupied) allExcluded.add(productId)
        const saved = await runRecovery(['batch', 'pool-save', '--batch-id', batchId], {
          mode: collectorPool.mode ?? 'total', quotaRules: collectorPool.quotaRules ?? [],
          quotaTransfers: collectorPool.quotaTransfers ?? [], target: collectorPool.target,
          collectorAccountIds: collectorPool.collectorAccountIds,
          collectorAccountId: collectorPool.collectorAccountIds[0],
          capturedAt: new Date().toISOString(), filterFingerprint: collectorPool.filterFingerprint,
          rows: [], excludedProductIds: [...allExcluded], sourceState: collectorPool.sourceState
        })
        const savedPool = saved.pool as { count?: number } | undefined
        const expectedCount = collectorPool.rows.filter((row) => !allExcluded.has(row.productId)).length
        if (Number(savedPool?.count) !== expectedCount) {
          throw new Error('服务器同步冲突剔除后的候选池数量不一致；消费账号未启动。')
        }
        const latest = await runRecovery(['batch', 'latest'])
        const latestBatch = latest.batch as SelectionRecoveryBatch | null
        if (!latestBatch || latestBatch.batchId !== batchId) throw new Error('服务器冲突处理后批次检查点发生变化。')
        showSavedPoolCheckpoint(latestBatch)
        const refilled = await collectBatchCandidatePool(latestBatch, contexts)
        batch = refilled.batch
        recoveryBatch.value = batch
        showSavedPoolCheckpoint(batch)
        batchPayload = batch.payload
        collectorPool = refilled.pool
        if (await pauseBatchAtSafeBoundary(batchId, '手动暂停于候选池补采后')) return
        if (!refilled.ready) {
          const unmet = collectorPool?.mode === 'quota'
            ? collectorPool.quotaProgress?.filter((item) => item.deficit > 0)
              .map((item) => `${quotaProgressLabel(item.ruleId)} 缺 ${item.deficit} 件`).join('；') : ''
          notice.value = unmet
            ? `服务器同步冲突后候选池配额未补足：${unmet}。已保存检查点，消费账号未启动。`
            : `服务器同步冲突后候选池 ${collectorPool?.rows.length ?? 0}/${collectorPool?.target ?? '?'}；已保存检查点，消费账号未启动。`
          selectionProgress.value = {
            ...selectionProgress.value!, state: 'finished', stage: '候选池待补充',
            message: notice.value, percent: null
          }
          return
        }
        if (await pauseBatchAtSafeBoundary(batchId, '手动暂停于候选池补采后')) return
        occupied = await occupiedPoolProductIds(
          collectorPool.rows, collectorPool.collectorAccountIds[0], batchId
        )
      }
    }
    if (collectorPool?.status === 'ready') {
      if (selectionProgressFlushTimer !== null) window.clearTimeout(selectionProgressFlushTimer)
      selectionProgressFlushTimer = null
      pendingSelectionProgressUpdate = null
      batchProgressPhase.value = 'account'
      selectionProgress.value = {
        ...selectionProgress.value!, accountId: null, accountName: '',
        stage: '从共享候选池准备账号商品分配',
        message: '候选池已达标；消费账号将按精确商品 ID 分别核验，池内商品不等于已加车商品。',
        itemIndex: null, itemTotal: null, percent: null
      }
    }
    const batchCandidatePlans = {
      ...preparedCandidatePlans,
      ...((batchPayload.candidatePlan ?? {}) as Record<string, AccountCandidatePlan>)
    }
    if (collectorPool?.status === 'ready') {
      const availablePoolIds = new Set(collectorPool.rows.map((row) => row.productId))
      for (const account of activeTaskAccounts) {
        const task = persistedAccounts.find((item) => item.accountId === account.id)
        if (!task || task.state === 'completed') continue
        const savedCandidates = Array.isArray(task.candidates) ? task.candidates : []
        const savedCompleted = Array.isArray(task.completed) ? task.completed : []
        // 已加车或加车结果不明的商品必须保留检查点，不能借换池重复执行。
        const onlyConfirmedSkips = savedCompleted.every((item) =>
          item.skipped === true && item.cartAttempted !== true && item.cartAdded !== true)
        const staleSnapshot = savedCandidates.length > 0 && onlyConfirmedSkips &&
          savedCandidates.every((item) => !availablePoolIds.has(item.productId))
        if (staleSnapshot) {
          const saved = await runRecovery([
            'account', 'update', '--batch-id', batchId, '--account-id', String(account.id)
          ], {
            state: 'needs_retry', stage: 'candidate_fetch', error: '',
            candidates: [], selectedIndexes: [], completed: []
          })
          Object.assign(task, saved.account as SelectionRecoveryAccount)
          try {
            await runRecovery(['log', 'append', '--batch-id', batchId, '--account-id', String(account.id)], {
              stage: '候选池恢复', code: 'INFO_STALE_ACCOUNT_PLAN_REPLACED',
              message: `旧账号候选 ${savedCandidates.length} 件均已不在本批可用池；已从现成池重新分配。`,
              detail: '旧商品的登记状态和历史日志保留；未重新采集三来源，也未释放结果不明的登记。'
            })
          } catch (cause) {
            runLogWarning.value = '账号候选重建日志写入失败：' + conciseFailureMessage(cause)
          }
        }
        const savedPlan = batchCandidatePlans[String(account.id)]
        if (savedPlan?.candidates?.length && savedPlan.candidates.every((item) =>
          !availablePoolIds.has(item.productId))) {
          delete batchCandidatePlans[String(account.id)]
        }
      }
      const unplanned = activeTaskAccounts.filter((account) => {
        const task = persistedAccounts.find((item) => item.accountId === account.id)
        return contexts.has(account.id) && !contextErrors.has(account.id) &&
          task?.state !== 'completed' && !task?.candidates?.length &&
          !task?.selectedIndexes?.length && !batchCandidatePlans[String(account.id)]
      })
      if (unplanned.length) {
        const planned = await prepareBatchCandidatePlans(
          unplanned, contexts, undefined, undefined, accountSwitchFailures,
          undefined, batchId, false, collectorPool
        )
        for (const [accountId, plan] of planned) batchCandidatePlans[String(accountId)] = plan
      }
    }
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

    if (await pauseBatchAtSafeBoundary(batchId, '手动暂停于消费账号启动前')) return
    const outcomes = await runSequentialSelectionBatch(
      taskAccounts,
      async (account) => {
        const task = persistedAccounts.find((item) => item.accountId === account.id)
        if (!task) throw new Error('恢复批次中找不到该账号任务。')
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
        const appendAccountLogs = async (entries: SelectionRunLogInput[]): Promise<void> => {
          if (!entries.length) return
          try {
            await runRecovery([
              'log', 'append-batch', '--batch-id', batchId, '--account-id', String(account.id)
            ], entries)
          } catch (cause) {
            runLogWarning.value = '选品过程日志批量写入失败：' + conciseFailureMessage(cause)
          }
        }
        const logFailure = appendAccountLog
        if (manualPausePending()) {
          return { status: '部分完成' as const, message: '手动暂停，当前账号尚未启动。', continueBatch: false }
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
        if (task.state === 'completed') {
          try {
            await window.api.browser.closeAccount({ accountId: account.id, profilePath: context.profilePath })
          } catch (cause) {
            const reason = String(cause instanceof Error ? cause.message : cause)
            await appendAccountLog({
              stage: '关闭账号浏览器', code: 'ACCOUNT_BROWSER_CLOSE_FAILED',
              message: '已完成账号的专属浏览器未能关闭；为避免同时打开多个账号窗口，批次已暂停。',
              detail: reason
            })
            return {
              status: '部分完成' as const,
              message: '已完成账号的浏览器未能关闭；后续账号尚未启动。',
              continueBatch: false
            }
          }
          return { status: '完成' as const, message: '已完成，跳过重复执行' }
        }

        let keepBrowserOpenForLogin = false
        let accountOutcome: { status: '完成' | '部分完成'; message?: string; continueBatch?: boolean } | null = null
        let accountFailure: unknown = null
        let accountSkipped = false
        let browserCloseFailure = ''
        let pauseAfterAccount = false
        try {
          // 登录预检是恢复前的临时动作，不得覆盖 cart_pending 等商品检查点。
          // 否则 getSelectionRecoveryMode 会误判为重新读取三来源候选。
          await persist({
            state: 'in_progress',
            ...(task.stage ? {} : { stage: 'account_preflight' }),
            error: ''
          })
          const target: LoginPreflightTarget = {
            accountId: account.id,
            accountName: account.name,
            profilePath: context.profilePath,
            resolvedProfilePath: context.resolvedProfilePath,
            contextError: undefined
          }
          const accountScopeFingerprint = stableJson({
            kind: 'batch-account-login',
            batchId,
            accountId: account.id,
            profilePath: context.resolvedProfilePath ?? context.profilePath
          })
          const loginFailures = await collectAccountSwitchFailures(
            [target], batchId, accountScopeFingerprint, false, true
          )
          const waitingForLogin = loginPreflight.value?.batchId === batchId &&
            loginPreflight.value.accounts.some((item) => item.accountId === account.id)
          if (waitingForLogin) {
            const reason = '当前账号需要完成登录或安全验证；批次已保存，后续账号尚未启动。'
            await appendAccountLog({
              stage: '账号登录预检', code: 'ACCOUNT_LOGIN_CONFIRMATION_REQUIRED',
              message: reason, detail: reason
            })
            await persist({ state: 'needs_retry', stage: 'queued', error: reason })
            keepBrowserOpenForLogin = false
            accountOutcome = { status: '部分完成', message: reason, continueBatch: true }
          } else if (manualPausePending()) {
            const reason = '手动暂停；登录预检已完成，当前账号尚未开始加车或发布。'
            await persist({ state: 'needs_retry', stage: task.stage || 'queued', error: reason })
            accountOutcome = { status: '部分完成', message: reason, continueBatch: false }
          } else {
            const switchFailure = loginFailures.get(account.id) ?? accountSwitchFailures.get(account.id)
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
            await appendAccountLog({
              stage: '账号选品', code: 'INFO_ACCOUNT_STARTED',
              message: `开始处理账号「${account.name}」；此前未启动其他账号浏览器。`
            })
            await persist({ state: 'in_progress', error: '' })
            let candidatePlan: AccountCandidatePlan | undefined = batchCandidatePlans[String(account.id)]
            if (!candidatePlan && !task.candidates?.length && !task.selectedIndexes?.length) {
              if (poolMode) {
                const reason = '共享候选池已达标，但当前账号没有可分配的商品 ID；保留检查点等待补池。'
                await persist({ state: 'needs_retry', stage: 'candidate_refilling', error: reason })
                accountOutcome = { status: '部分完成', message: reason, continueBatch: false }
              } else {
                await persist({ state: 'in_progress', stage: 'candidate_fetch', error: '' })
                const reusablePlans = reusablePreviewCandidatePlans([account], contexts)
                const plans = await prepareBatchCandidatePlans(
                  [account], contexts,
                  account.id === persistedReviewedAccountId ? persistedReviewedAccountId : undefined,
                  account.id === persistedReviewedAccountId ? persistedReviewedProducts : undefined,
                  undefined, reusablePlans, batchId, true
                )
                candidatePlan = plans.get(account.id)
                if (previewCandidatePool.value?.createdByAccountId === account.id && reusablePlans.size) {
                  previewCandidatePool.value = null
                  previewCandidatePoolCount.value = 0
                }
              }
            }
            if (!accountOutcome) {
              const executed = await executeAccountSelection(
              account.id,
              false,
              batchPayload.settingsSnapshot as Record<string, unknown>,
              account.id === persistedReviewedAccountId ? persistedReviewedProducts : undefined,
              context,
              { batchId, state: task, persist, logFailure, logEvent: appendAccountLog, logEvents: appendAccountLogs },
              persistedReviewedAccountId,
              candidatePlan,
              poolMode
            )
            // 某个商品需要后续重试时，当前账号仍已处理完并关闭窗口；继续处理下一个账号。
            // 保留账号任务对“当前账号必须停留/跳过”的决定；不能把候选不足、结果不明或账号级隔离强行改成继续。
              accountOutcome = { ...executed, continueBatch: executed.continueBatch }
            }
            await appendAccountLog({
              stage: '账号选品',
              code: accountOutcome.status === '完成' ? 'SUCCESS_ACCOUNT_COMPLETED' : 'WARNING_ACCOUNT_PARTIAL',
              message: `账号处理${accountOutcome.status}。${accountOutcome.message ? conciseFailureMessage(accountOutcome.message) : ''}`,
              detail: accountOutcome.message ?? ''
            })
          }
        } catch (cause) {
          const reason = String(cause instanceof Error ? cause.message : cause)
          accountFailure = cause
          const classification = toUserSelectionError(cause).classification
          pauseAfterAccount = isBaiyingCooldownClassification(classification)
          accountSkipped = [
            'ACCOUNT_SWITCH_CAPTCHA', 'ACCOUNT_SWITCH_LOGIN', 'ACCOUNT_SWITCH_DISCONNECT',
            'LOGIN_OR_PAGE_STATE_UNCONFIRMED'
          ].includes(String(classification ?? '')) || /权限不足|无权限|未授权|广告主.*不可见/.test(reason)
          if (task.state !== 'failed' && task.state !== 'needs_retry') {
            if (isBaiyingCooldownClassification(classification)) {
              await logFailure({
                stage: '获取候选', code: classification ?? 'BAIYING_RATE_LIMIT',
                message: toUserSelectionMessage(reason), detail: reason
              })
              await persist({ state: 'needs_retry', stage: task.stage || 'candidate_fetch', error: reason })
            } else if (accountSkipped) {
              await logFailure({
                stage: '账号隔离跳过', code: String(classification || 'ACCOUNT_SKIPPED'),
                message: '当前账号已隔离跳过，继续后续账号：' + conciseFailureMessage(reason), detail: reason
              })
              await persist({ state: 'failed', stage: 'account_skipped', error: reason })
              accountFailure = null
              accountOutcome = { status: '部分完成', message: `账号已隔离跳过：${reason}`, continueBatch: true }
            } else {
              await logFailure({
                stage: task.stage || '账号任务', code: 'ACCOUNT_TASK_FAILED',
                message: '账号任务失败：' + conciseFailureMessage(reason), detail: reason
              })
              await persist({ state: 'failed', stage: task.stage || 'account_run', error: reason })
            }
          }
        }
        if (!keepBrowserOpenForLogin) {
          try {
            const closed = await window.api.browser.closeAccount({ accountId: account.id, profilePath: context.profilePath })
            if (closed.ok !== true) throw new Error('客户端没有确认账号专属浏览器关闭')
          } catch (cause) {
            browserCloseFailure = String(cause instanceof Error ? cause.message : cause)
            await appendAccountLog({
              stage: '关闭账号浏览器', code: 'ACCOUNT_BROWSER_CLOSE_FAILED',
              message: '当前账号的专属浏览器未能关闭；为避免同时打开多个账号窗口，批次已暂停。',
              detail: browserCloseFailure
            })
          }
        }
        if (browserCloseFailure) {
          const primaryFailure = accountFailure
            ? String(accountFailure instanceof Error ? accountFailure.message : accountFailure)
            : ''
          if (task.state !== 'completed') {
            await persist({
              state: 'needs_retry',
              stage: task.stage || 'queued',
              error: [primaryFailure, '账号浏览器关闭失败：' + browserCloseFailure].filter(Boolean).join('；')
            })
          }
          return {
            status: accountFailure ? '失败' as const : '部分完成' as const,
            message: [primaryFailure, '账号浏览器关闭失败：' + browserCloseFailure, '后续账号尚未启动。']
              .filter(Boolean).join('；'),
            continueBatch: false
          }
        }
        if (accountFailure) {
          if (pauseAfterAccount) {
            return {
              status: '失败',
              message: String(accountFailure instanceof Error ? accountFailure.message : accountFailure),
              continueBatch: false
            }
          }
          throw accountFailure
        }
        if (manualPausePending()) {
          return { ...(accountOutcome ?? { status: '部分完成' as const, message: '当前账号已保存检查点。' }),
            continueBatch: false }
        }
        return accountOutcome ?? { status: '部分完成' as const, message: '当前账号没有返回处理结果。' }
      },
      (account, status) => {
        if (!(status === '部分完成' && accountStatuses[account.id]?.startsWith('needs_retry'))) {
          accountStatuses[account.id] = status
        }
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
      resumePriorityAccountId,
      true
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
    const confirmedCartProductKeys = new Set<string>()
    for (const account of persistedAccounts) {
      for (const item of account.completed ?? []) {
        const productId = String(item.productId ?? '').trim()
        if (item.cartAdded === true && productId) confirmedCartProductKeys.add(`${account.accountId}:${productId}`)
      }
    }
    const confirmedCartProductCount = confirmedCartProductKeys.size
    await appendBatchLog({
      stage: '批次结果',
      code: allComplete ? 'SUCCESS_BATCH_COMPLETED' : terminalFailed ? 'ERROR_BATCH_FAILED' : 'WARNING_BATCH_NEEDS_RETRY',
      message: `本轮结束：确认加车 ${confirmedCartProductCount} 个商品；完成 ${succeeded} 个账号，部分完成 ${partial} 个，失败 ${failed.length} 个，未执行 ${skipped} 个；${allComplete ? '批次已完成。' : terminalFailed ? '本批次已失败结束，原因已记录。' : `还有 ${waiting} 个账号待恢复。`}`
    })
    notice.value = (allComplete ? '批次完成。' : terminalFailed ? '本批次失败结束，原因已记录，可修正后启动新批次。' : '批次已保存，尚有 ' + waiting + ' 个账号任务待恢复。') +
      '本批次账号 ' + outcomes.length + ' 个：完成 ' + succeeded +
      ' 个，部分完成 ' + partial + ' 个，失败 ' + failed.length + ' 个，未执行 ' + skipped + ' 个。' +
      '确认加入选品车 ' + confirmedCartProductCount + ' 个商品。' +
      outcomes.map((item) => item.accountName + '：' + item.status).join('；')
    const rateLimitedAccount = persistedAccounts.find((item) =>
      item.state === 'needs_retry' &&
      isBaiyingCooldownClassification(toUserSelectionError(item.error).classification)
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
      stage: allComplete ? '选品流程已结束' : terminalFailed ? '选品批次失败结束' : '本轮处理结束，部分账号待恢复',
      message: `已处理 ${outcomes.filter((item) => item.status !== '未执行').length}/${activeTaskAccounts.length} 个账号；确认加入选品车 ${confirmedCartProductCount} 个商品；完成 ${succeeded}，部分完成 ${partial}，失败 ${failed.length}，未执行 ${skipped}。${terminalFailed ? '本批次已结束，无待恢复任务。' : ''}`,
      itemIndex: null,
      itemTotal: null,
      percent: allComplete ? 100 : null,
      resultSummary: `本批次确认加入选品车 ${confirmedCartProductCount} 个商品；上方流程百分比表示步骤完成度。`
    }
    const latestAfterRun = await runRecovery(['batch', 'latest'])
    if (latestAfterRun.batch) {
      recoveryBatch.value = latestAfterRun.batch as SelectionRecoveryBatch
    } else {
      const recentAfterRun = await runRecovery(['batch', 'recent'])
      recoveryBatch.value = recentAfterRun.batch as SelectionRecoveryBatch | null
    }
    if (recoveryBatch.value) showSavedPoolCheckpoint(recoveryBatch.value)
    else candidatePoolProgress.value = null
    if (!allComplete && !terminalFailed && await pauseBatchAtSafeBoundary(batchId, '手动暂停于当前账号收尾后')) return
    restoreSelectionCooldown(recoveryBatch.value)
    const readyPoolAfterRun = recoveryBatch.value?.payload.candidatePool as BatchCandidatePool | undefined
    if (recoveryBatch.value && readyPoolAfterRun && !loginPreflight.value &&
        hasStaleReadyPoolAccount(recoveryBatch.value, readyPoolAfterRun)) {
      scheduleCandidatePoolResume(recoveryBatch.value, readyPoolAfterRun)
    }
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
    if (!manualPausePending() && requestedAutoResumeBatchId && pendingAutoResume?.batchId === requestedAutoResumeBatchId &&
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
    if (manualPauseRequested.value && (!activeSelectionRunBatchId.value || recoveryBatch.value?.state === 'completed')) {
      manualPauseRequested.value = false
    }
    activeSelectionRunBatchId.value = null
    busy.value = false
  }
}

const runSelectionBatchOnce = createSingleFlight(runSelectionBatch)

function discardUnsavedAccountPaths(): void {
  accountDraft.browserProfilePath = savedAccountPaths.browserProfilePath
  accountDraft.downloadPath = savedAccountPaths.downloadPath
  error.value = ''
}

async function startSelectionFromButton(): Promise<void> {
  clearCandidatePoolResumeTimer()
  if (accountPathsHaveUnsavedChanges.value) {
    if (accountId.value === null) {
      error.value = '请先选择账号，再开始选品。'
      return
    }
    if (!await saveAccount()) return
  }
  if (manualPausePending() && !await clearManualSelectionPause()) return
  await startSelection()
}

async function startSelection(): Promise<boolean> {
  if (manualPausePending()) return false
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
    return false
  }
  if (selectionCooldownRemaining.value > 0 && !automaticResumeIsDue) {
    deferAutomaticResume('cooldown_countdown_not_ready')
    return false
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
  const runPromise = runSelectionBatchOnce()
  // runSelectionBatch 在首个 await 前同步设置 busy。若账号路径未保存等
  // 前置校验直接返回，不能把“定时已处理”误报成“已启动”。
  const started = busy.value
  await runPromise
  return started
}

async function loadDailySelectionSchedule(): Promise<void> {
  const schedule = await window.api.selectionSchedule.get()
  savedDailySelectionScheduleEnabled = schedule.enabled
  dailySelectionSchedule.enabled = schedule.enabled
  dailySelectionSchedule.time = schedule.time
  dailySelectionSchedule.launchAtLogin = schedule.launchAtLogin
  dailySelectionSchedule.canLaunchAtLogin = schedule.canLaunchAtLogin
}

async function saveDailySelectionSchedule(): Promise<void> {
  if (dailySelectionSchedule.saving) return
  dailySelectionSchedule.saving = true
  error.value = ''
  try {
    const result = await window.api.selectionSchedule.save({
      enabled: dailySelectionSchedule.enabled,
      time: dailySelectionSchedule.time,
      launchAtLogin: dailySelectionSchedule.enabled && dailySelectionSchedule.launchAtLogin
    })
    if (!result.ok || !result.schedule) throw new Error(result.error || '定时选品设置保存失败。')
    savedDailySelectionScheduleEnabled = result.schedule.enabled
    dailySelectionSchedule.enabled = result.schedule.enabled
    dailySelectionSchedule.time = result.schedule.time
    dailySelectionSchedule.launchAtLogin = result.schedule.launchAtLogin
    dailySelectionSchedule.canLaunchAtLogin = result.schedule.canLaunchAtLogin
    notice.value = result.schedule.enabled
      ? `每日定时选品已启用，每天 ${result.schedule.time} 按当前已保存设置开始。`
      : '每日定时选品已关闭。'
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    dailySelectionSchedule.saving = false
  }
}

function delayScheduledRetry(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

function scheduledLoginBlockReason(): string | null {
  const preflightAccounts = loginPreflight.value?.accounts ?? []
  for (const account of preflightAccounts) {
    const platforms = [
      { name: '百应', state: account.baiying.state, message: account.baiying.message },
      { name: '抖音创作者中心', state: account.creatorCenter.state, message: account.creatorCenter.message }
    ]
    const blocked = platforms.find((platform) =>
      platform.state === 'login_required' || platform.state === 'verification_required' ||
      /(?:需要登录|尚未登录|未登录|登录失效|LOGIN REQUIRED|NOT LOGGED IN|验证码|人工验证|安全验证|CAPTCHA|VERIFICATION REQUIRED)/i.test(platform.message)
    )
    if (blocked) return `账号「${account.accountName}」的${blocked.name}需要登录或完成验证。`
  }

  const pendingErrors = recoveryBatch.value?.accounts
    .filter((account) => account.state !== 'completed')
    .map((account) => account.error || '') ?? []
  const details = [error.value, selectionProgress.value?.stage, selectionProgress.value?.message, ...pendingErrors]
    .filter(Boolean).join('\n')
  const classification = toUserSelectionError(details).classification
  if (classification === 'ACCOUNT_SWITCH_LOGIN' || classification === 'ACCOUNT_SWITCH_CAPTCHA' ||
      /(?:需要登录|尚未登录|未登录|登录失效|LOGIN REQUIRED|NOT LOGGED IN|验证码|人工验证|安全验证|CAPTCHA|VERIFICATION REQUIRED)/i.test(details)) {
    return '百应或创作者中心需要登录或完成验证。'
  }
  return null
}

function scheduledRetryDelay(attempt: number): number {
  const exponent = Math.min(4, Math.max(0, attempt - 1))
  return Math.min(SCHEDULED_RETRY_MAX_DELAY_MS, SCHEDULED_RETRY_BASE_DELAY_MS * (2 ** exponent))
}

async function traceScheduledRun(
  due: SelectionScheduleDue,
  stage: string,
  attempt?: number,
  delayMs?: number
): Promise<void> {
  await window.api.selectionSchedule.trace({ date: due.date, stage, attempt, delayMs }).catch(() => undefined)
}

async function isScheduledSelectionEnabled(): Promise<boolean> {
  try {
    return (await window.api.selectionSchedule.get()).enabled
  } catch {
    // 读取主进程状态短暂失败时，以最近一次加载的已保存值为准；未保存的开关编辑不影响运行中任务。
    return savedDailySelectionScheduleEnabled
  }
}

async function waitForScheduledRetry(milliseconds: number): Promise<void> {
  scheduledRetryWaiting.value = true
  try {
    const deadline = Date.now() + milliseconds
    let nextScheduleCheck = 0
    while (Date.now() < deadline) {
      if (manualPausePending()) return
      if (Date.now() >= nextScheduleCheck) {
        if (!(await isScheduledSelectionEnabled())) return
        nextScheduleCheck = Date.now() + 5_000
      }
      if (scheduledLoginBlockReason()) return
      await delayScheduledRetry(Math.min(1_000, Math.max(1, deadline - Date.now())))
    }
  } finally {
    scheduledRetryWaiting.value = false
  }
}

async function handleScheduledSelectionStart(due: SelectionScheduleDue): Promise<void> {
  if (!selectionInitialized.value || handledScheduledStartDates.has(due.date)) return
  if (automaticScheduledStartActive) {
    if (due.date !== activeScheduledStartDate &&
        (!queuedScheduledStart || queuedScheduledStart.date < due.date)) {
      queuedScheduledStart = due
    }
    return
  }
  handledScheduledStartDates.add(due.date)
  automaticScheduledStartActive = true
  activeScheduledStartDate = due.date
  let outcome: 'completed' | 'login-blocked' | 'disabled' | 'manual-paused' | 'failed' | null = null
  let attempts = 0
  let currentRunStarted = false
  const initialBatch = recoveryBatch.value
  let currentRunBatchId = initialBatch && ['in_progress', 'needs_retry'].includes(initialBatch.state)
    ? initialBatch.batchId : null
  try {
    while (!outcome) {
      let attemptCounted = false
      try {
        if (manualPausePending()) {
          outcome = 'manual-paused'
          notice.value = '本批次已手动暂停；每日定时续做等待点击“继续选品”。'
          break
        }
        const scheduleEnabled = await isScheduledSelectionEnabled()
        if (!scheduleEnabled) {
          outcome = 'disabled'
          notice.value = '每日定时选品已关闭；停止本次自动运行。'
          await traceScheduledRun(due, 'schedule_disabled', attempts)
          break
        }

        const loginBlock = scheduledLoginBlockReason()
        if (loginBlock) {
          outcome = 'login-blocked'
          notice.value = `每日自动选品已暂停：${loginBlock}完成登录或验证后，可在此页重新检查并继续。`
          await traceScheduledRun(due, 'login_blocked', attempts)
          notifyGlobal(notice.value, 'error')
          break
        }
        // unknown / 页面未就绪不是明确的未登录状态；清除旧预检结果并在下一轮重新检查。
        if (loginPreflight.value) loginPreflight.value = null

        if (currentRunStarted && currentRunBatchId !== null &&
            recoveryBatch.value?.batchId === currentRunBatchId && recoveryBatch.value.state === 'completed') {
          outcome = 'completed'
          notice.value = `每日定时选品已完成（${due.date} ${due.time}）。`
          await traceScheduledRun(due, 'run_completed', attempts)
          break
        }

        if (busy.value) {
          if (selectionProgress.value?.state === 'running' && selectionProgress.value.mode === 'batch') {
            currentRunStarted = true
            currentRunBatchId = recoveryBatch.value?.batchId ?? currentRunBatchId
            notice.value = `每日计划已到；当前批次正在执行，完成后会自动检查结果并接续未完成项。`
          } else {
            notice.value = `每日计划已到；客户端正在处理其他操作，空闲后会自动开始选品。`
          }
          await waitForScheduledRetry(1_000)
          continue
        }

        if (selectionCooldownRemaining.value > 0) {
          const remaining = selectionCooldownRemaining.value
          notice.value = `百应冷却中，约 ${remaining} 秒后自动继续；当前批次检查点会保留。`
          await waitForScheduledRetry(Math.min(5_000, Math.max(1_000, remaining * 1_000)))
          continue
        }

        attempts += 1
        attemptCounted = true
        await traceScheduledRun(due, 'attempt_started', attempts)
        error.value = ''
        notice.value = `每日计划 ${due.time}：正在运行选品流程（第 ${attempts} 次尝试）。`
        const started = await startSelection()
        if (manualPausePending()) {
          outcome = 'manual-paused'
          notice.value = '本批次已手动暂停；每日定时续做等待点击“继续选品”。'
          break
        }
        if (started) {
          currentRunStarted = true
          const latestBatch = recoveryBatch.value
          if (latestBatch && (latestBatch.batchId !== initialBatch?.batchId || currentRunBatchId === latestBatch.batchId)) {
            currentRunBatchId = latestBatch.batchId
          }
        }

        const postRunLoginBlock = scheduledLoginBlockReason()
        if (postRunLoginBlock) {
          outcome = 'login-blocked'
          notice.value = `每日自动选品已暂停：${postRunLoginBlock}完成登录或验证后，可在此页重新检查并继续。`
          await traceScheduledRun(due, 'login_blocked', attempts)
          notifyGlobal(notice.value, 'error')
          break
        }
        if (started && currentRunBatchId !== null &&
            recoveryBatch.value?.batchId === currentRunBatchId && recoveryBatch.value.state === 'completed') {
          outcome = 'completed'
          notice.value = `每日定时选品已完成（${due.date} ${due.time}）。`
          await traceScheduledRun(due, 'run_completed', attempts)
          break
        }

        const delayMs = scheduledRetryDelay(attempts)
        const delaySeconds = Math.ceil(delayMs / 1_000)
        const reason = error.value || selectionProgress.value?.message || '流程尚未完成，待恢复检查点已保留'
        const retryMessage = `每日选品第 ${attempts} 次未完成：${reason}。检查点已保留，${delaySeconds} 秒后自动继续。`
        notice.value = retryMessage
        if (selectionProgress.value) {
          selectionProgress.value = {
            ...selectionProgress.value,
            state: 'running', stage: '等待定时自动续跑', message: retryMessage, percent: null
          }
        }
        await traceScheduledRun(due, 'retry_scheduled', attempts, delayMs)
        const retryBatchId = recoveryBatch.value?.batchId
        if (retryBatchId) {
          await runRecovery(['log', 'append', '--batch-id', retryBatchId, '--account-id', '0'], {
            stage: '定时自动续跑', code: 'INFO_SCHEDULED_AUTO_RETRY',
            message: `本次处理未完成，${delaySeconds} 秒后继续尝试（第 ${attempts} 次）。`,
            detail: error.value || selectionProgress.value?.message || ''
          }).catch(() => undefined)
        }
        if (attempts === 1 || attempts % 5 === 0) notifyGlobal(retryMessage, 'error')
        await waitForScheduledRetry(delayMs)
      } catch (cause) {
        if (manualPausePending()) {
          outcome = 'manual-paused'
          notice.value = '本批次已手动暂停；每日定时续做等待点击“继续选品”。'
          break
        }
        error.value = `每日定时选品本轮遇到错误：${String(cause instanceof Error ? cause.message : cause)}`
        const loginBlock = scheduledLoginBlockReason()
        if (loginBlock) {
          outcome = 'login-blocked'
          notice.value = `每日自动选品已暂停：${loginBlock}完成登录或验证后，可在此页重新检查并继续。`
          await traceScheduledRun(due, 'login_blocked', attempts)
          notifyGlobal(notice.value, 'error')
          break
        }
        if (!attemptCounted) attempts += 1
        const delayMs = scheduledRetryDelay(attempts)
        const retryMessage = `${error.value}；${Math.ceil(delayMs / 1_000)} 秒后自动重试，定时选品保持运行。`
        notice.value = retryMessage
        await traceScheduledRun(due, 'attempt_error', attempts, delayMs)
        await traceScheduledRun(due, 'retry_scheduled', attempts, delayMs)
        if (attempts === 1 || attempts % 5 === 0) notifyGlobal(retryMessage, 'error')
        await waitForScheduledRetry(delayMs)
      }
    }
  } finally {
    scheduledRetryWaiting.value = false
    automaticScheduledStartActive = false
    activeScheduledStartDate = null
    if (outcome === 'completed' || outcome === 'login-blocked' || outcome === 'disabled') {
      emit('scheduled-start-handled', due.date, outcome)
    } else {
      // 非终态不能消耗定时计划；清掉页面去重标记后由主进程再次投递。
      handledScheduledStartDates.delete(due.date)
      if (outcome) emit('scheduled-start-handled', due.date, outcome)
    }
    const queued = queuedScheduledStart
    queuedScheduledStart = null
    if (queued) void handleScheduledSelectionStart(queued)
  }
}

watch(() => props.scheduledStart, (due) => {
  if (due) void handleScheduledSelectionStart(due)
})
watch(selectionInitialized, (ready) => {
  if (ready && props.scheduledStart) void handleScheduledSelectionStart(props.scheduledStart)
})

watch(accountId, () => {
  candidates.value = []
  categoryRecommendations.value = []
  previewAccountId.value = null
  candidateSelectionCustomized.value = false
  previewCandidatePool.value = null
  previewCandidatePoolCount.value = 0
  if (selectionUiDataReady.value && (props.active || props.dialogOnly) && activeTab.value === '账号个性化') {
    void loadAccountSettings()
  }
})
watch(activeTab, (tab) => {
  if (selectionUiDataReady.value && (props.active || props.dialogOnly) && tab === '账号个性化') {
    void loadAccountSettings()
  }
})
watch([() => props.active, selectionInitialized], ([active, initialized]) => {
  if (active && initialized) void ensureSelectionUiData()
})

useAppRefresh(async () => {
  if ((!props.active && !(props.dialogOnly && selectionSettingsDialogOpen.value)) || busy.value) return
  await Promise.all([refreshSelectionAccounts(false), loadSavedLocalPools()])
})
onMounted(async () => {
  selectionViewUnmounted = false
  if (!props.dialogOnly) stopSelectionCooldownExpired = window.api.selectionCooldown.onExpired(onSelectionCooldownExpired)
  window.addEventListener('focus', handleSelectionViewVisibilityChange)
  document.addEventListener('visibilitychange', handleSelectionViewVisibilityChange)
  stopSelectionProgress = window.api.browser.onSelectionProgress((event) => {
    queueSelectionProgressUpdate(() => {
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
  })
  stopAutoPublishProgress = window.api.publish.onProgress((event) => {
    queueSelectionProgressUpdate(() => {
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
  })
  busy.value = true
  try {
    defaultDownloadRoot.value = await window.api.dir.getDownloadDir()
    await Promise.all([loadDailySelectionSchedule(), loadGlobal(), loadAccounts()])
    if (props.active || props.dialogOnly) await ensureSelectionUiData()
    await loadRecoveryStatus()
  } catch (cause) {
    error.value = String(cause instanceof Error ? cause.message : cause)
  } finally {
    busy.value = false
    selectionInitialized.value = true
    const pendingPool = recoveryBatch.value?.payload.candidatePool as BatchCandidatePool | undefined
    if (recoveryBatch.value?.state === 'needs_retry' && pendingPool?.status === 'collecting' &&
        !loginPreflight.value && await canResumePendingCandidatePool(recoveryBatch.value, pendingPool)) {
      scheduleCandidatePoolResume(recoveryBatch.value, pendingPool)
      notice.value = `候选池 ${pendingPool.rows.length}/${pendingPool.target}，已从检查点恢复自动续采等待；达到目标前不启动消费账号。`
    } else if (recoveryBatch.value && pendingPool && !loginPreflight.value &&
        hasStaleReadyPoolAccount(recoveryBatch.value, pendingPool)) {
      scheduleCandidatePoolResume(recoveryBatch.value, pendingPool)
      notice.value = '候选池已达标；旧账号候选均不可用，客户端将从现成池自动重分配并续选。'
    }
  }
})

onUnmounted(() => {
  selectionViewUnmounted = true
  savedPoolItemsRequest += 1
  stopSelectionProgress?.()
  stopAutoPublishProgress?.()
  stopSelectionCooldownExpired?.()
  window.removeEventListener('focus', handleSelectionViewVisibilityChange)
  document.removeEventListener('visibilitychange', handleSelectionViewVisibilityChange)
  stopSelectionCooldownTicker()
  if (selectionProgressFlushTimer !== null) window.clearTimeout(selectionProgressFlushTimer)
  selectionProgressFlushTimer = null
  pendingSelectionProgressUpdate = null
  clearAutoResumeTimer()
  clearCandidatePoolResumeTimer()
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
          <p v-if="accountDraft.selectionMode === 'inherit'" class="hint">当前通用模式：{{ global.selectionMode === 'category' ? '品类模式' : '通用数据模式（全部类目，排除 ' + selectedCategoryCount(global.excludedCategorySelection) + ' 个类目）' }}。</p>
          <p v-else-if="accountDraft.selectionMode === 'general'" class="hint">该账号使用通用数据模式，并应用本账号设置中的 {{ selectedCategoryCount(accountDraft.excludedCategorySelection) }} 个排除类目。</p>
          <p v-else class="hint">该账号使用品类模式；下方排除类目会在选定的品类范围内继续生效。</p>
          <details v-if="accountDraft.selectionMode !== 'inherit'" class="category-picker">
            <summary>排除类目（本账号）· 已选 {{ selectedCategoryCount(accountDraft.excludedCategorySelection) }} 项</summary>
            <p class="hint">只影响当前账号；“展开下级”不会排除上级。排除三级时按其二级父类目整支排除。</p>
            <div class="category-grid">
              <div v-for="(group, index) in [level1Options, accountExcludedLevel2Options, accountExcludedLevel3Options]" :key="index" class="category-level">
                <label>{{ ['排除一级类目', '排除二级类目', '排除三级类目'][index] }}</label>
                <div class="category-options">
                  <div v-for="item in group" :key="item.id" class="category-option">
                    <label>
                    <input
                      type="checkbox"
                      :checked="accountDraft.excludedCategorySelection[(['level1', 'level2', 'level3'][index] as CategoryLevel)].includes(item.id)"
                      :disabled="busy"
                      @change="toggleExcludedCategory(item.id, (['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', ($event.target as HTMLInputElement).checked)"
                    >
                    <span>{{ item.name }}</span>
                    </label>
                    <button v-if="index < 2" type="button" :disabled="busy" @click="showExcludedCategoryChildren(item.id, (['level1', 'level2'][index] as CategoryLevel), 'account')">展开下级</button>
                  </div>
                  <div v-if="!group.length" class="category-empty">{{ index === 0 ? '登录百应后等待类目捕获' : '请在上一级点击“展开下级”' }}</div>
                </div>
                <div class="mini-actions">
                  <button :disabled="busy || !group.length" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'all')">全选</button>
                  <button :disabled="busy || !group.length" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'invert')">反选</button>
                  <button :disabled="busy" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'clear')">清空</button>
                </div>
              </div>
            </div>
          </details>
          <template v-if="accountDraft.selectionMode === 'category' || (accountDraft.selectionMode === 'inherit' && global.selectionMode === 'category')">
          <div class="category-mode-options" role="group" aria-label="账号品类筛选策略">
            <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode" value="inherit" :disabled="busy"> 沿用通用设置</label>
            <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode" value="auto" :disabled="busy"> 自动推荐</label>
            <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode" value="manual" :disabled="busy"> 手动选择类目</label>
          </div>
          <p v-if="accountDraft.categoryMode === 'inherit'" class="hint">
            当前沿用通用设置：{{ global.selectionMode !== 'category' ? '通用数据模式，排除 ' + selectedCategoryCount(global.excludedCategorySelection) + ' 个类目' : global.categoryMode === 'manual' ? '手动筛选 ' + selectedCategoryCount(global.categorySelection) + ' 个类目' : '按百应当前可见高分商品自动推荐重点类目' }}。
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
            :rank-category-options="quotaCategoryOptions"
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
          <label class="path-field">视频保存位置<div><input v-model="accountDraft.downloadPath" :disabled="busy" placeholder="留空使用软件默认位置"><button class="btn" :disabled="busy" @click="chooseDownloadPath">选择文件夹</button><button class="btn" :disabled="busy" @click="accountDraft.downloadPath = ''">恢复默认</button></div></label>
          <details class="compact-help browser-profile-advanced" :open="Boolean(accountDraft.browserProfilePath) || undefined"><summary>浏览器登录资料（{{ accountDraft.browserProfilePath ? '已自定义' : '使用默认' }}，一般无需修改）</summary>
            <label class="path-field">浏览器登录资料文件夹<div><input v-model="accountDraft.browserProfilePath" :disabled="busy" placeholder="留空使用软件默认位置"><button class="btn" :disabled="busy" @click="chooseProfilePath">选择文件夹</button><button class="btn" :disabled="busy" @click="accountDraft.browserProfilePath = ''">恢复默认</button></div></label>
            <p class="hint">这里不是视频保存位置。自定义文件夹必须包含 Chrome 的 Default 文件夹；不确定时请点“恢复默认”。</p>
          </details>
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

  <section v-else-if="props.active && !selectionUiDataReady" class="selection-page">
    <p class="hint">正在读取选品设置…</p>
  </section>
  <section v-else-if="props.active" class="selection-page">
    <Teleport to="body">
      <div v-if="selectionSettingsDialogOpen" class="selection-settings-dialog-backdrop" @click="closeSelectionSettingsDialog" @keydown.esc.window.stop="closeSelectionSettingsDialog"></div>
    </Teleport>
    <header class="page-head">
      <div>
        <h1>开始选品</h1>
        <p>按已保存的规则，为启用账号自动挑选商品</p>
      </div>
      <div class="head-actions">
        <button class="btn" type="button" :disabled="busy" title="在当前客户端内打开居中设置面板" @click="openSelectionSettingsDialog">打开设置面板</button>
        <button v-if="(busy && selectionProgress?.mode === 'batch' && selectionProgress?.state === 'running') || (!busy && recoveryBatch?.state === 'needs_retry') || manualPausePending()" class="btn selection-pause-action" type="button" :disabled="manualPausePending() || manualPauseSaving" @click="requestManualSelectionPause">
          {{ manualPausePending() ? (busy ? '正在安全暂停…' : '已暂停选品') : '暂停选品' }}
        </button>
        <button v-if="recoveryBatch?.state === 'needs_retry'" class="btn selection-end-action" type="button" :disabled="busy || scheduledRetryWaiting || manualPauseSaving" @click="abandonStaleBatch">结束上一批次</button>
        <button class="btn primary" :disabled="busy || manualPauseSaving || scheduledRetryWaiting || selectionCooldownRemaining > 0 || (recoveryBatch?.state === 'needs_retry' && selectedRecoveryAccounts.length === 0)" @click="startSelectionFromButton">
          {{ selectionCooldownRemaining > 0 ? `冷却中（${selectionCooldownRemaining} 秒）` : accountPathsHaveUnsavedChanges ? '保存设置并开始选品' : recoveryBatch?.state === 'needs_retry' ? '继续选品' : '自动选品一起工作' }}
        </button>
      </div>
    </header>
    <div v-if="manualPausePending()" class="selection-manual-pause-banner" role="status">
      {{ busy ? '已请求安全暂停。当前操作返回并保存检查点后，会关闭账号浏览器并停止下一步。' : '本批次已手动暂停；冷却和每日计划不会自动续做。点击“继续选品”恢复。' }}
    </div>
    <div v-if="accountPathsHaveUnsavedChanges" class="selection-path-warning" role="status">
      <span>账号路径有改动。点击上方按钮会先保存，再开始选品；如果路径填错，可撤销本次修改。</span>
      <button class="btn" type="button" :disabled="busy" @click="discardUnsavedAccountPaths">撤销路径修改</button>
    </div>
    <p v-if="error" class="error selection-top-feedback" role="alert">{{ selectionErrorPresentation.userMessage }}</p>
    <details v-if="error && selectionErrorPresentation.diagnostic" class="selection-diagnostic">
      <summary>查看错误详情</summary><pre>{{ selectionErrorPresentation.diagnostic }}</pre>
    </details>
    <p v-if="notice" class="notice selection-top-feedback" role="status">{{ selectionNoticePresentation.userMessage }}</p>
    <div v-if="savedLocalPools[0]?.recentCount" class="saved-local-pool-banner" role="status">
      <span>本机已有候选池：{{ savedLocalPools[0].count }} 件已存，{{ savedLocalPools[0].recentCount }} 件在 24 小时内。可先从池内按商品 ID 选品。</span>
      <button class="btn" type="button" :disabled="busy" @click="openSelectionSettingsDialog">查看并使用</button>
    </div>
    <details class="client-advanced">
      <summary>每日定时选品：{{ dailySelectionSchedule.enabled ? `已开启，每天 ${dailySelectionSchedule.time}` : '未开启' }}</summary>
    <section class="daily-selection-schedule" aria-label="每日定时选品">
      <div class="daily-selection-copy">
        <strong>每日定时选品</strong>
        <span>{{ dailySelectionSchedule.enabled ? `每天 ${dailySelectionSchedule.time} 为所有启用账号启动当前自动流程` : '关闭中；启用后按每天设定时间自动开始' }}</span>
        <small>到设定时间自动启动；用户手动暂停时等待点击“继续选品”，未登录或需人工验证时暂停，其余失败保留进度并持续重试。完成后等待下一天计划；客户端需保持运行，安装版可选登录启动。</small>
      </div>
      <label class="daily-schedule-toggle">
        <input v-model="dailySelectionSchedule.enabled" type="checkbox" :disabled="dailySelectionSchedule.saving" @change="!dailySelectionSchedule.enabled && (dailySelectionSchedule.launchAtLogin = false)">
        启用
      </label>
      <label class="daily-schedule-time">开始时间
        <input v-model="dailySelectionSchedule.time" type="time" :disabled="dailySelectionSchedule.saving || !dailySelectionSchedule.enabled">
      </label>
      <label v-if="dailySelectionSchedule.canLaunchAtLogin" class="daily-schedule-toggle">
        <input v-model="dailySelectionSchedule.launchAtLogin" type="checkbox" :disabled="dailySelectionSchedule.saving || !dailySelectionSchedule.enabled">
        随 Windows 登录启动
      </label>
      <span v-else class="daily-schedule-login-hint" title="在安装版客户端中可启用开机启动">需保持客户端运行；安装版可设置随 Windows 登录启动</span>
      <button class="btn" :disabled="dailySelectionSchedule.saving" @click="saveDailySelectionSchedule">
        {{ dailySelectionSchedule.saving ? '保存中…' : '保存定时' }}
      </button>
    </section>
    </details>
    <details v-if="accounts.length" class="account-statuses"><summary>账号选品状态（{{ accounts.length }}）</summary><span v-for="account in accounts" :key="account.id">{{ account.name }}：{{ accountStatuses[account.id] }}</span></details>
    <section v-if="selectionCooldownArmed" class="selection-cooldown" role="status" aria-live="polite">
      <strong>百应暂时限流</strong>
      <p v-if="selectionCooldownRemaining > 0 && autoResumeAccountId !== null">已暂停后续百应请求，约 {{ selectionCooldownRemaining }} 秒后将从本机保存的检查点自动继续选品。</p>
      <p v-else-if="selectionCooldownRemaining > 0">百应请求暂缓约 {{ selectionCooldownRemaining }} 秒；当前没有可自动恢复的限流检查点。</p>
      <p v-else-if="autoResumeAccountId !== null">冷却已结束，正在从保存的检查点自动继续；也可手动点击“继续选品”。</p>
      <p v-else>冷却已结束；当前没有可自动恢复的限流检查点，可在确认页面后重新选品。</p>
    </section>
    <section v-if="showCandidatePoolProgress" class="selection-progress-card selection-pool-progress" :class="{ finished: candidatePoolProgress?.status === 'ready', failed: batchProgressPhase === 'pool' && selectionProgress?.state === 'failed' }" aria-live="polite">
      <div class="selection-progress-head">
        <strong>{{ candidatePoolProgress?.status === 'ready' ? '第一阶段 · 共享候选池已完成' : '第一阶段 · 建立共享候选池' }}</strong>
        <span v-if="candidatePoolProgress?.status === 'ready'">已保存 {{ candidatePoolProgress?.count }}/{{ candidatePoolProgress?.target }} 件唯一商品</span>
        <span v-else-if="selectionProgress?.state === 'running'">采集与复用检查中</span>
        <span v-else>检查点待续</span>
      </div>
      <template v-if="batchProgressPhase === 'pool' || candidatePoolProgress?.status !== 'ready'">
        <div class="selection-progress-overall">
          <strong>{{ candidatePoolProgressPercent === null ? '待读取' : `${candidatePoolProgressPercent}%` }}</strong>
          <span>已保存 {{ candidatePoolProgress?.count ?? '待读取' }}/{{ candidatePoolProgress?.target || '待读取' }} 件；候选池达标后才开始消费账号</span>
        </div>
        <div class="selection-progress-track" role="progressbar"
          :aria-valuenow="candidatePoolProgressPercent" :aria-valuemin="0" :aria-valuemax="100"
          :aria-valuetext="`候选池已保存 ${candidatePoolProgress?.count ?? '待读取'}/${candidatePoolProgress?.target || '待读取'} 件`">
          <span v-if="candidatePoolProgressPercent === null && selectionProgress?.state === 'running'" class="selection-progress-indeterminate"></span>
          <span v-else-if="candidatePoolProgressPercent !== null" class="selection-progress-fill" :style="{ width: `${candidatePoolProgressPercent}%` }"></span>
        </div>
        <p v-if="batchProgressPhase === 'pool' && selectionProgress?.stage" class="selection-progress-message">
          {{ selectionProgress.stage }}<span v-if="selectionProgress.message"> · {{ selectionProgressPresentation.userMessage }}</span>
        </p>
        <div v-if="candidatePoolSourceProgress.length" class="selection-pool-source-progress">
          <span v-for="source in candidatePoolSourceProgress" :key="source.key">
            {{ source.label }}：{{ source.collected }}{{ source.target === null ? ' 件' : `/${source.target} 件` }}
          </span>
        </div>
        <details v-if="candidatePoolProgress?.mode === 'quota' && candidatePoolProgress?.quotaProgress.length" class="selection-progress-details">
          <summary>查看来源与类目配额</summary>
          <ul class="selection-pool-quota-progress">
            <li v-for="quota in candidatePoolProgress?.quotaProgress ?? []" :key="quota.ruleId">
              {{ candidatePoolProgressRuleLabel(quota.ruleId) }}：{{ quota.collected }}/{{ quota.effectiveTarget ?? quota.target }} 件，缺 {{ quota.deficit }} 件
              <small v-if="quota.transferredIn || quota.transferredOut">（原配额 {{ quota.target }}，转入 {{ quota.transferredIn ?? 0 }}、转出 {{ quota.transferredOut ?? 0 }}）</small>
            </li>
          </ul>
        </details>
        <details v-if="batchProgressPhase === 'pool' && selectionProgress?.message && selectionProgressPresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionProgressPresentation.diagnostic }}</pre>
        </details>
      </template>
    </section>
    <section v-if="selectionProgress && (selectionProgress.mode === 'preview' || (batchProgressPhase === 'account' && candidatePoolProgress?.status !== 'collecting'))" class="selection-progress-card" :class="selectionProgress.state" aria-live="polite">
      <div class="selection-progress-head">
        <strong>{{ selectionProgress.mode === 'preview' ? '候选预览进度' : candidatePoolProgress ? '第二阶段 · 逐账号处理进度' : '自动选品进度' }}</strong>
        <span v-if="selectionProgress.accountId !== null">
          已处理 {{ selectionProgress.completedAccounts }}/{{ selectionProgress.accountTotal }} 个账号 · 当前 {{ selectionProgress.accountName }}（{{ selectionProgress.accountIndex }}/{{ selectionProgress.accountTotal }}）
        </span>
        <span v-else-if="selectionProgress.accountTotal > 0">
          已处理账号 {{ selectionProgress.completedAccounts }}/{{ selectionProgress.accountTotal }}
        </span>
      </div>
      <div class="selection-progress-overall">
        <strong>{{ selectionWorkflowPercent === null ? selectionProgress.state === 'running' ? '执行中' : '本轮待续' : `${selectionWorkflowPercent}%` }}</strong>
        <span>{{ selectionProgress.stage }}</span>
      </div>
      <div class="selection-progress-track" role="progressbar"
        :aria-valuenow="selectionWorkflowPercent" :aria-valuemin="0" :aria-valuemax="100"
        :aria-valuetext="selectionWorkflowPercent === null ? selectionProgress.state === 'running' ? '账号阶段执行中' : '账号阶段本轮结束，仍需核对检查点' : `当前账号流程估算 ${selectionWorkflowPercent}%`">
        <span v-if="selectionWorkflowPercent === null && selectionProgress.state === 'running'" class="selection-progress-indeterminate"></span>
        <span v-else-if="selectionWorkflowPercent !== null" class="selection-progress-fill" :style="{ width: `${selectionWorkflowPercent}%` }"></span>
      </div>
      <p v-if="selectionProgress.resultSummary" class="selection-progress-result">{{ selectionProgress.resultSummary }}</p>
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
        <small>{{ candidatePoolProgress && selectionProgress.mode === 'batch' ? '当前账号百分比按阶段权重估算；候选池已存数量以上方落盘结果为准。' : '流程百分比按阶段权重估算；商品计数和可用的阶段比例来自实时回报。' }}</small>
        <p v-if="selectionProgress.message" class="selection-progress-message">{{ selectionProgressPresentation.userMessage }}</p>
        <details v-if="selectionProgress.message && selectionProgressPresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionProgressPresentation.diagnostic }}</pre>
        </details>
      </details>
    </section>

    <details class="client-advanced selection-settings-advanced" :class="{ 'selection-settings-advanced--dialog': selectionSettingsDialogOpen }" :open="selectionSettingsDialogOpen || Boolean(error) || recoveryBatch?.state === 'needs_retry' || undefined">
      <summary @click="selectionSettingsDialogOpen && $event.preventDefault()">
        {{ selectionSettingsDialogOpen ? `选品设置 — ${activeTab === '通用设置' ? '通用' : accounts.find((item) => item.id === accountId)?.name ?? '账号'}` : '调整筛选条件与账号设置' }}
      </summary>
      <button v-if="selectionSettingsDialogOpen" class="selection-settings-dialog-close" type="button" aria-label="关闭选品设置" :disabled="busy" @click="closeSelectionSettingsDialog">×</button>
      <div class="selection-settings-dialog-content">
      <p v-if="selectionSettingsDialogOpen && error" class="error selection-settings-dialog-feedback" role="alert">{{ selectionErrorPresentation.userMessage }}</p>
      <p v-else-if="selectionSettingsDialogOpen && notice" class="notice selection-settings-dialog-feedback" role="status">{{ selectionNoticePresentation.userMessage }}</p>
      <div class="advanced-actions">
        <button class="btn" :disabled="busy || (activeTab === '账号个性化' && accountId === null)" @click="activeTab === '通用设置' ? saveGlobal() : saveAccount()">{{ busy ? '保存中…' : activeTab === '通用设置' ? '保存通用设置' : '保存账号设置' }}</button>
        <button class="btn" type="button" :disabled="busy" @click="refreshSelectionAccounts()">刷新账号（{{ accounts.length }}）</button>
        <button class="btn" :disabled="busy" @click="previewCandidates">预览候选</button>
      </div>
    <div class="tabs">
      <button :class="{ active: activeTab === '通用设置' }" @click="activeTab = '通用设置'">通用设置</button>
      <button :class="{ active: activeTab === '账号个性化' }" @click="activeTab = '账号个性化'">账号个性化选品</button>
      <button v-if="activeTab === '账号个性化'" class="account-state" disabled>当前账号：{{ accounts.find((item) => item.id === accountId)?.name ?? '未选择' }}</button>
    </div>

    <div class="body-layout">
      <div ref="settingsScrollRef" class="settings-scroll">
        <nav class="settings-jumps" aria-label="快速定位设置">
          <template v-if="activeTab === '通用设置'">
            <button class="btn" type="button" @click="jumpToSelectionSection('global-mode')">选品模式</button>
            <button class="btn" type="button" @click="jumpToSelectionSection('global-path')">下载路径</button>
            <button class="btn" type="button" @click="jumpToSelectionSection('global-filters')">选品条件</button>
            <button class="btn" type="button" @click="jumpToSelectionSection('global-quota')">执行与配额</button>
            <button class="btn" type="button" :disabled="busy" @click="bannedWordsDialogOpen = true">屏蔽词</button>
          </template>
          <template v-else>
            <button class="btn" type="button" @click="jumpToSelectionSection('account-mode')">账号模式</button>
            <button class="btn" type="button" @click="jumpToSelectionSection('account-quota')">每日配额</button>
            <button class="btn" type="button" @click="jumpToSelectionSection('account-filters')">账号筛选</button>
            <button class="btn" type="button" @click="jumpToSelectionSection('account-path')">账号路径</button>
          </template>
          <button v-if="candidates.length" class="btn" type="button" @click="jumpToSelectionSection('candidate-pool')">候选池 {{ candidates.length }}</button>
        </nav>
        <section v-if="activeTab === '通用设置' && activeQuotaPool?.rows.length" class="card" aria-label="当前批次候选池商品">
          <h2>当前批次候选池（{{ activeQuotaPool.rows.length }}/{{ activeQuotaPool.target }}）</h2>
          <p class="hint">这里显示当前批次检查点已保存的候选商品；查看详情只读取本机数据，不代表消费账号已核验或加车。</p>
          <label class="active-pool-search">搜索商品 ID 或名称
            <input v-model="activePoolSearch" type="search" placeholder="输入商品 ID 或名称">
          </label>
          <p v-if="!activePoolFilteredItems.length" class="hint">没有找到匹配的商品。</p>
          <ul v-else class="saved-pool-items">
            <li v-for="item in activePoolVisibleItems" :key="item.productId">
              <button type="button" class="candidate-detail-trigger" @click="openCandidateDetails(item, $event)">
                <strong :title="item.title">{{ item.title }}</strong><small>商品 ID：{{ item.productId }} · {{ localPoolSourceLabels[item.sourceKey ?? ''] ?? item.sourceKey ?? '来源未采集' }}</small>
              </button>
            </li>
          </ul>
          <div v-if="activePoolFilteredItems.length" class="saved-pool-pagination">
            <span>共 {{ activePoolFilteredItems.length }} 件 · 第 {{ activePoolPage }}/{{ activePoolPageCount }} 页</span>
            <button class="btn" type="button" :disabled="activePoolPage <= 1" @click="activePoolPage -= 1">上一页</button>
            <button class="btn" type="button" :disabled="activePoolPage >= activePoolPageCount" @click="activePoolPage += 1">下一页</button>
          </div>
        </section>
        <section v-if="activeTab === '通用设置'" class="card saved-local-pools" aria-label="本机已有候选池">
          <div class="section-title">
            <h2>本机已有候选池</h2>
            <button class="btn" type="button" :disabled="busy || savedLocalPoolLoading" @click="loadSavedLocalPools">刷新候选池</button>
          </div>
          <p class="hint">这里显示已落盘的商品 ID。使用时会把该池的筛选条件保存为当前通用设置，保留当前账号目标与执行设置；启动后先剔除已登记商品，再由当前启用账号逐个核验详情。池内数量不等于已加车数量。</p>
          <p v-if="savedLocalPoolLoading" class="hint">正在读取本机候选池…</p>
          <p v-else-if="!savedLocalPools.length" class="hint">本机还没有达到目标并保存的候选池。</p>
          <article v-for="pool in savedLocalPools.slice(0, 3)" :key="pool.batchId" class="saved-local-pool">
            <div class="saved-local-pool-heading">
              <div>
                <strong>已存 {{ pool.count }} 件 / 原目标 {{ pool.target }} 件</strong>
                <span>24 小时内 {{ pool.recentCount }} 件 · {{ pool.mode === 'quota' ? '按来源与类目配额' : '总数目标' }}</span>
                <small>佣金 {{ pool.minimumCommissionRate ?? '不限' }}%–{{ pool.maximumCommissionRate ?? '不限' }}% · 排除类目 {{ pool.excludedCategoryCount }} 项</small>
                <small>采集于 {{ pool.capturedAt ? new Date(pool.capturedAt).toLocaleString('zh-CN') : '时间未知' }} · 原批次 {{ pool.batchId }}</small>
              </div>
              <button class="btn primary" type="button" :disabled="busy || savedLocalPoolLoading || pool.recentCount < 1 || ['in_progress', 'needs_retry'].includes(recoveryBatch?.state ?? '')" @click="useSavedLocalPool(pool)">使用此池开始选品</button>
            </div>
            <div class="saved-local-pool-meter" :aria-label="`已存 ${pool.count} 件，原目标 ${pool.target} 件`">
              <span :style="{ width: `${Math.min(100, Math.round(pool.count / Math.max(1, pool.target) * 100))}%` }"></span>
            </div>
            <div class="saved-local-pool-sources">
              <div v-for="(count, source) in pool.sourceCounts" :key="source">
                <span>{{ localPoolSourceLabels[source] ?? source }} {{ count }} 件</span>
                <div class="saved-local-pool-source-meter"><span :style="{ width: `${localPoolSourcePercent(pool, count)}%` }"></span></div>
              </div>
            </div>
            <details v-if="pool.duplicateBatches.length" class="saved-pool-duplicates">
              <summary>同商品池的其他历史批次（{{ pool.duplicateBatches.length }}）</summary>
              <ul>
                <li v-for="duplicate in pool.duplicateBatches" :key="duplicate.batchId">
                  {{ duplicate.capturedAt ? new Date(duplicate.capturedAt).toLocaleString('zh-CN') : '时间未知' }} · 批次 {{ duplicate.batchId }}
                </li>
              </ul>
            </details>
            <div class="saved-pool-browser">
              <button class="btn" type="button" :aria-expanded="savedPoolBrowserBatchId === pool.batchId" @click="toggleSavedPoolBrowser(pool)">
                {{ savedPoolBrowserBatchId === pool.batchId ? '收起商品列表' : `查看全部 ${pool.count} 件商品` }}
              </button>
              <div v-if="savedPoolBrowserBatchId === pool.batchId" class="saved-pool-browser-content">
                <form class="saved-pool-search" @submit.prevent="loadSavedPoolItems(1)">
                  <input v-model="savedPoolSearch" type="search" aria-label="按商品 ID 或名称搜索候选池" placeholder="搜索商品 ID 或名称">
                  <button class="btn" type="submit" :disabled="savedPoolItemsLoading">搜索</button>
                </form>
                <p v-if="savedPoolItemsLoading" class="hint" role="status">正在读取商品…</p>
                <p v-if="savedPoolItemsError" class="error" role="alert">{{ savedPoolItemsError }}</p>
                <p v-if="!savedPoolItemsLoading && !savedPoolItemsError && !savedPoolItems.length" class="hint">没有找到匹配的商品。</p>
                <ul v-if="savedPoolItems.length" class="saved-pool-items">
                  <li v-for="item in savedPoolItems" :key="item.productId">
                    <button type="button" class="candidate-detail-trigger" @click="openCandidateDetails(item, $event)">
                      <strong :title="item.title">{{ item.title }}</strong><small>商品 ID：{{ item.productId }} · {{ localPoolSourceLabels[item.sourceKey ?? ''] ?? item.sourceKey ?? '来源未采集' }}</small>
                    </button>
                  </li>
                </ul>
                <div v-if="savedPoolTotal > 0" class="saved-pool-pagination">
                  <span>共 {{ savedPoolTotal }} 件 · 第 {{ savedPoolPage }}/{{ savedPoolPageCount }} 页</span>
                  <button class="btn" type="button" :disabled="savedPoolItemsLoading || savedPoolPage <= 1" @click="loadSavedPoolItems(savedPoolPage - 1)">上一页</button>
                  <button class="btn" type="button" :disabled="savedPoolItemsLoading || savedPoolPage >= savedPoolPageCount" @click="loadSavedPoolItems(savedPoolPage + 1)">下一页</button>
                </div>
              </div>
            </div>
          </article>
        </section>
        <details v-if="notice && selectionNoticePresentation.diagnostic" class="selection-diagnostic">
          <summary>查看诊断详情</summary><pre>{{ selectionNoticePresentation.diagnostic }}</pre>
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
        </section>
        <details v-if="!dialogOnly" class="card failure-log-card">
          <summary>选品过程日志{{ recoveryBatch ? ` · 最近批次 ${recoveryBatch.batchId}` : '' }}</summary>
          <div class="section-title">
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
        </details>

        <template v-if="activeTab === '通用设置'">
          <section id="global-mode" class="card">
            <div class="section-title"><h2>选品模式</h2><button class="btn" @click="browserVisible = !browserVisible">{{ browserVisible ? '隐藏百应登录入口' : '显示百应登录入口' }}</button></div>
            <p class="hint">默认从选品广场、热点趋势和联盟榜单采集候选，按统一数据门槛筛选。通用模式可排除指定类目，品类模式可自动推荐或手动限定类目。</p>
            <div class="category-mode-options" role="group" aria-label="通用选品模式">
              <label><input v-model="global.selectionMode" type="radio" name="global-selection-mode" value="general" :disabled="busy"> 通用数据模式（默认全部类目）</label>
              <label><input v-model="global.selectionMode" type="radio" name="global-selection-mode" value="category" :disabled="busy"> 品类模式</label>
            </div>
            <template v-if="global.selectionMode === 'general'">
              <details class="category-picker">
                <summary>排除类目（可选）· 已选 {{ selectedCategoryCount(global.excludedCategorySelection) }} 项</summary>
                <p class="hint">勾选表示排除；点“展开下级”只浏览，不会排除上级。排除三级时按其二级父类目整支排除。</p>
              <div class="category-grid">
                <div v-for="(group, index) in [level1Options, excludedLevel2Options, excludedLevel3Options]" :key="index" class="category-level">
                  <label>{{ ['排除一级类目', '排除二级类目', '排除三级类目'][index] }}</label>
                  <div class="category-options">
                    <div v-for="item in group" :key="item.id" class="category-option">
                      <label>
                      <input
                        type="checkbox"
                        :checked="global.excludedCategorySelection[(['level1', 'level2', 'level3'][index] as CategoryLevel)].includes(item.id)"
                        :disabled="busy"
                        @change="toggleExcludedCategory(item.id, (['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', ($event.target as HTMLInputElement).checked)"
                      >
                      <span>{{ item.name }}</span>
                      </label>
                      <button v-if="index < 2" type="button" :disabled="busy" @click="showExcludedCategoryChildren(item.id, (['level1', 'level2'][index] as CategoryLevel), 'global')">展开下级</button>
                    </div>
                    <div v-if="!group.length" class="category-empty">{{ index === 0 ? '登录百应后等待类目捕获' : '请在上一级点击“展开下级”' }}</div>
                  </div>
                  <div class="mini-actions">
                    <button :disabled="busy || !group.length" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', 'all')">全选</button>
                    <button :disabled="busy || !group.length" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', 'invert')">反选</button>
                    <button :disabled="busy" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'global', 'clear')">清空</button>
                  </div>
                </div>
              </div>
              </details>
            </template>
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

          <section id="global-path" class="card">
            <div class="section-title">
              <h2>通用下载目录</h2>
              <button class="btn" :disabled="busy" @click="chooseGlobalDownloadPath">选择下载目录</button>
            </div>
            <label class="path-field global-download-path">当前通用根目录
              <input :value="defaultDownloadRoot || '正在读取…'" readonly>
            </label>
            <details class="compact-help"><summary>路径规则</summary><p class="hint">选择后立即保存。账号独立下载路径优先；未设置时在通用目录下按账号、日期和商品分层。已有素材不会移动。</p></details>
          </section>

          <details id="global-filters" class="card filter-card">
            <summary>选品条件 · 每轮每账号目标 {{ global.candidateLimit }} 个商品</summary>
            <p v-if="commissionRuleMigrationRequired" class="warning">旧设置把佣金率当上限；请核对现在显示的最低佣金率并保存通用设置，旧批次不会自动改写。</p>
            <SelectionFilterEditor
              v-model="globalFilterSettings"
              title="选品条件"
              :filter-groups="filterGroups"
              :feature-options="featureOptions"
              :rank-category-options="quotaCategoryOptions"
              :filter-catalog-updated-at="filterCatalogUpdatedAt"
              :show-sync="true"
              :syncing="busy"
              @sync="refreshFilterCatalog"
            />
          </details>

          <section id="candidate-collector-pool" class="card">
            <h2>共享候选池采集</h2>
            <div class="form-grid">
              <label>池量设置方式<select v-model="global.candidatePoolMode" :disabled="busy">
                <option value="total">总数目标</option><option value="quota">按来源和品类逐项配额</option>
              </select></label>
            </div>
            <div class="form-grid">
              <label v-if="global.candidatePoolMode === 'total'">采集目标商品数<input v-model.number="global.candidatePoolTarget" type="number" min="1" max="2000" step="1"></label>
            </div>
            <template v-if="global.candidatePoolMode === 'quota'">
              <p class="hint">分别填写三个来源的合计目标；下方指定品类配额计入所属来源，剩余数量由该来源其他允许类目补足。总池目标为三个来源之和；若要填 0，先移除该来源的指定品类配额。</p>
              <div class="form-grid">
                <label>热点趋势目标件数<input type="number" min="0" max="2000" step="1" :value="sourceQuotaTarget('hotSpot')" :disabled="busy" @change="setSourceQuotaTarget('hotSpot', ($event.target as HTMLInputElement).value)"></label>
                <label>联盟榜单目标件数<input type="number" min="0" max="2000" step="1" :value="sourceQuotaTarget('allianceRank')" :disabled="busy" @change="setSourceQuotaTarget('allianceRank', ($event.target as HTMLInputElement).value)"></label>
                <label>选品广场目标件数<input type="number" min="0" max="2000" step="1" :value="sourceQuotaTarget('selectionSquare')" :disabled="busy" @change="setSourceQuotaTarget('selectionSquare', ($event.target as HTMLInputElement).value)"></label>
              </div>
              <p class="hint">按下方规则顺序轮流读取来源，重复商品归入先出现且仍缺额的规则；指定品类规则可排在来源余量规则前。每条配额分别达标后才开始账号选品；排除类目及统一筛选条件始终生效，来源确实耗尽后才转移缺额。</p>
              <div v-for="(rule, index) in global.candidatePoolQuotas" :key="rule.id" class="form-grid">
                <label>来源<select v-model="rule.sourceKey" :disabled="busy">
                  <option value="allianceRank">联盟榜单</option><option value="hotSpot">选品热点</option><option value="selectionSquare">选品广场</option>
                </select></label>
                <label v-if="rule.sourceKey === 'allianceRank'">榜单<select v-model="rule.rankBoard" :disabled="busy">
                  <option>爆款榜</option><option>趋势榜</option><option>带货榜</option><option>常销榜</option>
                </select></label>
                <label>品类<select :value="rule.categoryPath.join('|')" :disabled="busy" @change="setQuotaCategory(rule, ($event.target as HTMLSelectElement).value)">
                  <option value="">全部允许类目（来源目标）</option>
                  <option v-for="category in quotaCategoryOptionsForSource(rule.sourceKey)" :key="category.path.join('|')" :value="category.path.join('|')">{{ category.label }}（{{ category.id }}）</option>
                </select></label>
                <label>目标件数<input v-model.number="rule.target" type="number" min="1" max="2000" step="1" :disabled="busy"></label>
                <div><button type="button" :disabled="busy || index === 0" @click="moveQuotaRule(index, -1)">上移</button>
                  <button type="button" :disabled="busy || index === global.candidatePoolQuotas.length - 1" @click="moveQuotaRule(index, 1)">下移</button>
                  <button type="button" :disabled="busy" @click="global.candidatePoolQuotas.splice(index, 1)">删除</button></div>
              </div>
              <button type="button" :disabled="busy" @click="addQuotaRule">添加品类配额</button>
              <p class="hint">配额合计：{{ quotaPoolTarget }} 件（最多 2000 件）。榜单默认爆款榜，可逐行选其他榜单。原有排除品类继续生效。</p>
              <div v-if="activeQuotaPool?.mode === 'quota'">
                <p v-for="progress in activeQuotaPool.quotaProgress" :key="progress.ruleId" class="hint">
                  {{ quotaProgressLabel(progress.ruleId) }}：{{ progress.collected }}/{{ progress.effectiveTarget ?? progress.target }}，原配额 {{ progress.target }}，转入 {{ progress.transferredIn ?? 0 }}、转出 {{ progress.transferredOut ?? 0 }}，缺 {{ progress.deficit }} 件
                </p>
                <p v-for="(transfer, index) in activeQuotaPool.quotaTransfers ?? []" :key="`${transfer.fromRuleId}-${transfer.toRuleId}-${index}`" class="hint">
                  缺额转移：{{ quotaProgressLabel(transfer.fromRuleId) }} → {{ quotaProgressLabel(transfer.toRuleId) }}，{{ transfer.count }} 件
                </p>
              </div>
            </template>
            <p class="hint">批次先由下列 1–2 个账号顺序只读采集，达到目标数量后，其他账号才从候选池领商品。采集账号也能参加后续选品。</p>
            <label v-for="account in selectionScopeAccounts" :key="account.id">
              <input v-model="global.candidateCollectorAccountIds" type="checkbox" :value="account.id" :disabled="busy || (!global.candidateCollectorAccountIds.includes(account.id) && (account.status !== 'active' || global.candidateCollectorAccountIds.length >= 2))">
              {{ account.name }}（ID {{ account.id }}）
            </label>
          </section>

          <section id="global-quota" class="card">
            <h2>执行设置与每日配额</h2>
            <div class="form-grid">
              <label>置顶发布时间<input v-model="global.execution.pinTime" type="time"></label>
              <label>随机间隔下限（分钟）<input v-model.number="global.execution.randomIntervalMin" type="number" min="0"></label>
              <label>随机间隔上限（分钟）<input v-model.number="global.execution.randomIntervalMax" type="number" min="0"></label>
              <label>通用默认每品视频上限<input v-model.number="global.quota.perProductVideos" type="number" min="1"></label>
              <label>通用默认每账号商品上限<input v-model.number="global.quota.perAccountProducts" type="number" min="1"></label>
              <label>通用默认每账号视频上限<input v-model.number="global.quota.perAccountVideos" type="number" min="1"></label>
            </div>
            <p class="hint">每轮目标数量在“选品条件”设置；这里是每日累计上限。账号未单独设置时使用这些默认值。限额只控制客户端选品和发布计划，不代表平台吞吐承诺；待确认和已排期任务会占用计划日期用量。</p>
          </section>
        </template>

        <template v-else>
          <section id="account-mode" class="card">
            <div class="section-title"><h2>账号与品类</h2><select v-model.number="accountId" class="account-select" :disabled="busy"><option v-for="account in selectionScopeAccounts" :key="account.id" :value="account.id">{{ account.name }}（{{ account.status }}）</option></select></div>
            <p v-if="!accounts.length" class="hint">请先在账号管理中添加账号。</p>
            <p class="hint">账号可继承通用数据/品类模式，也可单独切换；切换为单独模式后，可在下方配置本账号排除类目。</p>
            <div class="category-mode-options" role="group" aria-label="账号选品模式">
              <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode-settings" value="inherit" :disabled="busy"> 沿用通用模式</label>
              <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode-settings" value="general" :disabled="busy"> 通用数据模式</label>
              <label><input v-model="accountDraft.selectionMode" type="radio" name="account-selection-mode-settings" value="category" :disabled="busy"> 品类模式</label>
            </div>
            <p v-if="accountDraft.selectionMode === 'inherit'" class="hint">当前通用模式：{{ global.selectionMode === 'category' ? '品类模式' : '通用数据模式（全部类目，排除 ' + selectedCategoryCount(global.excludedCategorySelection) + ' 个类目）' }}。</p>
            <p v-else-if="accountDraft.selectionMode === 'general'" class="hint">该账号使用通用数据模式，并应用本账号设置中的 {{ selectedCategoryCount(accountDraft.excludedCategorySelection) }} 个排除类目。</p>
            <p v-else class="hint">该账号使用品类模式；下方排除类目会在选定的品类范围内继续生效。</p>
            <details v-if="accountDraft.selectionMode !== 'inherit'" class="category-picker">
              <summary>排除类目（本账号）· 已选 {{ selectedCategoryCount(accountDraft.excludedCategorySelection) }} 项</summary>
            <p class="hint">只影响当前账号；“展开下级”不会排除上级。排除三级时按其二级父类目整支排除。</p>
              <div class="category-grid">
                <div v-for="(group, index) in [level1Options, accountExcludedLevel2Options, accountExcludedLevel3Options]" :key="index" class="category-level">
                  <label>{{ ['排除一级类目', '排除二级类目', '排除三级类目'][index] }}</label>
                  <div class="category-options">
                    <div v-for="item in group" :key="item.id" class="category-option">
                      <label>
                      <input
                        type="checkbox"
                        :checked="accountDraft.excludedCategorySelection[(['level1', 'level2', 'level3'][index] as CategoryLevel)].includes(item.id)"
                        :disabled="busy"
                        @change="toggleExcludedCategory(item.id, (['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', ($event.target as HTMLInputElement).checked)"
                      >
                      <span>{{ item.name }}</span>
                      </label>
                      <button v-if="index < 2" type="button" :disabled="busy" @click="showExcludedCategoryChildren(item.id, (['level1', 'level2'][index] as CategoryLevel), 'account')">展开下级</button>
                    </div>
                  <div v-if="!group.length" class="category-empty">{{ index === 0 ? '登录百应后等待类目捕获' : '请在上一级点击“展开下级”' }}</div>
                  </div>
                  <div class="mini-actions"><button :disabled="busy || !group.length" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'all')">全选</button><button :disabled="busy || !group.length" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'invert')">反选</button><button :disabled="busy" @click="excludedCategoryAction((['level1', 'level2', 'level3'][index] as CategoryLevel), 'account', 'clear')">清空</button></div>
                </div>
              </div>
            </details>
            <template v-if="accountDraft.selectionMode === 'category' || (accountDraft.selectionMode === 'inherit' && global.selectionMode === 'category')">
            <div class="category-mode-options" role="group" aria-label="账号品类筛选策略">
              <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode-settings" value="inherit" :disabled="busy"> 沿用通用设置</label>
              <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode-settings" value="auto" :disabled="busy"> 自动推荐</label>
              <label><input v-model="accountDraft.categoryMode" type="radio" name="account-category-mode-settings" value="manual" :disabled="busy"> 手动选择类目</label>
            </div>
            <p v-if="accountDraft.categoryMode === 'inherit'" class="hint">
              当前沿用通用设置：{{ global.selectionMode !== 'category' ? '通用数据模式，排除 ' + selectedCategoryCount(global.excludedCategorySelection) + ' 个类目' : global.categoryMode === 'manual' ? '手动筛选 ' + selectedCategoryCount(global.categorySelection) + ' 个类目' : '按百应当前可见高分商品自动推荐重点类目' }}。
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
          <section id="account-quota" class="card">
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
          <section id="account-filters" class="card">
            <label class="check-row account-filter-toggle">
              <input type="checkbox" :checked="accountFilterOverridesEnabled" :disabled="busy" @change="setAccountFilterOverrides(($event.target as HTMLInputElement).checked)">
              为此账号单独配置选品筛选条件
            </label>
            <p class="hint">关闭时继承通用筛选条件。屏蔽词始终全局生效，执行设置保持通用。</p>
            <details v-if="accountFilterOverridesEnabled" class="filter-card account-filter-details">
              <summary>编辑账号选品条件</summary>
              <SelectionFilterEditor
                v-model="accountFilterDraft"
                title="账号选品条件"
                :filter-groups="filterGroups"
                :feature-options="featureOptions"
                :rank-category-options="quotaCategoryOptions"
                :filter-catalog-updated-at="filterCatalogUpdatedAt"
                :disabled="busy"
                :show-sync="true"
                :syncing="busy"
                @sync="refreshFilterCatalog"
              />
            </details>
            <p v-else class="account-inherit-state">当前账号继承通用筛选设置。</p>
          </section>
          <section id="account-path" class="card">
            <div class="section-title"><h2>账号路径</h2><button class="btn" :disabled="busy" @click="resetAccountPaths">恢复两个路径默认值</button></div>
            <label class="path-field">视频保存位置<div><input v-model="accountDraft.downloadPath" placeholder="留空使用软件默认位置"><button class="btn" @click="chooseDownloadPath">选择文件夹</button><button class="btn" @click="accountDraft.downloadPath = ''">恢复默认</button></div></label>
            <details class="compact-help browser-profile-advanced" :open="Boolean(accountDraft.browserProfilePath) || undefined"><summary>浏览器登录资料（{{ accountDraft.browserProfilePath ? '已自定义' : '使用默认' }}，一般无需修改）</summary>
              <label class="path-field">浏览器登录资料文件夹<div><input v-model="accountDraft.browserProfilePath" placeholder="留空使用软件默认位置"><button class="btn" @click="chooseProfilePath">选择文件夹</button><button class="btn" @click="accountDraft.browserProfilePath = ''">恢复默认</button></div></label>
              <p class="hint">这里不是视频保存位置。自定义文件夹必须包含 Chrome 的 Default 文件夹；不确定时请点“恢复默认”。</p>
            </details>
            <p v-if="accountPathsHaveUnsavedChanges" class="hint warning">路径有未保存修改；新批次仍会使用上次保存的目录。点击“保存账号设置”后才生效；已有素材不会移动，已开始处理的恢复批次沿用原路径。</p>
          </section>
        </template>

        <section v-if="candidates.length" id="candidate-pool" class="card candidate-card">
          <h2>三源候选池（{{ candidates.length }}）</h2>
          <p class="hint">候选来自选品广场、热点趋势和联盟榜单的当前可见列表，统一按数据门槛筛选并按商品 ID 去重；已登记或受配额、发布任务占用的商品会排除。正式批次会复用匹配的预览池，并在执行前再次检查登记状态。</p>
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
          <div v-for="candidate in candidates" :key="candidate.productId" class="candidate-row">
            <input type="checkbox" :checked="candidate.selected" disabled>
            <span class="candidate-main"><b :title="candidate.title">{{ candidate.title }}</b><small>商品 ID：{{ candidate.productId }} · 月销：{{ candidate.monthSales ?? '平台未提供' }} · 佣金：{{ candidate.commissionRate ?? '平台未提供' }}% · 体验分：{{ candidate.dataMetrics?.experienceScore ?? candidate.shopScore ?? '平台未提供' }} · {{ candidate.trendRising ? '走势上升' : '走势未确认上升' }}</small><small v-if="candidate.detailCapturedAt || candidate.analysisScore !== undefined || candidate.unifiedScore !== undefined">统一分：{{ candidate.unifiedScore ?? candidate.analysisScore ?? '暂无可评分实测数据' }}/100<span v-if="candidate.hotOrRankBoost">（热点/榜单加权 +{{ candidate.hotOrRankBoost }}）</span><span v-if="candidate.businessPriorityBoost">（经营推荐加权 +{{ candidate.businessPriorityBoost }}）</span> · 数据覆盖 {{ candidate.analysisCoverage ?? 0 }}/{{ candidate.analysisMetricCount ?? 0 }} 项 · {{ candidate.analysisStatus ?? '已分析' }}<span v-if="candidate.status"> · {{ candidate.status }}</span></small><details v-if="candidate.analysisReasons?.length" class="candidate-analysis" @click.stop><summary>查看数据分析依据</summary><small>{{ candidate.analysisReasons.join('；') }}</small></details><small v-if="candidate.sources?.length" class="source-badges">来源命中：{{ candidateSourceLabels(candidate) }}</small><small v-if="candidate.categoryStatus === '类目未知'" class="warning">类目未知；通用数据模式可按数据门槛参与，品类推荐不计入</small><small v-if="candidate.publicationLock" class="warning">{{ productPublishLockLabel(candidate.publicationLock) }}</small></span>
            <button class="btn candidate-detail-action" type="button" @click="openCandidateDetails(candidate, $event)">查看详情</button>
          </div>
        </section>
      </div>

      <aside v-if="browserVisible" class="browser-panel">
        <BrowserPane default-target="baiying" :profile-key="browserProfileKey" :profile-path="accountDraft.browserProfilePath || undefined" />
      </aside>
    </div>
      <footer v-if="selectionSettingsDialogOpen" class="selection-settings-dialog-footer">
        <span>设置保存后用于后续选品任务</span>
        <div>
          <button class="btn" type="button" :disabled="busy" @click="closeSelectionSettingsDialog">关闭</button>
          <button class="btn primary" type="button" :disabled="busy || (activeTab === '账号个性化' && accountId === null)" @click="saveSelectionSettingsAndClose">
            {{ busy ? '保存中…' : activeTab === '通用设置' ? '保存通用设置' : '保存账号设置' }}
          </button>
        </div>
      </footer>
      </div>
    </details>
    <Teleport to="body">
      <section v-if="candidateDetails" class="candidate-detail-mask" @click.self="closeCandidateDetails" @keydown.esc.window.stop="closeCandidateDetails">
        <div class="candidate-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="candidate-detail-title">
          <header class="candidate-detail-head">
            <div><h2 id="candidate-detail-title">候选商品详情</h2><p>本机已保存的信息；消费账号仍须按精确商品 ID 核验后才能加车。</p></div>
            <button ref="candidateDetailsCloseButton" class="btn" type="button" aria-label="关闭候选商品详情" @click="closeCandidateDetails">关闭</button>
          </header>
          <div class="candidate-detail-body">
            <section v-for="group in candidateDetailGroups" :key="group.title" class="candidate-detail-group">
              <h3>{{ group.title }}</h3>
              <dl class="candidate-detail-fields">
                <div v-for="field in group.fields" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div>
              </dl>
            </section>
          </div>
        </div>
      </section>
    </Teleport>
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
          <p class="hint">商品名称包含任一词即排除；屏蔽词全局生效。封建迷信相关词由系统强制保留，其他屏蔽词可编辑。</p>
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
              <p v-if="loginPreflight.batchId">先处理当前账号的登录状态；批次已保存，其他账号浏览器不会提前启动。</p>
              <p v-else>每个执行账号都需要分别登录百应和抖音创作者中心。两站任一未确认时，不会创建或恢复选品批次。</p>
            </div>
            <button class="btn" type="button" aria-label="关闭登录检查" :disabled="loginPreflight.checking" @click="loginPreflight = null">×</button>
          </header>
          <p v-if="loginPreflight.checking" class="login-preflight-progress">{{ loginPreflight.batchId ? '正在检查当前账号的百应和创作者中心登录状态，请稍候。' : '正在逐个检查账号的两站登录状态，请稍候。' }}</p>
          <p v-else-if="loginPreflightReady(loginPreflight.accounts)" class="login-preflight-success">{{ loginPreflight.batchId ? '当前账号的百应和创作者中心均已确认。点击「继续选品」后会从当前账号继续。' : '每个账号的百应和创作者中心均已自动或手动确认。点击「继续选品」后将进入选品流程。' }}</p>
          <p v-else class="login-preflight-warning">{{ loginPreflight.batchId ? '批次已保存，当前账号尚未执行选品，后续账号也未启动。请先打开未登录的平台完成登录/验证，再点击「重新检查登录状态」。' : '尚未创建或修改批次。请先打开未登录的平台完成登录/验证，再点击「重新检查登录状态」。' }}显示“需要登录”或“需要人工验证”时必须重新检查通过，不能跳过。</p>
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
.account-settings-mask { position: fixed; inset: 0; z-index: 1200; display: grid; place-items: center; overflow: auto; padding: 16px; background: rgb(15 23 42 / 46%); }
.account-settings-dialog { display: flex; flex: 0 1 auto; flex-direction: column; width: min(1120px, 96vw); height: min(92vh, 900px); max-height: calc(100vh - 32px); min-height: 0; overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-panel); box-shadow: 0 18px 56px rgb(15 23 42 / 24%); }
.account-settings-head, .account-settings-foot { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 15px 18px; border-bottom: 1px solid var(--border); }
.account-settings-head, .account-settings-foot { flex: 0 0 auto; }
.account-settings-head h2 { margin: 0; font-size: 18px; }
.account-settings-head p { margin: 4px 0 0; color: var(--text-muted); font-size: 12px; }
.account-settings-head-actions { display: flex; flex-wrap: wrap; gap: 7px; }
.account-settings-body { flex: 1 1 auto; height: 0; min-height: 0; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; padding: 14px 18px; }
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
.selection-page { display: flex; flex: 1 1 auto; flex-direction: column; width: 100%; height: 100%; min-width: 0; min-height: 0; gap: 10px; overflow: hidden; color: var(--text); }
.selection-path-warning { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-shrink: 0; margin: 0; padding: 10px 12px; border: 1px solid #e8cf96; border-radius: var(--radius); background: #fff8e9; color: #74511c; font-size: 12px; line-height: 1.5; }
.selection-path-warning .btn { flex-shrink: 0; }
.selection-top-feedback { flex-shrink: 0; margin: 0; line-height: 1.5; }
.client-advanced { flex-shrink: 0; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); box-shadow: var(--shadow-card); }
.client-advanced > summary { cursor: pointer; padding: 12px 14px; color: var(--text); font-size: 13px; font-weight: 600; }
.client-advanced[open] > summary { border-bottom: 1px solid var(--border); }
.client-advanced .daily-selection-schedule { margin: 10px; }
.selection-settings-advanced .advanced-actions { display: flex; gap: 8px; padding: 10px 12px 0; }
.selection-settings-advanced .tabs { margin: 10px 12px 0; }
.selection-settings-advanced[open] { display: flex; flex: 1 1 0; flex-direction: column; min-height: 0; overflow: hidden; }
.selection-settings-advanced[open] > summary, .selection-settings-advanced[open] > .selection-settings-dialog-content > .advanced-actions, .selection-settings-advanced[open] > .selection-settings-dialog-content > .tabs { flex: 0 0 auto; }
.selection-settings-advanced[open] > .selection-settings-dialog-content { display: flex; flex: 1 1 0; flex-direction: column; min-height: 0; overflow: hidden; }
.selection-settings-advanced .body-layout { flex: 1 1 0; min-height: 0; max-height: none; padding: 10px 12px 12px; }
.selection-settings-dialog-backdrop { position: fixed; inset: 0; z-index: 1199; background: rgb(15 23 42 / 48%); backdrop-filter: blur(1px); }
.selection-settings-advanced--dialog { position: fixed; z-index: 1200; top: 50%; left: 50%; width: min(1680px, calc(100vw - 32px)); height: min(92vh, 900px); max-width: none; max-height: calc(100vh - 20px); min-height: 0; margin: 0; transform: translate(-50%, -50%); border-radius: 12px; box-shadow: 0 24px 72px rgb(15 23 42 / 30%); }
.selection-settings-advanced--dialog[open] { flex: none; }
.selection-settings-advanced--dialog > summary { flex: 0 0 auto; padding: 17px 58px 16px 20px; font-size: 17px; }
.selection-settings-advanced--dialog > .selection-settings-dialog-content { position: absolute; inset: 58px 0 0; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.selection-settings-dialog-feedback { flex: 0 0 auto; margin: 8px 18px 0; }
.selection-settings-dialog-close { position: absolute; z-index: 2; top: 10px; right: 14px; width: 34px; height: 34px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-panel); color: var(--text-muted); font-size: 22px; line-height: 1; }
.selection-settings-dialog-close:hover { border-color: var(--brand); color: var(--brand); }
.selection-settings-advanced--dialog .advanced-actions { flex: 0 0 auto; padding: 8px 18px 0; }
.selection-settings-advanced--dialog .advanced-actions > button:first-child { display: none; }
.selection-settings-advanced--dialog .tabs { flex: 0 0 auto; margin: 8px 18px 0; }
.selection-settings-advanced--dialog .body-layout { flex: 1 1 auto; height: 0; min-height: 0; padding: 8px 18px 12px; }
.selection-settings-advanced--dialog .settings-scroll { flex: 1 1 auto; width: 100%; height: 100%; min-height: 0; }
.selection-settings-advanced--dialog .browser-panel { display: none; }
.selection-settings-dialog-footer { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 18px; border-top: 1px solid var(--border); background: var(--bg-panel); color: var(--text-muted); font-size: 12px; }
.selection-settings-dialog-footer > div { display: flex; gap: 8px; }
.page-head { position: relative; z-index: 3; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 4px 0; background: var(--bg-app); }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.selection-pause-action, .selection-end-action { min-height: 36px; padding: 7px 14px; font-weight: 700; }
.selection-pause-action { border-color: #d97706; background: #fff4dd; color: #824900; }
.selection-pause-action:hover:not(:disabled) { border-color: #b45309; background: #ffebbd; }
.selection-end-action { border-color: #dc2626; background: #fff1f1; color: #991b1b; }
.selection-end-action:hover:not(:disabled) { border-color: #b91c1c; background: #ffe0e0; }
.selection-manual-pause-banner { margin: 8px 0; padding: 11px 13px; border: 1px solid #d97706; border-radius: var(--radius); background: #fff8e8; color: #704b00; font-size: 12px; font-weight: 600; line-height: 1.5; }
.daily-selection-schedule { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; padding: 11px 14px; border: 1px solid #cfdef8; border-radius: var(--radius); background: linear-gradient(110deg, #f5f9ff, #ffffff); font-size: 12px; box-shadow: var(--shadow-card); }
.daily-selection-copy { display: flex; flex-direction: column; gap: 3px; min-width: 190px; margin-right: auto; }
.daily-selection-copy span, .daily-schedule-login-hint { color: var(--text-muted); }
.daily-schedule-toggle, .daily-schedule-time { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.daily-schedule-time input { min-height: 30px; border: 1px solid var(--border); border-radius: 5px; padding: 3px 6px; color: var(--text); background: var(--bg-panel); }
.daily-schedule-login-hint { max-width: 210px; }
.page-head h1 { margin: 0; font-size: 20px; }
.page-head p, .hint { margin: 5px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.55; }
.tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--border); }
.tabs button { padding: 8px 12px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--text-muted); }
.tabs button.active { border-bottom-color: var(--brand); color: var(--brand); }
.tabs .account-state { margin-left: auto; cursor: default; }
.account-statuses { color: var(--text-muted); font-size: 12px; }
.account-statuses summary { width: fit-content; cursor: pointer; }
.account-statuses span { display: inline-flex; margin: 7px 14px 0 0; }
.global-download-path { margin-top: 10px; }
.selection-progress-card { padding: 11px 13px; border: 1px solid #93c5fd; border-radius: var(--radius); background: var(--brand-soft); color: #1e3a8a; }
.selection-progress-card.finished { border-color: #86efac; background: #f0fdf4; color: #166534; }
.selection-progress-card.failed { border-color: #fecaca; background: #fef2f2; color: #991b1b; }
.selection-cooldown { margin: 10px 0; padding: 11px 13px; border: 1px solid #f0c36d; border-radius: var(--radius); background: #fff8e8; color: #704b00; }
.selection-cooldown p { margin: 5px 0 0; font-size: 12px; }
.selection-diagnostic { margin: 4px 0 8px; color: var(--text-muted); font-size: 11px; }
.selection-diagnostic summary { cursor: pointer; }
.selection-diagnostic pre { max-height: 180px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.selection-progress-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.selection-progress-head span, .selection-progress-card small, .selection-progress-message { color: var(--text-muted); font-size: 11px; }
.selection-pool-progress { margin-bottom: 8px; }
.selection-pool-source-progress { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 9px; color: var(--text-muted); font-size: 12px; }
.selection-pool-quota-progress { display: grid; gap: 5px; margin: 7px 0 0; padding-left: 19px; color: var(--text); font-size: 12px; }
.selection-progress-overall { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-top: 8px; }
.selection-progress-overall strong { flex: 0 0 auto; font-size: 24px; line-height: 1; }
.selection-progress-overall span { min-width: 0; overflow: hidden; color: var(--text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.selection-progress-track { position: relative; height: 7px; margin-top: 8px; overflow: hidden; border-radius: 999px; background: rgba(59, 130, 246, .16); }
.selection-progress-fill { display: block; height: 100%; border-radius: inherit; background: var(--brand); transition: width .2s ease; }
.selection-progress-card.failed .selection-progress-fill { background: #dc2626; }
.selection-progress-indeterminate { position: absolute; inset: 0 auto 0 0; width: 35%; border-radius: inherit; background: var(--brand); animation: selection-progress-slide 1.2s ease-in-out infinite alternate; }
.selection-progress-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 5px 8px; margin: 9px 0 0; padding: 0; list-style: none; }
.selection-progress-steps li { display: flex; align-items: center; min-width: 0; gap: 7px; padding: 5px 7px; border: 1px solid rgba(100, 116, 139, .16); border-radius: 6px; color: var(--text-muted); background: rgba(255, 255, 255, .48); font-size: 11px; }
.selection-progress-steps li.running { border-color: #93c5fd; color: var(--brand); background: rgba(219, 234, 254, .7); }
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
.selection-progress-result { margin: 7px 0 0; color: var(--text-muted); font-size: 12px; font-weight: 600; line-height: 1.45; }
.selection-progress-card.failed .selection-progress-result { color: #991b1b; }
.selection-progress-details { margin-top: 7px; color: var(--text-muted); font-size: 11px; }
.selection-progress-details > summary, .category-recommendation-more > summary { width: fit-content; cursor: pointer; }
.selection-progress-details[open] > summary { margin-bottom: 4px; }
@keyframes selection-progress-slide { from { transform: translateX(-20%); } to { transform: translateX(210%); } }
.body-layout { display: flex; flex: 1; min-height: 0; gap: 10px; }
.settings-scroll { flex: 1; min-width: 0; min-height: 0; overflow: auto; overscroll-behavior-y: contain; overflow-anchor: none; scrollbar-gutter: stable; padding: 1px 4px 12px 1px; scroll-padding-top: 48px; }
.settings-jumps { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; gap: 6px; padding: 7px 2px; border-bottom: 1px solid var(--border); background: var(--bg-app); }
.settings-jumps .btn { flex: 0 0 auto; min-height: 28px; padding: 0 10px; white-space: nowrap; font-size: 12px; }
.browser-panel { width: 43%; min-width: 0; min-height: 0; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.card { margin-bottom: 10px; padding: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); box-shadow: var(--shadow-card); }
.card h2 { margin: 0 0 10px; font-size: 15px; }
.section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.category-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.category-picker { margin-top: 12px; padding: 9px 10px; border: 1px solid var(--border); border-radius: var(--radius); }
.category-picker summary, .compact-help summary { cursor: pointer; color: var(--text); font-size: 12px; font-weight: 600; }
.filter-card > summary { cursor: pointer; color: var(--text); font-size: 15px; font-weight: 600; }
.filter-card[open] > summary { margin-bottom: 12px; }
.account-filter-details { margin-top: 9px; }
.compact-help { margin-top: 8px; }
.browser-profile-advanced { padding: 9px 11px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); }
.browser-profile-advanced .hint { margin: 8px 0 0; }
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
.category-option { display: flex !important; flex-direction: row !important; align-items: center; gap: 7px !important; min-height: 24px; padding: 2px 4px; border-radius: 4px; color: var(--text) !important; cursor: pointer; content-visibility: auto; contain-intrinsic-size: auto 28px; }
.category-option > label { display: flex; align-items: center; gap: 7px; flex: 1; min-width: 0; cursor: pointer; }
.category-option > button { border: 0; background: transparent; color: var(--brand); font-size: 11px; white-space: nowrap; cursor: pointer; }
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
.candidate-row { display: flex; align-items: flex-start; gap: 8px; padding: 7px 2px; border-top: 1px solid var(--border); content-visibility: auto; contain-intrinsic-size: auto 120px; }
.candidate-row > input { margin-top: 3px; }
.candidate-main { flex: 1; min-width: 0; }
.candidate-detail-action { flex: 0 0 auto; align-self: center; }
.candidate-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.candidate-main b { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; color: var(--text); font-size: 13px; }
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
.saved-local-pools { margin: 10px 0; }
.saved-local-pool-banner { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 10px 0; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); color: var(--text); font-size: 13px; }
.saved-local-pools > .hint { margin: 7px 0; }
.saved-local-pool { margin-top: 9px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.saved-local-pool-heading { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
.saved-local-pool-heading > div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.saved-local-pool-heading span, .saved-local-pool-heading small { color: var(--text-muted); }
.saved-local-pool-heading small { overflow-wrap: anywhere; }
.saved-local-pool-meter, .saved-local-pool-source-meter { height: 8px; overflow: hidden; border-radius: 99px; background: var(--border); }
.saved-local-pool-meter { margin: 10px 0; }
.saved-local-pool-meter > span, .saved-local-pool-source-meter > span { display: block; height: 100%; border-radius: inherit; background: var(--brand); }
.saved-local-pool-sources { display: flex; flex-wrap: wrap; gap: 8px; }
.saved-local-pool-sources > div { min-width: 130px; flex: 1 1 130px; font-size: 12px; color: var(--text-muted); }
.saved-local-pool-source-meter { height: 5px; margin-top: 4px; }
.saved-pool-duplicates { margin-top: 9px; color: var(--text-muted); font-size: 12px; }
.saved-pool-duplicates summary { cursor: pointer; }
.saved-pool-duplicates ul { margin: 6px 0 0; padding-left: 20px; }
.saved-pool-browser { margin-top: 12px; }
.saved-pool-browser-content { margin-top: 10px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); }
.active-pool-search { display: flex; flex-direction: column; gap: 6px; max-width: 380px; margin-top: 12px; color: var(--text-muted); font-size: 12px; }
.active-pool-search input { min-height: 32px; padding: 5px 9px; border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-panel); color: var(--text); }
.saved-pool-search { display: flex; gap: 8px; }
.saved-pool-search input { flex: 1; min-width: 0; min-height: 32px; padding: 5px 9px; border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-app); color: var(--text); }
.active-pool-search input:focus-visible, .saved-pool-search input:focus-visible, .candidate-detail-trigger:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
.saved-pool-items { list-style: none; margin: 10px 0 0; padding: 0; max-height: 320px; overflow: auto; }
.saved-pool-items li + li { border-top: 1px solid var(--border); }
.candidate-detail-trigger { display: flex; flex-direction: column; gap: 3px; width: 100%; padding: 7px 4px; border: 0; border-radius: var(--radius-control); background: transparent; color: var(--text); text-align: left; cursor: pointer; }
.candidate-detail-trigger:hover { background: var(--brand-soft); }
.candidate-detail-trigger strong { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere; }
.candidate-detail-trigger small { color: var(--text-muted); overflow-wrap: anywhere; }
.saved-pool-pagination { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-top: 10px; font-size: 12px; color: var(--text-muted); }
.saved-pool-pagination span { margin-right: auto; }
.candidate-detail-mask { position: fixed; inset: 0; z-index: 1400; display: grid; place-items: center; padding: 16px; background: rgb(29 29 31 / 40%); }
.candidate-detail-dialog { display: flex; flex-direction: column; width: min(860px, 100%); max-height: min(90vh, 900px); overflow: hidden; border: 1px solid var(--border); border-radius: 20px; background: var(--bg-panel); color: var(--text); box-shadow: var(--shadow-float); }
.candidate-detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; padding: 17px 19px 12px; border-bottom: 1px solid var(--border); }
.candidate-detail-head h2 { margin: 0; font-size: 19px; }
.candidate-detail-head p { margin: 6px 0 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }
.candidate-detail-body { display: grid; gap: 12px; overflow: auto; padding: 16px 19px 20px; }
.candidate-detail-group { padding: 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-app); }
.candidate-detail-group h3 { margin: 0 0 10px; font-size: 14px; }
.candidate-detail-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; margin: 0; }
.candidate-detail-fields > div { min-width: 0; }
.candidate-detail-fields dt { margin-bottom: 3px; color: var(--text-muted); font-size: 11px; }
.candidate-detail-fields dd { margin: 0; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.recovery-card p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.recovery-account-list { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
.recovery-account-option, .recovery-account-completed { display: inline-flex; align-items: center; gap: 6px; min-height: 30px; padding: 4px 8px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-panel); color: var(--text); font-size: 12px; }
.recovery-account-option input { margin: 0; }
.recovery-account-option small { color: var(--text-muted); }
.recovery-account-completed { color: var(--text-muted); }
.recovery-account-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-top: 9px; }
.failure-log-card { margin-bottom: 10px; }
.failure-log-card > summary { cursor: pointer; font-size: 12px; font-weight: 600; }
.failure-log-card .section-title { margin-top: 10px; }
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
.run-log-level.is-info { background: var(--brand-soft); color: var(--brand); }
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
@media (max-width: 1150px) {
  .browser-panel { width: 38%; min-width: 0; }
  .form-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 900px) {
  .body-layout { display: flex; flex-direction: column; gap: 10px; }
  .settings-scroll { order: 1; min-height: 0; }
  .browser-panel { order: 2; flex: 0 1 180px; width: 100%; max-height: 240px; overflow: auto; }
  .settings-jumps { position: static; }
}
@media (max-width: 650px) {
  .candidate-row { flex-wrap: wrap; }
  .candidate-detail-action { margin-left: 22px; }
  .candidate-detail-fields { grid-template-columns: minmax(0, 1fr); }
  .candidate-detail-head, .candidate-detail-body { padding-left: 14px; padding-right: 14px; }
}
@media (max-height: 700px) {
  .browser-panel { max-height: 150px; }
  .card { margin-bottom: 8px; padding: 11px; }
  .account-settings-mask { padding: 8px; }
  .account-settings-dialog { height: calc(100vh - 16px); max-height: calc(100vh - 16px); }
}
/* 设置弹窗与候选池保持清晰层级，避免高密度配置变得拥挤。 */
.selection-page { gap: 12px; }
.page-head { padding: 7px 2px 10px; }
.page-head h1 { font-size: 24px; letter-spacing: -.03em; }
.selection-settings-dialog-backdrop { background: rgb(29 29 31 / 40%); backdrop-filter: none; }
.selection-settings-advanced--dialog { border-radius: 20px; box-shadow: var(--shadow-float); }
.selection-settings-advanced--dialog > summary { font-size: 19px; letter-spacing: -.02em; }
.selection-settings-dialog-close { border-radius: var(--radius-control); }
.settings-jumps { gap: 7px; padding: 9px 2px; background: var(--bg-panel); }
.card { margin-bottom: 12px; padding: 16px; }
.card h2 { font-size: 16px; letter-spacing: -.01em; }
.form-grid { gap: 10px; }
.form-grid input, .form-grid select, .path-field input, .account-select { min-height: 32px; border-radius: var(--radius-control); background: var(--bg-panel); }
.form-grid textarea { border-radius: var(--radius-control); background: var(--bg-panel); }
.btn { min-height: 32px; border-radius: var(--radius-control); }
.selection-pause-action, .selection-end-action { min-height: 36px; }
.btn.primary { border-color: var(--brand); background: var(--brand); color: #fff; }
.filter-chip { border-radius: 999px; }
.banned-words-dialog, .login-preflight-dialog { border-radius: 20px; box-shadow: var(--shadow-float); }
.account-settings-dialog { border-radius: 20px; box-shadow: var(--shadow-float); }
.account-settings-mask, .banned-words-mask, .login-preflight-mask { background: rgb(29 29 31 / 40%); }
.selection-progress-card { padding: 14px 16px; }
.selection-progress-steps { gap: 8px; }
.selection-progress-steps li { border-radius: var(--radius-control); }
</style>
