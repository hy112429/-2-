export type CandidatePoolSource = 'hotSpot' | 'allianceRank' | 'selectionSquare'
export type CandidatePoolRankBoard = '爆款榜' | '趋势榜' | '带货榜' | '常销榜'
export interface CandidatePoolQuotaRule {
  id: string
  sourceKey: CandidatePoolSource
  rankBoard: CandidatePoolRankBoard
  categoryId: string
  categoryPath: string[]
  target: number
}

export interface SelectionFilterSettings {
  candidateLimit: number
  filters: {
    delivery: Record<string, unknown>
    product: Record<string, unknown>
    features: string[]
  }
  poolPriority: string[]
  poolFallback: boolean
  sources: {
    hotSpotEnabled: boolean
    allianceRankEnabled: boolean
    selectionSquareEnabled: boolean
    rankBoard: '爆款榜' | '趋势榜' | '带货榜' | '常销榜'
    rankPeriod: '实时' | '昨日' | '近7日' | '近30日'
    rankCarrier: '不限载体' | '短视频' | '直播' | '橱窗' | '图文'
    rankPriceRange: '不限' | '1-10' | '10-50' | '50-100' | '100-500' | '500-5000' | '5000+'
    rankCategoryPath: string[]
    hotSpotMinExposure: number | null
    hotSpotMinRelatedProducts: number | null
    priority: Array<'hotSpot' | 'allianceRank'>
  }
  thresholds: {
    salesAmount: number | null
    salesCount: number | null
    conversionRate: number | null
    views: number | null
    commissionRate: number | null
    commissionRateMax: number | null
    experienceScore: number | null
  }
  trendPreference: string
}

export const DEFAULT_SELECTION_FILTER_SETTINGS: SelectionFilterSettings = {
  candidateLimit: 5,
  filters: { delivery: {}, product: {}, features: [] },
  poolPriority: ['流量扶持', '小二严选', '低价好货', '福袋奖品'],
  poolFallback: true,
  sources: { hotSpotEnabled: true, allianceRankEnabled: true, selectionSquareEnabled: true, rankBoard: '爆款榜', rankPeriod: '昨日', rankCarrier: '不限载体', rankPriceRange: '不限', rankCategoryPath: [], hotSpotMinExposure: null, hotSpotMinRelatedProducts: null, priority: ['hotSpot', 'allianceRank'] },
  thresholds: { salesAmount: null, salesCount: null, conversionRate: null, views: null, commissionRate: null, commissionRateMax: null, experienceScore: null },
  trendPreference: '上升优先'
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function normalizeFilterGroup(value: unknown, fallback: Record<string, unknown>): Record<string, unknown> {
  return { ...fallback, ...asRecord(value) }
}

function normalizeFeatures(value: unknown, fallback: string[]): string[] {
  if (Array.isArray(value)) return value.map(String)
  if (value && typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .filter(([, selected]) => Boolean(selected))
      .map(([label]) => label)
  }
  return [...fallback]
}

function normalizeThreshold(value: unknown, fallback: number | null): number | null {
  if (value === null || value === '') return null
  const numberValue = Number(value)
  return Number.isFinite(numberValue) ? numberValue : fallback
}

/** 合并账号筛选覆盖项与通用筛选默认值；屏蔽词、配额和执行设置不属于本结构。 */
export function createSelectionFilterSettings(
  value?: unknown,
  fallback: SelectionFilterSettings = DEFAULT_SELECTION_FILTER_SETTINGS
): SelectionFilterSettings {
  const source = asRecord(value)
  const sourceFilters = asRecord(source.filters)
  const sourceThresholds = asRecord(source.thresholds)
  const fallbackFilters = fallback.filters
  const fallbackThresholds = fallback.thresholds
  const fallbackSources = fallback.sources
  const candidateLimit = Number(source.candidateLimit)
  const deliveryFilters = normalizeFilterGroup(sourceFilters.delivery, fallbackFilters.delivery)
  const productFilters = normalizeFilterGroup(sourceFilters.product, fallbackFilters.product)
  // 数字佣金上下限是唯一设置入口；旧版手选的百应档位不能继续覆盖它。
  delete deliveryFilters['佣金区间']
  delete productFilters['佣金区间']
  const sourceSources = asRecord(source.sources)
  const sourcePriority = Array.isArray(sourceSources.priority)
    ? sourceSources.priority.map(String).filter((item): item is 'hotSpot' | 'allianceRank' => item === 'hotSpot' || item === 'allianceRank')
    : [...fallbackSources.priority]
  const normalizedSourcePriority = [...new Set(sourcePriority)]
  for (const item of fallbackSources.priority) {
    if (!normalizedSourcePriority.includes(item)) normalizedSourcePriority.push(item)
  }
  return {
    candidateLimit: Number.isFinite(candidateLimit) ? candidateLimit : fallback.candidateLimit,
    filters: {
      delivery: deliveryFilters,
      product: productFilters,
      features: normalizeFeatures(sourceFilters.features, fallbackFilters.features)
    },
    poolPriority: Array.isArray(source.poolPriority) ? source.poolPriority.map(String) : [...fallback.poolPriority],
    poolFallback: typeof source.poolFallback === 'boolean' ? source.poolFallback : fallback.poolFallback,
    sources: {
      hotSpotEnabled: typeof sourceSources.hotSpotEnabled === 'boolean' ? sourceSources.hotSpotEnabled : fallbackSources.hotSpotEnabled,
      allianceRankEnabled: typeof sourceSources.allianceRankEnabled === 'boolean' ? sourceSources.allianceRankEnabled : fallbackSources.allianceRankEnabled,
      selectionSquareEnabled: typeof sourceSources.selectionSquareEnabled === 'boolean' ? sourceSources.selectionSquareEnabled : fallbackSources.selectionSquareEnabled,
      rankBoard: ['爆款榜', '趋势榜', '带货榜', '常销榜'].includes(String(sourceSources.rankBoard)) ? sourceSources.rankBoard as SelectionFilterSettings['sources']['rankBoard'] : fallbackSources.rankBoard,
      rankPeriod: ['实时', '昨日', '近7日', '近30日'].includes(String(sourceSources.rankPeriod)) ? sourceSources.rankPeriod as SelectionFilterSettings['sources']['rankPeriod'] : fallbackSources.rankPeriod,
      rankCarrier: ['不限载体', '短视频', '直播', '橱窗', '图文'].includes(String(sourceSources.rankCarrier)) ? sourceSources.rankCarrier as SelectionFilterSettings['sources']['rankCarrier'] : fallbackSources.rankCarrier,
      rankPriceRange: ['不限', '1-10', '10-50', '50-100', '100-500', '500-5000', '5000+'].includes(String(sourceSources.rankPriceRange)) ? sourceSources.rankPriceRange as SelectionFilterSettings['sources']['rankPriceRange'] : fallbackSources.rankPriceRange,
      rankCategoryPath: Array.isArray(sourceSources.rankCategoryPath) ? sourceSources.rankCategoryPath.map(String) : [...fallbackSources.rankCategoryPath],
      hotSpotMinExposure: normalizeThreshold(sourceSources.hotSpotMinExposure, fallbackSources.hotSpotMinExposure),
      hotSpotMinRelatedProducts: normalizeThreshold(sourceSources.hotSpotMinRelatedProducts, fallbackSources.hotSpotMinRelatedProducts),
      priority: normalizedSourcePriority
    },
    thresholds: {
      salesAmount: normalizeThreshold(sourceThresholds.salesAmount, fallbackThresholds.salesAmount),
      salesCount: normalizeThreshold(sourceThresholds.salesCount, fallbackThresholds.salesCount),
      conversionRate: normalizeThreshold(sourceThresholds.conversionRate, fallbackThresholds.conversionRate),
      views: normalizeThreshold(sourceThresholds.views, fallbackThresholds.views),
      commissionRate: normalizeThreshold(sourceThresholds.commissionRate, fallbackThresholds.commissionRate),
      commissionRateMax: normalizeThreshold(sourceThresholds.commissionRateMax, fallbackThresholds.commissionRateMax),
      experienceScore: normalizeThreshold(sourceThresholds.experienceScore, fallbackThresholds.experienceScore)
    },
    trendPreference: typeof source.trendPreference === 'string' ? source.trendPreference : fallback.trendPreference
  }
}
