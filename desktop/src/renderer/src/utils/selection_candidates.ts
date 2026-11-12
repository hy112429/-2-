export interface SelectionCandidateRankInput {
  productId: string
  leafCategoryId?: string
  categoryIds?: string[]
  unifiedScore?: number | null
  analysisScore?: number | null
  analysisRawScore?: number | null
  poolRank?: number
  batchRandomKey?: number
  selected?: boolean
}

export interface SelectionCategoryRecommendation {
  categoryId: string
  highScoreCount: number
  averageScore: number
}

export interface SelectionRegistrySnapshot {
  usedProductIds: string[]
  accountUsedCategoryIds: string[]
  categoryUsage?: Record<string, number>
}

export function candidateLeafCategoryId(candidate: SelectionCandidateRankInput): string {
  return String(candidate.leafCategoryId ?? candidate.categoryIds?.at(-1) ?? '').trim()
}

export function filterCandidatesByRegistry<T extends SelectionCandidateRankInput>(
  rows: T[],
  snapshot: SelectionRegistrySnapshot,
  allowUnknownCategory = false,
  enforceCategoryUniqueness = true
): { eligible: T[]; excluded: Array<{ candidate: T; reason: string }> } {
  const usedProducts = new Set(snapshot.usedProductIds.map(String))
  const usedCategories = new Set(snapshot.accountUsedCategoryIds.map(String))
  const eligible: T[] = []
  const excluded: Array<{ candidate: T; reason: string }> = []
  for (const candidate of rows) {
    const categoryId = candidateLeafCategoryId(candidate)
    if (!candidate.productId) {
      excluded.push({ candidate, reason: '来源数据缺少商品 ID' })
    } else if (usedProducts.has(candidate.productId)) {
      excluded.push({ candidate, reason: '商品 ID 已在全账号池登记' })
    } else if (!categoryId && !allowUnknownCategory) {
      excluded.push({ candidate, reason: '品类模式下来源数据缺少三级类目 ID' })
    } else if (enforceCategoryUniqueness && categoryId && usedCategories.has(categoryId)) {
      excluded.push({ candidate, reason: '该账号历史或本轮已使用此末级类目' })
    } else {
      eligible.push(candidate)
      if (enforceCategoryUniqueness && categoryId) usedCategories.add(categoryId)
    }
  }
  return { eligible, excluded }
}

export function rankByRecommendedCategories<T extends SelectionCandidateRankInput>(
  rows: T[],
  categoryUsage: Record<string, number>,
  targetCount: number
): { candidates: Array<T & { selected: true }>; recommendations: SelectionCategoryRecommendation[] } {
  const scoreOf = (candidate: SelectionCandidateRankInput): number => {
    const score = Number(candidate.unifiedScore ?? candidate.analysisScore ?? candidate.analysisRawScore)
    return Number.isFinite(score) ? score : 0
  }
  const ranked = [...rows].sort((left, right) =>
    scoreOf(right) - scoreOf(left) || Number(left.poolRank ?? 99) - Number(right.poolRank ?? 99)
  )
  const highScoreRows = ranked.filter((candidate) => scoreOf(candidate) >= 60)
  const sample = highScoreRows.length ? highScoreRows : ranked.slice(0, Math.max(1, targetCount))
  const grouped = new Map<string, T[]>()
  for (const candidate of sample) {
    const categoryId = candidateLeafCategoryId(candidate)
    if (!categoryId) continue
    grouped.set(categoryId, [...(grouped.get(categoryId) ?? []), candidate])
  }
  const categoryOrder = [...grouped.entries()].sort(([leftId, leftRows], [rightId, rightRows]) => {
    const leftAverage = leftRows.reduce((sum, row) => sum + scoreOf(row), 0) / leftRows.length
    const rightAverage = rightRows.reduce((sum, row) => sum + scoreOf(row), 0) / rightRows.length
    return rightRows.length - leftRows.length || rightAverage - leftAverage ||
      Number(categoryUsage[leftId] ?? 0) - Number(categoryUsage[rightId] ?? 0) || leftId.localeCompare(rightId)
  })
  const recommendations = categoryOrder.map(([categoryId, categoryRows]) => ({
    categoryId,
    highScoreCount: categoryRows.length,
    averageScore: Number((categoryRows.reduce((sum, row) => sum + scoreOf(row), 0) / categoryRows.length).toFixed(1))
  }))
  const orderedCategoryIds = categoryOrder.map(([id]) => id)
  const recommendedCategoryIds = new Set(orderedCategoryIds)
  const unrankedCategoryIds = [...new Set(rows.map(candidateLeafCategoryId))]
    .filter((id) => id && !recommendedCategoryIds.has(id))
    .sort((leftId, rightId) => {
      const leftRows = rows.filter((row) => candidateLeafCategoryId(row) === leftId)
      const rightRows = rows.filter((row) => candidateLeafCategoryId(row) === rightId)
      const leftAverage = leftRows.reduce((sum, row) => sum + scoreOf(row), 0) / Math.max(1, leftRows.length)
      const rightAverage = rightRows.reduce((sum, row) => sum + scoreOf(row), 0) / Math.max(1, rightRows.length)
      return rightAverage - leftAverage ||
        Number(categoryUsage[leftId] ?? 0) - Number(categoryUsage[rightId] ?? 0) ||
        leftId.localeCompare(rightId)
    })
  orderedCategoryIds.push(...unrankedCategoryIds)
  const groupedCandidates = new Map(orderedCategoryIds.map((id) => [id, rows
    .filter((candidate) => candidateLeafCategoryId(candidate) === id)
    .sort((left, right) => scoreOf(right) - scoreOf(left) || left.productId.localeCompare(right.productId))
  ]))
  const interleaved: Array<T & { selected: true }> = []
  for (let offset = 0; interleaved.length < rows.length; offset += 1) {
    let addedThisRound = false
    for (const categoryId of orderedCategoryIds) {
      const candidate = groupedCandidates.get(categoryId)?.[offset]
      if (!candidate) continue
      interleaved.push({ ...candidate, selected: true })
      addedThisRound = true
    }
    if (!addedThisRound) break
  }
  return { candidates: interleaved, recommendations }
}

export interface UnifiedCandidateScoreInput extends SelectionCandidateRankInput {
  sources?: string[]
  dataMetrics?: Record<string, number | null | undefined>
  trendKnown?: boolean
  trendStatus?: string
  hotOrRankBoost?: number
  businessPriorityBoost?: number
}

const UNIFIED_METRIC_WEIGHTS: Record<string, number> = {
  salesAmount: 0.23,
  salesCount: 0.18,
  conversionRate: 0.18,
  views: 0.09,
  commissionRate: 0.09,
  experienceScore: 0.08,
  trend: 0.15
}

function finiteMetric(value: unknown): number | null {
  const parsed = Number(value)
  return value !== null && value !== undefined && Number.isFinite(parsed) ? parsed : null
}

/** 在给定候选池之间用同一套百分位权重计算分数；正式批次传入全部账号候选池。 */
export function scoreUnifiedCandidatePools<T extends UnifiedCandidateScoreInput>(
  pools: ReadonlyMap<number, readonly T[]>
): Map<number, T[]> {
  const uniqueById = new Map<string, T>()
  for (const rows of pools.values()) {
    for (const candidate of rows) {
      const previous = uniqueById.get(candidate.productId)
      const coverage = (row: T): number => Object.values(row.dataMetrics ?? {}).filter((value) => finiteMetric(value) !== null).length
      if (!previous) {
        uniqueById.set(candidate.productId, candidate)
        continue
      }
      const preferred = coverage(candidate) > coverage(previous) ? candidate : previous
      const secondary = preferred === candidate ? previous : candidate
      const mergedMetrics = { ...(secondary.dataMetrics ?? {}), ...(preferred.dataMetrics ?? {}) }
      const poolRanks = [finiteMetric(previous.poolRank), finiteMetric(candidate.poolRank)]
        .filter((value): value is number => value !== null)
      for (const [key, value] of Object.entries(secondary.dataMetrics ?? {})) {
        if (finiteMetric(mergedMetrics[key]) === null && finiteMetric(value) !== null) mergedMetrics[key] = value
      }
      uniqueById.set(candidate.productId, {
        ...secondary,
        ...preferred,
        dataMetrics: mergedMetrics,
        poolRank: poolRanks.length ? Math.min(...poolRanks) : preferred.poolRank,
        sources: [...new Set([...(previous.sources ?? []), ...(candidate.sources ?? [])])],
        trendKnown: previous.trendKnown === true || candidate.trendKnown === true,
        trendStatus: previous.trendKnown === true ? previous.trendStatus : candidate.trendStatus
      })
    }
  }
  const valuesByMetric = new Map<string, number[]>()
  for (const metric of Object.keys(UNIFIED_METRIC_WEIGHTS).filter((key) => key !== 'trend')) {
    valuesByMetric.set(metric, [...uniqueById.values()]
      .map((candidate) => finiteMetric(candidate.dataMetrics?.[metric]))
      .filter((value): value is number => value !== null))
  }
  const percentile = (value: number, values: number[]): number => {
    if (values.length <= 1 || new Set(values).size <= 1) return 50
    const less = values.filter((item) => item < value).length
    const equal = values.filter((item) => item === value).length
    return ((less + (equal - 1) / 2) / (values.length - 1)) * 100
  }

  const result = new Map<number, T[]>()
  for (const [accountId, rows] of pools) {
    result.set(accountId, rows.map((candidate) => {
      const canonical = uniqueById.get(candidate.productId) ?? candidate
      let weighted = 0
      let weightUsed = 0
      let metricCount = 0
      for (const [metric, weight] of Object.entries(UNIFIED_METRIC_WEIGHTS)) {
        if (metric === 'trend') {
          if (canonical.trendKnown !== true) continue
          const status = String(canonical.trendStatus ?? '')
          weighted += (status === 'rising' ? 100 : status === 'flat' ? 50 : 0) * weight
          weightUsed += weight
          metricCount += 1
          continue
        }
        const value = finiteMetric(canonical.dataMetrics?.[metric])
        if (value === null) continue
        weighted += percentile(value, valuesByMetric.get(metric) ?? []) * weight
        weightUsed += weight
        metricCount += 1
      }
      const coverage = metricCount / Object.keys(UNIFIED_METRIC_WEIGHTS).length
      const raw = weightUsed ? weighted / weightUsed : 0
      const sourceSet = new Set(canonical.sources ?? [])
      const boost = Math.min(10, (sourceSet.has('hotSpot') ? 5 : 0) + (sourceSet.has('allianceRank') ? 5 : 0))
      const poolRank = finiteMetric(canonical.poolRank)
      const poolPriorityIndex = poolRank === null ? null : Math.max(0, Math.trunc(poolRank))
      const poolPriorityBoost = poolPriorityIndex === null ? 0 : Math.max(0, 8 - poolPriorityIndex * 2)
      const commissionRate = finiteMetric(canonical.dataMetrics?.commissionRate)
      const commissionBoost = commissionRate !== null && commissionRate >= 20 ? 4 : 0
      const experienceScore = finiteMetric(canonical.dataMetrics?.experienceScore)
      const experienceBoost = experienceScore !== null && experienceScore >= 4.6 ? 3 : 0
      const businessPriorityBoost = Number((poolPriorityBoost + commissionBoost + experienceBoost).toFixed(1))
      const unifiedScore = Number(Math.min(100, raw * coverage + boost + businessPriorityBoost).toFixed(1))
      const previousReasons = Array.isArray((candidate as T & { analysisReasons?: string[] }).analysisReasons)
        ? (candidate as T & { analysisReasons: string[] }).analysisReasons
          .filter((reason) =>
            !reason.startsWith('候选内综合评分') &&
            !reason.startsWith('跨账号统一评分') &&
            !reason.startsWith('经营建议加权：') &&
            reason !== '热点/榜单来源优先'
          )
        : []
      const unifiedReasons = [
        `跨账号统一评分 ${unifiedScore}/100（统一候选池相对分 ${raw.toFixed(1)}，覆盖 ${metricCount}/${Object.keys(UNIFIED_METRIC_WEIGHTS).length} 项）`,
        ...(boost ? [`热点/联盟参考加权 +${boost}`] : []),
        ...(poolPriorityBoost ? [`经营建议加权：选品池优先级第 ${poolPriorityIndex! + 1} 位 +${poolPriorityBoost}`] : []),
        ...(commissionBoost ? [`经营建议加权：佣金率 ${commissionRate}% 达到推荐线 20%，+${commissionBoost}`] : []),
        ...(experienceBoost ? [`经营建议加权：商家体验分 ${experienceScore} 达到推荐线 4.6，+${experienceBoost}`] : []),
        ...previousReasons
      ]
      return {
        ...candidate,
        sources: [...sourceSet] as T['sources'],
        hotOrRankBoost: boost,
        businessPriorityBoost,
        unifiedScore,
        analysisReasons: unifiedReasons
      }
    }))
  }
  return result
}

function stableRandomUnit(value: string): number {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return ((hash >>> 0) + 1) / 4294967297
}

/**
 * 按批次 ID 为已通过硬筛选的候选生成稳定加权随机顺序。
 * 分数提高抽中概率，但不会让每轮都固定从最高分商品开始。
 */
export function rankCandidatesForBatch<T extends UnifiedCandidateScoreInput>(
  rows: readonly T[],
  batchSeed: string
): T[] {
  const seed = batchSeed.trim() || 'selection-batch'
  return rows.map((candidate) => {
    const score = finiteMetric(candidate.unifiedScore) ?? finiteMetric(candidate.analysisScore) ?? 0
    const normalizedScore = Math.max(0, Math.min(100, score))
    const weight = 0.75 + normalizedScore / 100 * 1.5
    const random = stableRandomUnit(`${seed}\u0000${candidate.productId}`)
    return { ...candidate, batchRandomKey: -Math.log(random) / weight }
  }).sort((left, right) =>
    (finiteMetric(left.batchRandomKey) ?? Number.POSITIVE_INFINITY) -
      (finiteMetric(right.batchRandomKey) ?? Number.POSITIVE_INFINITY) ||
    left.productId.localeCompare(right.productId)
  )
}

export interface ExclusiveCandidatePool<T extends UnifiedCandidateScoreInput> {
  accountId: number
  accountName: string
  capacity: number
  candidates: T[]
}

export interface ExclusiveCandidateAllocation<T extends UnifiedCandidateScoreInput> {
  candidates: T[]
  skipped: Array<T & { mutualExclusionReason: string; mutualExclusionWinnerAccountId: number }>
}

/** 全批次按批次随机权重分配；同一商品只分给一个账号。未指定随机顺序时兼容原统一分排序。 */
export function allocateExclusiveCandidates<T extends UnifiedCandidateScoreInput>(
  pools: readonly ExclusiveCandidatePool<T>[]
): Map<number, ExclusiveCandidateAllocation<T>> {
  const allocations = new Map<number, ExclusiveCandidateAllocation<T>>()
  const accountOrder = new Map([...pools].sort((left, right) => left.accountId - right.accountId)
    .map((pool, index) => [pool.accountId, index]))
  for (const pool of pools) allocations.set(pool.accountId, { candidates: [], skipped: [] })
  const pairs = pools.flatMap((pool) => pool.candidates.map((candidate, candidateIndex) => ({
    pool,
    candidate,
    candidateIndex,
    randomKey: finiteMetric(candidate.batchRandomKey),
    score: finiteMetric(candidate.unifiedScore) ?? finiteMetric(candidate.analysisScore) ?? 0
  }))).sort((left, right) => {
    const candidateOrder = left.randomKey !== null && right.randomKey !== null
      ? left.randomKey - right.randomKey
      : right.score - left.score
    return candidateOrder ||
      (accountOrder.get(left.pool.accountId) ?? 0) - (accountOrder.get(right.pool.accountId) ?? 0) ||
      left.candidateIndex - right.candidateIndex ||
      left.candidate.productId.localeCompare(right.candidate.productId)
  })
  // 商品 ID 来自页面文本，偶尔会带空白；批次互斥必须按规范化 ID 判断，
  // 否则同一个商品可能以两个字符串形式分配给不同账号。
  const winnerByProduct = new Map<string, { accountId: number; accountName: string; score: number }>()
  const countByAccount = new Map<number, number>()
  for (const pair of pairs) {
    const { pool, candidate, score } = pair
    const productKey = String(candidate.productId ?? '').trim()
    if (!productKey) continue
    const existing = winnerByProduct.get(productKey)
    if (existing) {
      if (existing.accountId !== pool.accountId) {
        allocations.get(pool.accountId)?.skipped.push({
          ...candidate,
          mutualExclusionWinnerAccountId: existing.accountId,
          mutualExclusionReason: `商品 ID 已分配给账号「${existing.accountName}」（统一分 ${existing.score.toFixed(1)}），本账号统一分 ${score.toFixed(1)}，已跳过`
        })
      }
      continue
    }
    if ((countByAccount.get(pool.accountId) ?? 0) >= Math.max(0, pool.capacity)) continue
    winnerByProduct.set(productKey, { accountId: pool.accountId, accountName: pool.accountName, score })
    countByAccount.set(pool.accountId, (countByAccount.get(pool.accountId) ?? 0) + 1)
    allocations.get(pool.accountId)?.candidates.push({ ...candidate, selected: true })
  }
  for (const allocation of allocations.values()) {
    allocation.candidates.sort((left, right) => {
      const leftRandomKey = finiteMetric(left.batchRandomKey)
      const rightRandomKey = finiteMetric(right.batchRandomKey)
      const candidateOrder = leftRandomKey !== null && rightRandomKey !== null
        ? leftRandomKey - rightRandomKey
        : (finiteMetric(right.unifiedScore) ?? 0) - (finiteMetric(left.unifiedScore) ?? 0)
      return candidateOrder || left.productId.localeCompare(right.productId)
    })
  }
  return allocations
}
