/**
 * 图片清晰度检测工具（服务于知识库上传 OCR 前置校验）
 *
 * 算法：Laplacian variance（拉普拉斯方差）
 *   createObjectURL 加载图片 → canvas 等比缩放到最长边 512 → 转灰度
 *   → 3x3 拉普拉斯卷积（4 邻域）→ 计算响应方差作为清晰度分数。
 *   方差越大说明高频细节（边缘/笔画）越丰富，图片越清晰；反之则模糊。
 *
 * 阈值标定：用 Python(PIL+numpy) 对真实上传样本复现同算法标定得到，
 *   清晰样本方差均 > 1300，重模糊（高斯 sigma=10）样本方差均 < 100，
 *   故取 100 作为"很模糊"判定阈值（详见 .dev-logs/calibrate_blur.py）。
 */

// 缩放后参与计算的最长边
const MAX_SIDE = 512

/**
 * 模糊判定阈值：清晰度分数（拉普拉斯方差）低于此值判定为模糊。
 * 经真实样本 + 高斯模糊(sigma=3/6/10)标定得到。
 * @type {number}
 */
export const BLUR_VARIANCE_THRESHOLD = 100

// 降级跳过检测的 MIME：SVG（矢量，无像素模糊概念）、GIF（可能是动图）
const SKIP_MIME_PREFIXES = ['image/svg', 'image/gif']

/**
 * 分析图片清晰度
 * @param {File|Blob} fileOrBlob 待检测的图片文件
 * @returns {Promise<{score: number, blurry: boolean, width: number, height: number}>}
 *   - score：清晰度分数（拉普拉斯方差），降级/失败时为 Infinity
 *   - blurry：是否判定为模糊；加载失败或降级跳过时恒为 false（不阻断上传）
 *   - width / height：缩放后参与计算的画布尺寸（原始尺寸降级时为 0）
 */
export function analyzeImageBlur(fileOrBlob) {
  return new Promise((resolve) => {
    // 非图片直接放行
    const type = (fileOrBlob && fileOrBlob.type) || ''
    if (!type.startsWith('image/')) {
      resolve({ score: Infinity, blurry: false, width: 0, height: 0 })
      return
    }
    // SVG / 动图降级：跳过检测，不阻断上传
    if (SKIP_MIME_PREFIXES.some((p) => type.startsWith(p))) {
      resolve({ score: Infinity, blurry: false, width: 0, height: 0 })
      return
    }

    const url = URL.createObjectURL(fileOrBlob)
    const img = new Image()

    const cleanup = () => {
      URL.revokeObjectURL(url)
    }

    img.onload = () => {
      try {
        const result = computeLaplacianVariance(img)
        cleanup()
        resolve(result)
      } catch (err) {
        // 计算异常（如 canvas 读取受限）：降级放行
        console.warn('[imageQuality] 清晰度计算失败，已降级放行:', err)
        cleanup()
        resolve({ score: Infinity, blurry: false, width: 0, height: 0 })
      }
    }
    img.onerror = () => {
      // 加载失败：降级放行，不阻断上传
      cleanup()
      resolve({ score: Infinity, blurry: false, width: 0, height: 0 })
    }
    img.src = url
  })
}

/**
 * 在 canvas 上完成缩放 → 灰度 → 3x3 拉普拉斯卷积 → 方差计算
 * @param {HTMLImageElement} img 已加载的图片
 * @returns {{score: number, blurry: boolean, width: number, height: number}}
 */
function computeLaplacianVariance(img) {
  const nw = img.naturalWidth || img.width
  const nh = img.naturalHeight || img.height
  if (!nw || !nh) {
    return { score: Infinity, blurry: false, width: 0, height: 0 }
  }

  // 等比缩放到最长边 MAX_SIDE（不放大，小图保持原尺寸）
  const ratio = Math.max(nw, nh) > MAX_SIDE ? MAX_SIDE / Math.max(nw, nh) : 1
  const w = Math.max(1, Math.round(nw * ratio))
  const h = Math.max(1, Math.round(nh * ratio))

  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  if (!ctx) {
    return { score: Infinity, blurry: false, width: 0, height: 0 }
  }
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(img, 0, 0, w, h)

  const { data } = ctx.getImageData(0, 0, w, h)

  // 转灰度（ITU-R BT.601 加权，与 PIL convert('L') 一致）
  const gray = new Float64Array(w * h)
  for (let i = 0, p = 0; i < data.length; i += 4, p++) {
    gray[p] = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]
  }

  // 3x3 拉普拉斯卷积（4 邻域）：kernel = [[0,1,0],[1,-4,1],[0,1,0]]
  // 仅在内部区域 (1..w-2, 1..h-2) 计算，避免边界越界
  const iw = w - 2
  const ih = h - 2
  if (iw <= 0 || ih <= 0) {
    return { score: Infinity, blurry: false, width: w, height: h }
  }
  const n = iw * ih
  let sum = 0
  let sumSq = 0
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const c = gray[y * w + x]
      const lap =
        4 * c -
        gray[(y - 1) * w + x] -
        gray[(y + 1) * w + x] -
        gray[y * w + (x - 1)] -
        gray[y * w + (x + 1)]
      sum += lap
      sumSq += lap * lap
    }
  }
  const mean = sum / n
  const variance = sumSq / n - mean * mean

  return {
    score: variance,
    blurry: variance < BLUR_VARIANCE_THRESHOLD,
    width: w,
    height: h,
  }
}
