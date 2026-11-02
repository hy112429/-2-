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
  join(sitePackages, 'numpy', '__init__.py'),
  join(sitePackages, 'cv2', '__init__.py'),
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
  'from playwright.sync_api import sync_playwright; import cv2, numpy, requests; print("Python package imports OK")'
], { env, encoding: 'utf8', windowsHide: true })

if (probe.status !== 0) {
  if (probe.stdout) process.stderr.write(probe.stdout)
  if (probe.stderr) process.stderr.write(probe.stderr)
  process.exit(probe.status ?? 1)
}

if (probe.stdout) process.stdout.write(probe.stdout)
console.log('Python runtime, site packages, Playwright driver, FFmpeg, and FFprobe are ready for packaging.')
