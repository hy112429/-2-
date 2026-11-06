import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/renderer/src/utils/selection_candidates.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(moduleValue.exports, () => undefined, moduleValue)
const { filterCandidatesByRegistry, rankByRecommendedCategories } = moduleValue.exports

const candidate = (productId, categoryId, analysisScore, poolRank = 1) => ({
  productId, leafCategoryId: categoryId, analysisScore, poolRank, selected: false
})

test('类目推荐先比较高分商品数量，再比较平均分，并在候选序列中轮转类目', () => {
  const rows = [
    candidate('x-1', 'cat-x', 88, 1),
    candidate('x-2', 'cat-x', 72, 2),
    candidate('y-1', 'cat-y', 95, 3),
    candidate('z-1', 'cat-z', 59, 4)
  ]
  const result = rankByRecommendedCategories(rows, {}, 2)

  assert.deepEqual(result.recommendations, [
    { categoryId: 'cat-x', highScoreCount: 2, averageScore: 80 },
    { categoryId: 'cat-y', highScoreCount: 1, averageScore: 95 }
  ])
  assert.deepEqual(result.candidates.map((row) => row.productId), ['x-1', 'y-1', 'z-1', 'x-2'])
  assert.ok(result.candidates.every((row) => row.selected === true))
})

test('无 60 分候选时按 A-67 分数取前 N 个，并用已用较少的类目打破同分', () => {
  const rows = [
    candidate('g-1', 'cat-g', 55, 1),
    candidate('h-1', 'cat-h', 55, 2),
    candidate('outside-n', 'cat-g', 40, 3)
  ]
  const result = rankByRecommendedCategories(rows, { 'cat-g': 8, 'cat-h': 1 }, 2)

  assert.deepEqual(result.recommendations, [
    { categoryId: 'cat-h', highScoreCount: 1, averageScore: 55 },
    { categoryId: 'cat-g', highScoreCount: 1, averageScore: 55 }
  ])
})

test('登记过滤强制排除全局重复商品、账号历史类目和缺少类目 ID 的候选', () => {
  const rows = [
    candidate('global-used', 'cat-new', 90),
    candidate('account-category-used', 'cat-old', 85),
    { ...candidate('missing-category', '', 80), leafCategoryId: '' },
    candidate('available', 'cat-new-2', 70)
  ]
  const result = filterCandidatesByRegistry(rows, {
    usedProductIds: ['global-used'],
    accountUsedCategoryIds: ['cat-old'],
    categoryUsage: {}
  })

  assert.deepEqual(result.eligible.map((row) => row.productId), ['available'])
  assert.deepEqual(result.excluded.map(({ candidate: row, reason }) => [row.productId, reason]), [
    ['global-used', '商品 ID 已在全账号池登记'],
    ['account-category-used', '该账号历史或本轮已使用此末级类目'],
    ['missing-category', '品类模式下来源数据缺少三级类目 ID']
  ])
})
