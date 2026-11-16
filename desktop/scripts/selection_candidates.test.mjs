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
const { candidateLeafCategoryId, filterCandidatesByRegistry, rankByRecommendedCategories } = moduleValue.exports

const candidate = (productId, categoryId, analysisScore, poolRank = 1) => ({
  productId, leafCategoryId: categoryId, categoryIds: categoryId ? ['1', '201', categoryId] : [],
  analysisScore, poolRank, selected: false
})

test('类目推荐先比较高分商品数量，再比较平均分，并在候选序列中轮转类目', () => {
  const rows = [
    candidate('x-1', '301', 88, 1),
    candidate('x-2', '301', 72, 2),
    candidate('y-1', '302', 95, 3),
    candidate('z-1', '303', 59, 4)
  ]
  const result = rankByRecommendedCategories(rows, {}, 2)

  assert.deepEqual(result.recommendations, [
    { categoryId: '301', highScoreCount: 2, averageScore: 80 },
    { categoryId: '302', highScoreCount: 1, averageScore: 95 }
  ])
  assert.deepEqual(result.candidates.map((row) => row.productId), ['x-1', 'y-1', 'z-1', 'x-2'])
  assert.ok(result.candidates.every((row) => row.selected === true))
})

test('无 60 分候选时按 A-67 分数取前 N 个，并用已用较少的类目打破同分', () => {
  const rows = [
    candidate('g-1', '304', 55, 1),
    candidate('h-1', '305', 55, 2),
    candidate('outside-n', '304', 40, 3)
  ]
  const result = rankByRecommendedCategories(rows, { '304': 8, '305': 1 }, 2)

  assert.deepEqual(result.recommendations, [
    { categoryId: '305', highScoreCount: 1, averageScore: 55 },
    { categoryId: '304', highScoreCount: 1, averageScore: 55 }
  ])
})

test('登记过滤强制排除全局重复商品、账号历史类目和缺少类目 ID 的候选', () => {
  const rows = [
    candidate('global-used', '306', 90),
    candidate('account-category-used', '307', 85),
    { ...candidate('missing-category', '', 80), leafCategoryId: '' },
    candidate('available', '308', 70)
  ]
  const result = filterCandidatesByRegistry(rows, {
    usedProductIds: ['global-used'],
    accountUsedCategoryIds: ['307'],
    categoryUsage: {}
  })

  assert.deepEqual(result.eligible.map((row) => row.productId), ['available'])
  assert.deepEqual(result.excluded.map(({ candidate: row, reason }) => [row.productId, reason]), [
    ['global-used', '商品 ID 已在全账号池登记'],
    ['account-category-used', '该账号历史或本轮已使用此末级类目'],
    ['missing-category', '品类模式下来源数据缺少三级类目 ID']
  ])
})

test('部分、零值和冲突类目路径不能冒充三级末级类目', () => {
  assert.equal(candidateLeafCategoryId({ productId: 'a', categoryIds: ['1', '201'] }), '')
  assert.equal(candidateLeafCategoryId({ productId: 'b', categoryIds: ['1', '201', '0'] }), '')
  assert.equal(candidateLeafCategoryId({ productId: 'c', categoryIds: ['1', '201', '301'], leafCategoryId: '302' }), '')
  assert.equal(candidateLeafCategoryId({ productId: 'd', categoryIds: ['1', '201', '301'], categoryStatus: 'ambiguous' }), '')
  assert.equal(candidateLeafCategoryId({ productId: 'e', categoryIds: ['1', '201', '301'] }), '301')
})
