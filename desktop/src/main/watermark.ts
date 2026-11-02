import { ipcMain, protocol, net } from 'electron'
import { execFile } from 'child_process'
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { getEngineDir } from './engine'

/**
 * 去水印 UI 辅助（Step 11 补充）
 * ─────────────────────────────────────────────
 * 1. watermark:getFrame      —— 抽视频首帧（旧版）
 * 2. watermark:getFrames     —— 抽 N 帧落盘为 JPEG，返回本地路径（半自动标注轨迹用）
 * 3. watermark:saveTemplate  —— 把框选区域 PNG dataURL 存为临时模板文件（旧版）
 *
 * 帧的展示不走「base64 dataURL → Image」链路（此前该链路在 IPC 传大字符串后
 * Image 解码失败，多轮未解），改为：
 *   - 主进程把抽帧 JPEG 写本地临时文件；
 *   - 注册自定义协议 `frame://` 供渲染进程 <img> 直接加载本地文件；
 *   彻底绕开 IPC 大字符串与 dataURL 解码，路径驱动，可靠。
 *
 * 实际去水印执行走引擎通道（remove_watermark.py --points，已在白名单）。
 */

/** 半自动去水印标注用的一帧：时间/帧号/分辨率/本地 JPEG 路径 */
export interface WmFrame {
  t: number
  frame: number
  w: number
  h: number
  /** 本地 JPEG 绝对路径（渲染进程经 frame:// 协议加载） */
  path: string
}

const FRAME_DIR = join(tmpdir(), 'wm_frames')

/** 安装版优先使用随包 ffmpeg/ffprobe，开发态或缺少随包文件时回退到 PATH。 */
function resolveMediaTool(name: 'ffmpeg' | 'ffprobe'): string {
  const executable = process.platform === 'win32' ? `${name}.exe` : name
  const bundled = join(getEngineDir(), 'bin', executable)
  return existsSync(bundled) ? bundled : name
}

/** 解析 ffprobe 输出（如 "25/1" → 25） */
function parseRate(value: string): number {
  const [a, b] = value.split('/')
  const num = Number(a)
  const den = Number(b)
  if (!Number.isFinite(num)) return 25
  if (!Number.isFinite(den) || den === 0) return num
  return num / den
}

function runFfprobe(args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(resolveMediaTool('ffprobe'), args, { windowsHide: true }, (err, stdout) => {
      if (err) reject(new Error(err.message ?? 'ffprobe 失败'))
      else resolve(stdout.trim())
    })
  })
}

function runFfmpegFrame(ts: number, videoPath: string): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    execFile(
      resolveMediaTool('ffmpeg'),
      [
        '-y', '-loglevel', 'error',
        '-ss', String(ts), '-i', videoPath,
        // 缩到 640 宽 + JPEG 压缩：IPC 传 base64 时负载比原 PNG 小 50 倍
        // （原 720x960 PNG 单帧 ~5MB → JPEG ~100KB），避免大字符串解码失败
        '-vf', 'scale=640:-1',
        '-frames:v', '1',
        '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', '-'
      ],
      { maxBuffer: 24 * 1024 * 1024, windowsHide: true },
      (err, stdout) => {
        if (err || stdout.length === 0) reject(new Error(err?.message ?? '抽帧无输出'))
        else resolve(Buffer.isBuffer(stdout) ? stdout : Buffer.from(stdout, 'utf8'))
      }
    )
  })
}

/**
 * 注册 `frame://` 协议：渲染进程 <img src="frame:///<绝对路径>"> 直接加载本地帧图。
 * 必须配合 index.ts 顶层的 registerSchemesAsPrivileged 使用（ready 前注册 scheme）。
 */
export function registerFrameProtocol(): void {
  protocol.handle('frame', (req) => {
    try {
      const url = new URL(req.url)
      // frame:///C:\Users\... → pathname 去掉前导 / 得绝对路径
      const raw = url.pathname.replace(/^\//, '')
      const filePath = decodeURIComponent(raw)
      return net.fetch('file:///' + filePath.replace(/\\/g, '/'))
    } catch {
      return new Response('not found', { status: 404 })
    }
  })
}

export function registerWatermarkIpc(): void {
  // 抽首帧：ffmpeg 输出 PNG 到 stdout，转 dataURL
  ipcMain.handle(
    'watermark:getFrame',
    (_event, videoPath: string): Promise<{ ok: boolean; dataUrl?: string; error?: string }> => {
      return new Promise((resolve) => {
        execFile(
          resolveMediaTool('ffmpeg'),
          ['-y', '-loglevel', 'error', '-ss', '0', '-i', videoPath, '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
          { maxBuffer: 24 * 1024 * 1024, windowsHide: true },
          (err, stdout) => {
            if (err || stdout.length === 0) {
              resolve({ ok: false, error: `抽帧失败：${err?.message ?? '无输出'}` })
            } else {
              const buf = Buffer.isBuffer(stdout) ? stdout : Buffer.from(stdout, 'utf8')
              resolve({ ok: true, dataUrl: `data:image/png;base64,${buf.toString('base64')}` })
            }
          }
        )
      })
    }
  )

  // 抽 N 个时间点的帧（半自动标注水印轨迹）：返回每帧 t/帧号/分辨率/PNG dataURL
  ipcMain.handle(
    'watermark:getFrames',
    async (_event, videoPath: string, n: number): Promise<{ ok: boolean; frames?: WmFrame[]; error?: string }> => {
      try {
        const count = Math.min(Math.max(Number(n) || 5, 2), 8)
        const durS = Number(await runFfprobe(['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', videoPath]))
        const fps = parseRate(await runFfprobe(['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=r_frame_rate', '-of', 'csv=p=0', videoPath]))
        const [w, h] = (await runFfprobe(['-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', videoPath])).split(',')

        const frames: WmFrame[] = []
        // 每次清空旧帧文件，只保留本次
        rmSync(FRAME_DIR, { recursive: true, force: true })
        mkdirSync(FRAME_DIR, { recursive: true })
        const stamp = Date.now()
        for (let i = 0; i < count; i++) {
          // 在 (0, 时长) 内均匀取 count 个时间点，避开端点
          const t = durS > 0 ? Math.max(0, durS * ((i + 1) / (count + 1))) : i * 1
          const buf = await runFfmpegFrame(t, videoPath)
          const file = join(FRAME_DIR, `f_${stamp}_${i}.jpg`)
          writeFileSync(file, buf)
          frames.push({
            t: Math.round(t * 10) / 10,
            frame: Math.round(t * fps),
            w: Number(w) || 0,
            h: Number(h) || 0,
            path: file
          })
        }
        return { ok: true, frames }
      } catch (error) {
        return { ok: false, error: String((error as Error).message ?? error) }
      }
    }
  )

  // 保存模板：PNG dataURL → 临时文件，返回路径供 remove_watermark.py --template 使用
  ipcMain.handle(
    'watermark:saveTemplate',
    (_event, dataUrl: string): { ok: boolean; path?: string; error?: string } => {
      try {
        const match = /^data:image\/png;base64,(.+)$/.exec(dataUrl ?? '')
        if (!match) return { ok: false, error: '模板图片格式不正确（应为 PNG dataURL）' }
        const file = join(tmpdir(), `wm_tmpl_${Date.now()}.png`)
        writeFileSync(file, Buffer.from(match[1], 'base64'))
        return { ok: true, path: file }
      } catch (error) {
        return { ok: false, error: String(error) }
      }
    }
  )
}
