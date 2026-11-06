import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/main/probe.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const saved = []
const mocks = {
  fs: { appendFileSync: () => undefined },
  os: { tmpdir: () => 'D:\\software\\codex' },
  './engine': {
    runScript: (_script, options) => {
      saved.push(options.stdinJson)
      return { promise: Promise.resolve({ ok: true }) }
    }
  }
}
const moduleValue = { exports: {} }
new Function('exports', 'require', 'module', compiled)(moduleValue.exports, (name) => mocks[name] ?? require(name), moduleValue)
const { captureExternalBaiyingResponse, onProductCaptured } = moduleValue.exports

function response(productId, media) {
  return JSON.stringify({
    data: {
      product_id: productId,
      model: { product: { product_base: {
        title: '测试商品', big_imgs: [{ url: 'https://img.example/cover.jpg' }], media
      } } }
    }
  })
}

test('商品详情零视频也立即通知查询，不把商品图片误当视频', () => {
  const captured = []
  const off = onProductCaptured((item) => captured.push(item))
  try {
    captureExternalBaiyingResponse(
      'https://buyin.jinritemai.com/pc/selection/decision/pack_detail',
      response('3827855045362319495', { cover_url: 'https://img.example/cover.jpg' }), 1
    )
    assert.deepEqual(captured, [{ productId: '3827855045362319495', videoUrls: [], accountId: 1 }])
    assert.deepEqual(saved.at(-1).videoUrls, [])
  } finally { off() }
})

test('同一详情中的多条真实视频直链都交给缓存和查询', () => {
  const captured = []
  const off = onProductCaptured((item) => captured.push(item))
  try {
    captureExternalBaiyingResponse(
      'https://buyin.jinritemai.com/pc/selection/decision/pack_detail',
      response('3827855045362319496', { videos: [
        { play_url: 'https://video.example/one.mp4' },
        { play_url: 'https://video.example/two.mp4' }
      ] }), 2
    )
    assert.deepEqual(captured[0].videoUrls, [
      'https://video.example/one.mp4', 'https://video.example/two.mp4'
    ])
    assert.deepEqual(saved.at(-1).videoUrls, captured[0].videoUrls)
  } finally { off() }
})
