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
    priority: Array<'hotSpot' | 'allianceRank'>
  }
  thresholds: {
    salesAmount: number | null
    salesCount: number | null
    conversionRate: number | null
    views: number | null
    commissionRate: number | null
    experienceScore: number | null
  }
  trendPreference: string
}

export const DEFAULT_SELECTION_FILTER_SETTINGS: SelectionFilterSettings = {
  candidateLimit: 5,
  filters: { delivery: {}, product: {}, features: [] },
  poolPriority: ['流量扶持', '小二严选', '低价好货', '福袋奖品'],
  poolFallback: true,
  sources: { hotSpotEnabled: true, allianceRankEnabled: true, priority: ['hotSpot', 'allianceRank'] },
  thresholds: { salesAmount: null, salesCount: null, conversionRate: null, views: null, commissionRate: null, experienceScore: null },
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
  const productFilters = normalizeFilterGroup(sourceFilters.product, fallbackFilters.product)
  // 旧版本保存的是百应固定区间；新版本改为本地数值阈值，避免旧档位继续限制候选。
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
      delivery: normalizeFilterGroup(sourceFilters.delivery, fallbackFilters.delivery),
      product: productFilters,
      features: normalizeFeatures(sourceFilters.features, fallbackFilters.features)
    },
    poolPriority: Array.isArray(source.poolPriority) ? source.poolPriority.map(String) : [...fallback.poolPriority],
    poolFallback: typeof source.poolFallback === 'boolean' ? source.poolFallback : fallback.poolFallback,
    sources: {
      hotSpotEnabled: typeof sourceSources.hotSpotEnabled === 'boolean' ? sourceSources.hotSpotEnabled : fallbackSources.hotSpotEnabled,
      allianceRankEnabled: typeof sourceSources.allianceRankEnabled === 'boolean' ? sourceSources.allianceRankEnabled : fallbackSources.allianceRankEnabled,
      priority: normalizedSourcePriority
    },
    thresholds: {
      salesAmount: normalizeThreshold(sourceThresholds.salesAmount, fallbackThresholds.salesAmount),
      salesCount: normalizeThreshold(sourceThresholds.salesCount, fallbackThresholds.salesCount),
      conversionRate: normalizeThreshold(sourceThresholds.conversionRate, fallbackThresholds.conversionRate),
      views: normalizeThreshold(sourceThresholds.views, fallbackThresholds.views),
      commissionRate: normalizeThreshold(sourceThresholds.commissionRate, fallbackThresholds.commissionRate),
      experienceScore: normalizeThreshold(sourceThresholds.experienceScore, fallbackThresholds.experienceScore)
    },
    trendPreference: typeof source.trendPreference === 'string' ? source.trendPreference : fallback.trendPreference
  }
}
