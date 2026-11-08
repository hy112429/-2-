import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { delimiter, join } from 'node:path'

const stagingEngine = 'D:/software/tg_pkg_staging/engine'
const runtime = join(stagingEngine, 'runtime')
const sitePackages = join(stagingEngine, '.venv', 'Lib', 'site-packages')
const python = join(runtime, 'python.exe')

const requiredFiles = [
  python,
  join(sitePackages, 'playwright', 'sync_api', '__init__.py'),
  join(sitePackages, 'playwright', 'driver', 'node.exe'),
  join(sitePackages, 'requests', '__init__.py'),
  join(sitePackages, 'jieba', '__init__.py'),
  join(sitePackages, 'faster_whisper', '__init__.py'),
  join(sitePackages, 'ctranslate2', '__init__.py'),
  join(sitePackages, 'av', '__init__.py'),
  join(sitePackages, 'huggingface_hub', '__init__.py'),
  join(sitePackages, 'tokenizers', '__init__.py'),
  join(sitePackages, 'onnxruntime', '__init__.py'),
  join(sitePackages, 'numpy', '__init__.py'),
  join(sitePackages, 'cv2', '__init__.py'),
  join(sitePackages, 'rapidocr', '__init__.py'),
  join(sitePackages, 'rapidocr', 'models', 'PP-OCRv6_det_small.onnx'),
  join(sitePackages, 'rapidocr', 'models', 'ch_ppocr_mobile_v2.0_cls_mobile.onnx'),
  join(sitePackages, 'rapidocr', 'models', 'PP-OCRv6_rec_small.onnx'),
  join(stagingEngine, 'bin', 'ffmpeg.exe'),
  join(stagingEngine, 'bin', 'ffprobe.exe')
]
const missingFiles = requiredFiles.filter((file) => !existsSync(file))
if (missingFiles.length) {
  console.error('Python 打包环境缺少必需文件：')
  for (const file of missingFiles) console.error(`- ${file}`)
  process.exit(1)
}

const env = {
  ...process.env,
  PYTHONHOME: runtime,
  PYTHONPATH: [sitePackages, process.env['PYTHONPATH']].filter(Boolean).join(delimiter)
}
const probe = spawnSync(python, [
  '-c',
  'from playwright.sync_api import sync_playwright; import av, cv2, ctranslate2, faster_whisper, huggingface_hub, jieba, numpy, onnxruntime, rapidocr, requests, tokenizers; print("Python package imports OK")'
], { env, encoding: 'utf8', windowsHide: true })

if (probe.status !== 0) {
  if (probe.stdout) process.stderr.write(probe.stdout)
  if (probe.stderr) process.stderr.write(probe.stderr)
  process.exit(probe.status ?? 1)
}

if (probe.stdout) process.stdout.write(probe.stdout)
console.log('Python runtime, site packages, Playwright driver, FFmpeg, and FFprobe are ready for packaging.')
