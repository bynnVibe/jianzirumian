/**
 * 图片处理工具
 * - 压缩至指定大小以内（默认 5MB）
 * - 辅助裁剪后导出
 */

export const MAX_UPLOAD_SIZE = 5 * 1024 * 1024 // 5MB

/**
 * 读取 File/Blob 为 DataURL
 */
export function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = (e) => resolve(e.target.result)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

/**
 * 将 DataURL 转回 File 对象
 */
export function dataURLToFile(dataURL, filename, lastModified = Date.now()) {
  const arr = dataURL.split(',')
  const mime = arr[0].match(/:(.*?);/)[1]
  const bstr = atob(arr[1])
  let n = bstr.length
  const u8arr = new Uint8Array(n)
  while (n--) {
    u8arr[n] = bstr.charCodeAt(n)
  }
  return new File([u8arr], filename, { type: mime, lastModified })
}

/**
 * 使用 canvas 压缩图片至目标大小以内（优先保证清晰度，服务于 OCR 识别）
 * 策略：PNG 无损优先 → 保持分辨率降 JPEG 质量 → 实在超限才缩尺寸
 * @param {HTMLCanvasElement} canvas 待压缩的 canvas
 * @param {string} filename 文件名
 * @param {number} maxBytes 最大字节数
 * @param {number} maxDimension 最长边限制
 * @returns {Promise<File>}
 */
export async function compressCanvasToFile(
  canvas,
  filename,
  maxBytes = MAX_UPLOAD_SIZE,
  maxDimension = 4096
) {
  let { width, height } = canvas

  // 第一步：等比缩放到合理尺寸（避免巨大 canvas 导致的内存问题）
  if (width > maxDimension || height > maxDimension) {
    const ratio = Math.min(maxDimension / width, maxDimension / height)
    width = Math.floor(width * ratio)
    height = Math.floor(height * ratio)
  }

  const scaledCanvas = document.createElement('canvas')
  scaledCanvas.width = width
  scaledCanvas.height = height
  const ctx = scaledCanvas.getContext('2d')
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(canvas, 0, 0, width, height)

  // 第二步：优先 PNG 无损导出（文字/手写笔记类图像效果最佳）
  const pngDataURL = scaledCanvas.toDataURL('image/png')
  if (byteLength(pngDataURL) <= maxBytes) {
    return dataURLToFile(pngDataURL, filename)
  }

  // 第三步：保持分辨率，二分法寻找满足大小的最高 JPEG 质量
  const findQuality = (cv) => {
    let lo = 0.4
    let hi = 0.95
    let best = null
    for (let i = 0; i < 8; i++) {
      const mid = (lo + hi) / 2
      const url = cv.toDataURL('image/jpeg', mid)
      if (byteLength(url) > maxBytes) {
        hi = mid
      } else {
        lo = mid
        best = url
      }
    }
    return best
  }

  let bestDataURL = findQuality(scaledCanvas)

  // 第四步：实在超限才逐步缩小尺寸（下限提高到 1600，尽量保留 OCR 所需分辨率）
  while (!bestDataURL && (width > 1600 || height > 1600)) {
    width = Math.floor(width * 0.9)
    height = Math.floor(height * 0.9)
    scaledCanvas.width = width
    scaledCanvas.height = height
    ctx.drawImage(canvas, 0, 0, width, height)
    bestDataURL = findQuality(scaledCanvas)
  }

  if (bestDataURL) {
    return dataURLToFile(bestDataURL, filename)
  }

  // 兜底：极端情况下直接用低质量导出
  return dataURLToFile(scaledCanvas.toDataURL('image/jpeg', 0.4), filename)
}

/**
 * 直接压缩一个 File
 * @param {File} file
 * @param {number} maxBytes
 * @returns {Promise<File>}
 */
export async function compressImageFile(file, maxBytes = MAX_UPLOAD_SIZE) {
  if (file.size <= maxBytes && file.type !== 'image/bmp') {
    return file
  }
  const dataURL = await readFileAsDataURL(file)
  const img = await loadImage(dataURL)
  const canvas = document.createElement('canvas')
  canvas.width = img.width
  canvas.height = img.height
  const ctx = canvas.getContext('2d')
  ctx.drawImage(img, 0, 0)
  return compressCanvasToFile(canvas, file.name, maxBytes)
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = reject
    img.src = src
  })
}

/**
 * 将带 EXIF 方向信息的图片烤正为正常方向（DataURL）。
 * 浏览器 <img> 会自动按 EXIF 展示，但 cropper 等基于 canvas 的操作仍会按原始像素处理，
 * 导致旋转/裁剪出现“额外角度”。先用 createImageBitmap({imageOrientation:'from-image'}) 修正。
 */
export async function normalizeImageOrientation(file) {
  if (!file || !file.type.startsWith('image/')) {
    throw new Error('非图片文件')
  }

  // 优先使用浏览器原生 API 烤正方向
  if ('createImageBitmap' in window) {
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
      const canvas = document.createElement('canvas')
      canvas.width = bitmap.width
      canvas.height = bitmap.height
      const ctx = canvas.getContext('2d')
      ctx.drawImage(bitmap, 0, 0)
      return canvas.toDataURL(file.type, 0.95)
    } catch {
      // fallback 到原始 dataURL
    }
  }

  // 兜底：直接返回原图 dataURL
  return readFileAsDataURL(file)
}

function byteLength(dataURL) {
  // DataURL 中 base64 部分的字节数（粗略，略小于实际 Blob 大小，但足够用于判断）
  const base64 = dataURL.split(',')[1]
  return Math.floor((base64.length * 3) / 4)
}
