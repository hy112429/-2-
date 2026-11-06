import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse as parseSfc } from '@vue/compiler-sfc'
import { parse } from '@vue/compiler-dom'

const root = fileURLToPath(new URL('../src/renderer/src/components/', import.meta.url))
const files = readdirSync(root).filter((name) => name.endsWith('.vue'))
const problems = []
const details = []

function hasAttr(node, name, value) {
  return node.props.some((prop) => prop.type === 6 && prop.name === name && (value === undefined || prop.value?.content === value))
}

function hasEvent(node, name) {
  return node.props.some((prop) => prop.type === 7 && prop.name === 'on' && prop.arg?.type === 4 && prop.arg.content === name)
}

function clickIsBackdrop(node) {
  return node.props.some((prop) => prop.type === 7 && prop.name === 'on' && prop.arg?.content === 'click' && prop.modifiers.some((modifier) => modifier.content === 'self'))
}

function visit(node, file, counts, lineOffset, hasWatermarkAlternative) {
  if (node.type === 1) {
    const line = node.loc.start.line + lineOffset
    if (node.tag === 'button') {
      counts.buttons++
      if (!hasEvent(node, 'click') && !hasAttr(node, 'type', 'submit') && !hasAttr(node, 'disabled')) {
        problems.push(`${file}:${line} 按钮没有点击事件或表单提交行为`)
      }
    }
    if (['div', 'span', 'img', 'td', 'li'].includes(node.tag) && hasEvent(node, 'click') && !clickIsBackdrop(node)) {
      counts.customClicks++
      if (node.tag === 'img') {
        if (hasAttr(node, 'class', 'wm-frame-img') && hasWatermarkAlternative) counts.pointerAlternatives++
        else details.push(`${file}:${line} 图像坐标操作需要键盘替代操作`)
      } else if (!hasAttr(node, 'role', 'button') || !hasAttr(node, 'tabindex', '0') || !hasEvent(node, 'keydown')) {
        details.push(`${file}:${line} 非原生可点击元素需要检查键盘操作`)
      }
    }
  }
  for (const child of node.children || []) visit(child, file, counts, lineOffset, hasWatermarkAlternative)
}

for (const file of files) {
  const source = readFileSync(join(root, file), 'utf8')
  const { descriptor, errors } = parseSfc(source, { filename: file })
  if (errors.length || !descriptor.template) {
    problems.push(`${file} 模板解析失败或不存在`)
    continue
  }
  const counts = { buttons: 0, customClicks: 0, pointerAlternatives: 0 }
  visit(parse(descriptor.template.content), file, counts, descriptor.template.loc.start.line - 1, source.includes('@click="autoDetectWatermark"'))
  console.log(`${file}: ${counts.buttons} 个按钮，${counts.customClicks} 个自定义点击区域${counts.pointerAlternatives ? `（${counts.pointerAlternatives} 个有自动操作替代）` : ''}`)
}

for (const note of details) console.warn(`检查：${note}`)
for (const issue of problems) console.error(`错误：${issue}`)
console.log(`界面静态交互检查：${files.length} 页，${problems.length} 个未绑定按钮，${details.length} 个需检查的自定义点击区域`)
if (problems.length) process.exitCode = 1
