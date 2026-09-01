<template>
  <div class="upload-view">
    <!-- 头部 + 模块切换 -->
    <div class="page-header">
      <div class="page-title-row">
        <h1 class="page-title">知识库上传</h1>
        <div class="module-tabs">
          <button
            class="module-tab"
            :class="{ active: activeTab === 'upload' }"
            @click="activeTab = 'upload'"
          >
            文件上传
          </button>
          <button
            class="module-tab"
            :class="{ active: activeTab === 'ingest' }"
            @click="switchToIngest()"
          >
            知识入库
            <span v-if="doneCount" class="tab-badge">{{ doneCount }}</span>
          </button>
        </div>
      </div>
      <p class="page-desc">图片 / Word / PDF 上传后后台多线程自动解析；在「知识入库」检查解析结果无误后，一键入库知识库与 llm-wiki 百科</p>
    </div>

    <!-- ==================== Tab 1：文件上传 ==================== -->
    <div v-show="activeTab === 'upload'" class="tab-panel">
      <!-- ---------- 图片上传 ---------- -->
      <div class="upload-section">
        <div class="module-head">
          <span class="module-badge img">图</span>
          <div>
            <div class="module-title">图片上传<span class="module-sub-inline">（手写笔记 OCR）</span></div>
            <div class="module-sub">支持单张 / 批量上传；上传成功后后台自动 OCR 识别，点击可预览原始图片</div>
          </div>
        </div>

        <!-- OCR 引擎 -->
        <div class="ocr-selector">
          <label class="ocr-label">OCR 识别引擎</label>
          <div class="ocr-options">
            <button
              v-for="opt in ocrOptions"
              :key="opt.value"
              class="ocr-option"
              :class="{ selected: selectedOCR === opt.value }"
              @click="selectedOCR = opt.value"
            >
              {{ opt.label }}
            </button>
          </div>
        </div>

        <!-- 单张 / 批量 子模式 -->
        <div class="submode-row">
          <button class="submode-btn" :class="{ active: ocrSubMode === 'single' }" @click="ocrSubMode = 'single'">单张上传</button>
          <button class="submode-btn" :class="{ active: ocrSubMode === 'batch' }" @click="ocrSubMode = 'batch'">批量上传</button>
        </div>

        <!-- 单张上传 -->
        <div
          v-if="ocrSubMode === 'single'"
          class="upload-zone"
          :class="{ 'drag-over': imgDragOver }"
          @dragover.prevent="imgDragOver = true"
          @dragleave="imgDragOver = false"
          @drop.prevent="handleImageDrop"
          @click="triggerFileInput"
        >
          <input ref="fileInputRef" type="file" accept="image/*" class="file-input-hidden" @change="handleImageSelect" />
          <input ref="cameraInputRef" type="file" accept="image/*" capture="environment" class="file-input-hidden" @change="handleImageSelect" />
          <div class="upload-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="38" height="38">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
              <circle cx="8.5" cy="8.5" r="1.5"/>
              <polyline points="21 15 16 10 5 21"/>
            </svg>
          </div>
          <p class="upload-text">点击或拖拽图片到此处</p>
          <p class="upload-hint">支持 JPG / PNG / BMP 等图片（≤5MB，超出自动压缩）</p>
          <div class="upload-actions" @click.stop>
            <button type="button" class="btn-upload-action" @click="triggerCameraInput">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16">
                <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
                <circle cx="12" cy="13" r="4"/>
              </svg>
              拍照上传
            </button>
            <button type="button" class="btn-upload-action" @click="triggerFileInput">选择图片</button>
          </div>
        </div>

        <!-- 批量上传：待选列表 + 逐个/一键上传 -->
        <template v-else>
          <div
            class="upload-zone"
            :class="{ 'drag-over': batchDragOver, 'has-file': batchFiles.length > 0 }"
            @dragover.prevent="batchDragOver = true"
            @dragleave="batchDragOver = false"
            @drop.prevent="handleBatchDrop"
            @click="triggerBatchInput"
          >
            <input ref="batchInputRef" type="file" accept="image/*" multiple class="file-input-hidden" @change="handleBatchSelect" />
            <input ref="batchCameraInputRef" type="file" accept="image/*" capture="environment" multiple class="file-input-hidden" @change="handleBatchSelect" />
            <template v-if="batchFiles.length === 0">
              <div class="upload-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="38" height="38">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="17 8 12 3 7 8"/>
                  <line x1="12" y1="3" x2="12" y2="15"/>
                </svg>
              </div>
              <p class="upload-text">点击或拖拽多张图片到此处</p>
              <p class="upload-hint">可一次选择多张图片加入待上传列表，逐个上传或一键全部上传</p>
              <div class="upload-actions" @click.stop>
                <button type="button" class="btn-upload-action" @click="triggerBatchCamera">拍照追加</button>
                <button type="button" class="btn-upload-action" @click="triggerBatchInput">选择多张图片</button>
              </div>
            </template>
            <template v-else>
              <div class="batch-summary">已选择 {{ batchFiles.length }} 张图片 · {{ formatSize(batchTotalSize) }}</div>
              <p class="upload-hint">点击「全部上传」依次上传并后台自动解析；也可在下方列表逐张上传</p>
            </template>
          </div>

          <div v-if="batchFiles.length > 0" class="batch-list">
            <div
              v-for="(bf, idx) in batchFiles"
              :key="bf.localId"
              class="batch-item"
              :class="bf.status"
            >
              <img
                v-if="bf.thumb"
                :src="bf.thumb"
                class="batch-thumb"
                title="点击图片编辑（裁剪 / 旋转）"
                alt=""
                @click.stop="openBatchCrop(idx)"
              />
              <span v-else class="fi-icon image">图</span>
              <div class="fi-main" @click="(bf.status === 'ready' || bf.status === 'error') && openBatchCrop(idx)">
                <div class="fi-name" :title="bf.name">{{ bf.name }}</div>
                <div class="fi-meta">
                  <span class="fi-size">{{ formatSize(bf.size) }}</span>
                  <span v-if="bf.status === 'ready'" class="fi-status">待上传</span>
                  <span v-else-if="bf.status === 'uploading'" class="fi-status">上传中 {{ bf.progress }}%</span>
                  <span v-else-if="bf.status === 'uploaded'" class="status-ok">✓ 已上传，后台解析中</span>
                  <span v-else class="status-err">✗ {{ bf.error || '上传失败' }}</span>
                </div>
                <div v-if="bf.status === 'uploading'" class="progress-track">
                  <div class="progress-fill" :style="{ width: bf.progress + '%' }"></div>
                </div>
              </div>
              <div class="fi-actions" @click.stop>
                <button v-if="bf.status === 'ready' || bf.status === 'error'" class="btn btn-sm btn-outline" @click="openBatchCrop(idx)">编辑</button>
                <button v-if="bf.status === 'ready' || bf.status === 'error'" class="btn btn-sm btn-primary" @click="uploadBatchItem(idx)">上传</button>
                <button v-if="bf.status === 'ready' || bf.status === 'error'" class="btn btn-sm btn-outline-danger" @click="removeBatchItem(idx)">移除</button>
              </div>
            </div>
            <div class="batch-actions">
              <button class="btn btn-primary" :disabled="batchUploading || batchReadyCount === 0" @click="uploadAllBatch">
                <span v-if="batchUploading" class="spinner-xs"></span>
                全部上传（{{ batchReadyCount }}）
              </button>
              <button class="btn btn-secondary" :disabled="batchUploading" @click="clearBatchList">清空列表</button>
            </div>
          </div>
        </template>
      </div>

      <!-- ---------- Word / PDF 上传 ---------- -->
      <div class="upload-section">
        <div class="module-head">
          <span class="module-badge doc">文</span>
          <div>
            <div class="module-title">Word / PDF 上传</div>
            <div class="module-sub">上传成功后后台自动逐页解析（Word 生成原始排版 PDF 预览），点击可预览原始文档</div>
          </div>
        </div>
        <div
          class="upload-zone"
          :class="{ 'drag-over': docDragOver }"
          @dragover.prevent="docDragOver = true"
          @dragleave="docDragOver = false"
          @drop.prevent="handleDocDrop"
          @click="triggerDocInput"
        >
          <input ref="docInputRef" type="file" accept=".docx,.pdf" multiple class="file-input-hidden" @change="handleDocSelect" />
          <div class="upload-icon doc">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="38" height="38">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
          </div>
          <p class="upload-text">点击或拖拽 Word / PDF 文档到此处（可多选）</p>
          <p class="upload-hint">支持 .docx、.pdf（≤20MB）</p>
          <div class="upload-actions" @click.stop>
            <button type="button" class="btn-upload-action" @click="triggerDocInput">选择文档</button>
          </div>
        </div>
      </div>

      <!-- ---------- 上传文件列表（实时进度 + 后台解析状态） ---------- -->
      <div v-if="fileList.length" class="upload-section">
        <div class="file-list-head">
          <span>上传文件列表（后台多线程解析中）</span>
          <span class="file-list-count">{{ fileList.length }} 个</span>
        </div>
        <div
          v-for="f in fileList"
          :key="f.id || f.localId"
          class="file-item"
          :class="['st-' + f.status]"
        >
          <span class="fi-icon" :class="f.source_type">{{ typeIcon(f.source_type) }}</span>
          <div class="fi-main">
            <div class="fi-name" :title="f.filename">{{ f.filename }}</div>
            <div class="fi-meta">
              <span class="fi-size">{{ formatSize(f.file_size || f.size || 0) }}</span>
              <span class="fi-status">
                <template v-if="f.status === 'uploading'">上传中 {{ f.progress }}%</template>
                <template v-else-if="f.status === 'pending'">排队等待解析…</template>
                <template v-else-if="f.status === 'parsing'"><span class="spinner-xs"></span> 后台解析中…</template>
                <template v-else-if="f.status === 'done'">
                  <span class="status-ok">✓ 解析完成</span>
                  <span v-if="f.source_type === 'image'">{{ f.text_length }} 字</span>
                  <span v-else>{{ f.page_count }} 页 / {{ f.text_length }} 字</span>
                </template>
                <template v-else-if="f.status === 'error'">
                  <span class="status-err">✗ 解析失败</span>
                </template>
              </span>
            </div>
            <div v-if="f.status === 'uploading'" class="progress-track">
              <div class="progress-fill" :style="{ width: f.progress + '%' }"></div>
            </div>
            <div v-if="f.status === 'error' && f.error" class="fi-error" :title="f.error">{{ f.error }}</div>
          </div>
          <div class="fi-actions" @click.stop>
            <button v-if="f.status === 'done'" class="btn btn-sm btn-success" @click="goIngest(f)">去入库 →</button>
            <button v-if="canPreview(f)" class="btn btn-sm btn-outline" @click="previewOriginal(f)">预览</button>
            <button v-if="f.status === 'error'" class="btn btn-sm btn-primary" @click="retryParse(f)">重试</button>
            <button v-if="f.status !== 'uploading'" class="btn btn-sm btn-outline-danger" @click="removeFile(f)">删除</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== Tab 2：知识入库 ==================== -->
    <div v-show="activeTab === 'ingest'" class="tab-panel">
      <!-- 目标知识库 -->
      <div class="upload-section">
        <div class="module-head">
          <span class="module-badge">库</span>
          <div>
            <div class="module-title">目标知识库</div>
            <div class="module-sub">解析完成的文件将入库到所选知识库，并同步编译 llm-wiki 百科卡片</div>
          </div>
        </div>
        <div class="kb-selector-row">
          <select v-model="kbVisibilityFilter" class="kb-select kb-filter" @change="onKbFilterChange">
            <option value="public">公共知识库</option>
            <option value="private">个人知识库</option>
          </select>
          <select v-model="selectedKbId" class="kb-select">
            <option v-for="kb in filteredKbList" :key="kb.id" :value="kb.id">
              {{ kb.name }}（{{ kb.entry_count }} 条）
            </option>
          </select>
          <button class="btn-new-kb" @click="openCreateKbModal">+ 新建知识库</button>
        </div>
      </div>

      <!-- 待入库列表（解析进展实时刷新） -->
      <div class="upload-section">
        <div class="module-head">
          <span class="module-badge">入</span>
          <div>
            <div class="module-title">待入库列表</div>
            <div class="module-sub">查看后台解析进展与处理效果（图片 OCR 文本 / 文档解析文本），检查无误后逐文件入库</div>
          </div>
        </div>

        <!-- 入库实时进度 -->
        <div
          v-if="ingestProgress"
          class="ingest-progress"
          :class="{ done: ingestProgress.finished, 'has-fail': ingestProgress.fail > 0 }"
        >
          <div class="ip-row">
            <span v-if="!ingestProgress.finished" class="ip-text">
              <span class="spinner-xs"></span>
              入库中 {{ ingestProgress.done + ingestProgress.fail }} / {{ ingestProgress.total }} · 《{{ ingestProgress.currentName }}》
            </span>
            <span v-else class="ip-text">
              <template v-if="ingestProgress.fail === 0">✓ 全部入库成功：{{ ingestProgress.done }} 个文件已写入知识库，后台编译百科卡片中</template>
              <template v-else>入库结束：成功 {{ ingestProgress.done }} 个，失败 {{ ingestProgress.fail }} 个</template>
            </span>
            <button v-if="ingestProgress.finished" class="ip-close" @click="ingestProgress = null" aria-label="关闭">✕</button>
          </div>
          <div class="progress-track">
            <div class="progress-fill" :class="{ 'fill-ok': ingestProgress.finished && ingestProgress.fail === 0 }" :style="{ width: ingestProgressPercent + '%' }"></div>
          </div>
        </div>

        <div v-if="!ingestItems.length" class="result-empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          <p>暂无待入库文件</p>
          <p class="empty-tip">在「文件上传」上传图片 / Word / PDF，解析完成后会出现在这里</p>
        </div>

        <template v-else>
          <div class="ingest-list">
            <div
              v-for="it in ingestItems"
              :key="it.id"
              class="ingest-item"
              :class="{ active: selectedItemId === it.id, 'is-error': it.status === 'error' }"
              @click="selectItem(it)"
            >
              <span class="fi-icon small" :class="it.source_type">{{ typeIcon(it.source_type) }}</span>
              <div class="ii-main">
                <div class="ii-name" :title="it.filename">{{ it.filename }}</div>
                <div class="ii-meta">
                  <template v-if="it.status === 'done'">
                    <span class="status-ok">✓ 待入库</span>
                    {{ it.source_type === 'image' ? `OCR ${it.text_length} 字` : `${it.page_count} 页 / ${it.text_length} 字` }}
                  </template>
                  <template v-else>
                    <span class="status-err">✗ 解析失败，点击重试</span>
                  </template>
                </div>
              </div>
              <span class="ii-arrow">{{ selectedItemId === it.id ? '▼' : '▶' }}</span>
            </div>
          </div>

          <button
            v-if="doneCount > 1"
            class="btn btn-success ingest-all-btn"
            :disabled="ingestBusy"
            @click="ingestAll"
          >
            <span v-if="ingestBusy" class="spinner-xs"></span>
            全部入库（{{ doneCount }} 个）
          </button>
        </template>

        <!-- 选中项：处理效果检查 / 编辑 / 入库 -->
        <div v-if="detail" class="review-panel">
          <div class="review-head">
            <span class="review-title">处理效果检查 · {{ detail.filename }}</span>
            <input
              v-model="ingestTitle"
              type="text"
              class="title-input"
              placeholder="入库标题（留空使用文件名）"
            />
          </div>

          <template v-if="detail.status === 'error'">
            <div class="review-error">解析失败：{{ detail.error }}</div>
            <div class="review-actions">
              <button class="btn btn-primary" @click="retryParse(detail)">重新解析</button>
            </div>
          </template>

          <template v-else-if="detail.source_type === 'image'">
            <!-- 图片：原图对照 + 可编辑 OCR 文本 -->
            <div class="review-image-row">
              <img :src="buildImageUrl(detail.file_path)" class="review-thumb" alt="原始图片" />
              <textarea v-model="detail.parsed_text" class="ocr-textarea" rows="12" placeholder="OCR 识别文本"></textarea>
            </div>
          </template>

          <template v-else>
            <!-- 文档：逐页解析文本，可编辑，可跳回原始文档对应页 -->
            <div class="doc-pages-toolbar">
              <span class="doc-pages-info">
                共 {{ detail.pages.length }} 页 · 合计 {{ detailTextLength }} 字
                <span v-if="pdfPreviewName(detail) && detail.source_type === 'word'" class="doc-preview-hint">（Word 已生成原始排版 PDF 预览）</span>
              </span>
              <a
                v-if="pdfPreviewName(detail)"
                :href="pdfPreviewUrl(detail)"
                target="_blank"
                class="btn btn-sm btn-outline"
              >预览原始文档</a>
            </div>
            <div class="doc-pages-list">
              <div v-for="page in detail.pages" :key="page.page_number" class="doc-page-card">
                <div class="doc-page-header">
                  <span class="doc-page-title">第 {{ page.page_number }} 页</span>
                  <div class="doc-page-actions">
                    <a
                      v-if="pdfPreviewName(detail)"
                      :href="pageLink(detail, page.page_number)"
                      target="_blank"
                      class="btn btn-sm btn-outline"
                    >预览</a>
                    <button
                      class="btn btn-sm btn-primary"
                      :disabled="polishingPages.has(page.page_number)"
                      @click="polishPage(page)"
                    >
                      <span v-if="polishingPages.has(page.page_number)" class="spinner-xs"></span>
                      {{ polishingPages.has(page.page_number) ? '润色中...' : '一键润色' }}
                    </button>
                  </div>
                </div>
                <textarea v-model="page.text" class="doc-page-textarea" rows="6"></textarea>
              </div>
            </div>
          </template>

          <div v-if="detail.status === 'done'" class="review-actions">
            <button
              v-if="detail.source_type === 'image'"
              class="btn btn-primary"
              :disabled="isPolishing"
              @click="polishImageText"
            >
              <span v-if="isPolishing" class="spinner-xs"></span>
              {{ isPolishing ? '润色中...' : '一键润色' }}
            </button>
            <button class="btn btn-success" :disabled="ingestBusy" @click="ingestSelected">
              <span v-if="ingestBusy" class="spinner-xs"></span>
              {{ ingestBusy ? '入库中...' : '确认入库（知识库 + llm-wiki）' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 图片裁剪弹窗 -->
    <div v-if="cropModalVisible" class="crop-modal" @click.self="closeCropModal">
      <div class="crop-modal-content">
        <div class="crop-modal-header">
          <h3 class="crop-modal-title">编辑图片（裁剪 / 旋转）</h3>
          <button class="crop-modal-close" @click="closeCropModal" aria-label="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="crop-modal-body">
          <div class="crop-toolbar">
            <button class="btn btn-outline btn-sm" @click="rotateCropImage(-90)" title="向左旋转 90°">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
              </svg>
              向左旋转
            </button>
            <button class="btn btn-outline btn-sm" @click="rotateCropImage(90)" title="向右旋转 90°">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
              向右旋转
            </button>
            <span class="rotate-angle">{{ targetAngle }}°</span>
            <button class="rotate-reset" @click="resetTransform" title="恢复图片原始位置、缩放和旋转">复位</button>
            <span class="crop-toolbar-hint">先旋转摆正图片，再选取截取区域</span>
          </div>
          <div ref="cropperWrapperRef" class="cropper-wrapper">
            <img ref="cropperImageRef" :src="cropImageUrl" class="cropper-image" alt="待裁剪" />
          </div>
          <p class="crop-hint">拖动框选区域，滚轮缩放；长按图片区域可拖动图片</p>
        </div>
        <div class="crop-modal-footer">
          <button class="btn btn-secondary" @click="skipCrop">不裁剪，使用原图</button>
          <button class="btn btn-primary" @click="confirmCrop">确认裁剪</button>
        </div>
      </div>
    </div>

    <!-- 原始图片预览弹窗 -->
    <div v-if="previewImageSrc" class="preview-modal" @click.self="previewImageSrc = ''">
      <div class="preview-modal-content">
        <div class="preview-modal-header">
          <h3>原始文档预览</h3>
          <button class="modal-close" @click="previewImageSrc = ''" aria-label="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <img :src="previewImageSrc" class="preview-img" alt="原始文档" />
      </div>
    </div>

    <!-- 新建知识库弹窗 -->
    <div v-if="showCreateKbModal" class="preview-modal" @click.self="showCreateKbModal = false">
      <div class="preview-modal-content kb-modal">
        <div class="preview-modal-header">
          <h3>新建知识库</h3>
          <button class="modal-close" @click="showCreateKbModal = false" aria-label="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="kb-modal-body">
          <label class="ocr-label">知识库名称</label>
          <input v-model="newKb.name" type="text" class="title-input" placeholder="如：中医养生笔记" />
          <label class="ocr-label">可见范围</label>
          <div class="kb-selector-row">
            <select v-model="newKb.visibility" class="kb-select">
              <option value="private">个人知识库（仅自己可见）</option>
              <option v-if="kbIsAdmin" value="public">公共知识库（所有人可见）</option>
            </select>
          </div>
          <div class="review-actions">
            <button class="btn btn-secondary" @click="showCreateKbModal = false">取消</button>
            <button class="btn btn-primary" :disabled="creatingKb" @click="submitCreateKb">
              <span v-if="creatingKb" class="spinner-xs"></span> 创建
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 全局状态提示 -->
    <div v-if="statusMessage" class="status-toast" :class="statusType">{{ statusMessage }}</div>
  </div>
</template>

<script setup>
import { ref, computed, reactive, onMounted, onUnmounted, nextTick } from 'vue'
import Cropper from 'cropperjs/dist/cropper.esm.js'
import {
  uploadPendingFile,
  listPendingUploads,
  getPendingUpload,
  ingestPendingUpload,
  retryPendingParse,
  deletePendingUpload,
  polishOCR,
  listKnowledgeBases,
  createKnowledgeBase,
  getImageUrl as buildImageUrl,
} from '@/api'
import { compressImageFile, compressCanvasToFile, MAX_UPLOAD_SIZE } from '@/utils/image'

// ---- 模块切换 ----
const activeTab = ref('upload')
function switchToIngest() {
  activeTab.value = 'ingest'
  refreshPending()
  startPolling()
}

// ---- OCR 引擎选项 ----
const ocrOptions = [
  { value: 'local', label: '本地 OCR (EasyOCR)' },
  { value: 'aliyun', label: '阿里云 OCR' },
  { value: 'custom_api', label: '自定义 OCR API' },
]
const selectedOCR = ref('aliyun')
const ocrSubMode = ref('single')

// ---- 上传区状态 ----
const fileInputRef = ref(null)
const cameraInputRef = ref(null)
const batchInputRef = ref(null)
const batchCameraInputRef = ref(null)
const docInputRef = ref(null)
const imgDragOver = ref(false)
const batchDragOver = ref(false)
const docDragOver = ref(false)

// ---- 文件列表：本地上传中条目 + 服务端待处理条目 ----
const uploadingItems = ref([])
const serverItems = ref([])
const fileList = computed(() => [...uploadingItems.value, ...serverItems.value])

// ---- 状态提示 ----
const statusMessage = ref('')
const statusType = ref('info')
let statusTimer = null
function showStatus(msg, type = 'info') {
  statusMessage.value = msg
  statusType.value = type
  if (statusTimer) clearTimeout(statusTimer)
  statusTimer = setTimeout(() => {
    statusMessage.value = ''
  }, 4000)
}

// ---- 文件类型识别 ----
const IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp']
function detectType(file) {
  const ext = '.' + ((file.name || '').split('.').pop() || '').toLowerCase()
  if (IMAGE_EXTS.includes(ext)) return 'image'
  if (ext === '.docx') return 'word'
  if (ext === '.pdf') return 'pdf'
  return null
}
function typeIcon(type) {
  return type === 'image' ? '图' : type === 'word' ? 'W' : type === 'pdf' ? 'P' : '文'
}
function formatSize(bytes) {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}
const baseName = (p) => (p ? String(p).split('/').pop() : '')

// ---- 图片单张上传 ----
function triggerFileInput() {
  fileInputRef.value?.click()
}
function triggerCameraInput() {
  cameraInputRef.value?.click()
}
function handleImageSelect(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  handleImageFiles(files)
}
function handleImageDrop(e) {
  imgDragOver.value = false
  handleImageFiles(Array.from(e.dataTransfer.files || []))
}
function handleImageFiles(files) {
  const images = []
  for (const f of files) {
    if (detectType(f) === 'image') images.push(f)
    else showStatus(`《${f.name}》不是图片，请在下方 Word / PDF 区上传`, 'error')
  }
  if (!images.length) return
  // 选择图片后先进入编辑界面（裁剪 / 旋转），确认后再上传
  openCropEditor(images[0], { type: 'single' })
  if (images.length > 1) {
    showStatus(`单张上传仅取第一张图片，其余 ${images.length - 1} 张请使用批量上传`, 'info')
  }
}

// ---- 图片批量上传（待选列表 + 逐个/一键上传） ----
const batchFiles = ref([])
const batchTotalSize = computed(() => batchFiles.value.reduce((s, f) => s + (f.size || 0), 0))
const batchReadyCount = computed(() => batchFiles.value.filter((f) => f.status === 'ready' || f.status === 'error').length)
const batchUploading = computed(() => batchFiles.value.some((f) => f.status === 'uploading'))

function triggerBatchInput() {
  batchInputRef.value?.click()
}
function triggerBatchCamera() {
  batchCameraInputRef.value?.click()
}
function handleBatchSelect(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  appendBatchFiles(files)
}
function handleBatchDrop(e) {
  batchDragOver.value = false
  appendBatchFiles(Array.from(e.dataTransfer.files || []))
}
function appendBatchFiles(files) {
  for (const f of files) {
    if (detectType(f) !== 'image') {
      showStatus(`《${f.name}》不是图片，批量上传仅支持图片`, 'error')
      continue
    }
    if (batchFiles.value.some((x) => x.name === f.name && x.size === f.size)) continue
    batchFiles.value.push({
      localId: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      file: f,
      name: f.name,
      size: f.size,
      thumb: URL.createObjectURL(f),
      status: 'ready',
      progress: 0,
      error: '',
    })
  }
}
function removeBatchItem(idx) {
  const bf = batchFiles.value[idx]
  if (bf?.thumb) URL.revokeObjectURL(bf.thumb)
  batchFiles.value.splice(idx, 1)
}
function clearBatchList() {
  batchFiles.value.forEach((f) => {
    if (f.status !== 'uploading' && f.thumb) URL.revokeObjectURL(f.thumb)
  })
  batchFiles.value = batchFiles.value.filter((f) => f.status === 'uploading')
}

async function uploadBatchItem(idx) {
  const bf = batchFiles.value[idx]
  if (!bf || bf.status === 'uploading' || bf.status === 'uploaded') return
  bf.status = 'uploading'
  bf.progress = 0
  startPolling()
  try {
    let toUpload = bf.file
    if (bf.file.size > MAX_UPLOAD_SIZE) {
      toUpload = await compressImageFile(bf.file)
    }
    const res = await uploadPendingFile(toUpload, selectedOCR.value, (p) => {
      bf.progress = p
    })
    bf.status = 'uploaded'
    if (res.data?.item && !serverItems.value.some((s) => s.id === res.data.item.id)) {
      serverItems.value.unshift(res.data.item)
    }
    showStatus(`《${bf.name}》上传成功，后台正在解析…`, 'success')
    refreshPending()
    // 已上传的条目进入下方统一文件列表，待选列表自动移除
    setTimeout(() => {
      const i = batchFiles.value.findIndex((x) => x.localId === bf.localId)
      if (i >= 0 && batchFiles.value[i].status === 'uploaded') batchFiles.value.splice(i, 1)
    }, 1200)
  } catch (e) {
    bf.status = 'error'
    bf.error = e.message || '上传失败'
    showStatus(`《${bf.name}》上传失败：${e.message || '网络异常'}`, 'error')
  }
}

async function uploadAllBatch() {
  const targets = batchFiles.value.filter((f) => f.status === 'ready' || f.status === 'error')
  for (const t of targets) {
    const idx = batchFiles.value.findIndex((x) => x.localId === t.localId)
    if (idx >= 0) await uploadBatchItem(idx)
  }
}

// ---- 图片编辑（裁剪 / 旋转） ----
const cropModalVisible = ref(false)
// 服务对象：{ type: 'single' } 单张模式 或 { type: 'batch', index } 批量条目
const cropTarget = ref(null)
const cropImageUrl = ref('')
const cropperInstance = ref(null)
const cropperImageRef = ref(null)
const cropperWrapperRef = ref(null)
const fileBeforeCrop = ref(null)
const targetAngle = ref(0)

const CROPPER_TEMPLATE = `
  <cropper-canvas background>
    <cropper-image initial-center-size="contain" rotatable scalable translatable></cropper-image>
    <cropper-selection initial-coverage="0.8" movable resizable zoomable outlined>
      <cropper-handle action="move" plain></cropper-handle>
      <cropper-handle action="n-resize"></cropper-handle>
      <cropper-handle action="e-resize"></cropper-handle>
      <cropper-handle action="s-resize"></cropper-handle>
      <cropper-handle action="w-resize"></cropper-handle>
      <cropper-handle action="ne-resize"></cropper-handle>
      <cropper-handle action="nw-resize"></cropper-handle>
      <cropper-handle action="se-resize"></cropper-handle>
      <cropper-handle action="sw-resize"></cropper-handle>
    </cropper-selection>
  </cropper-canvas>
`

// 长按拖动图片相关状态
let longPressTimer = null
let panStart = null
let isLongPressPanning = false
const LONG_PRESS_THRESHOLD = 50
const PAN_MOVE_THRESHOLD = 6
const PAN_SENSITIVITY = 3

function isCropperHandle(target) {
  if (!target) return false
  const tag = target.tagName?.toLowerCase?.() || ''
  return tag === 'cropper-handle' || !!target.closest?.('cropper-handle')
}

function cancelLongPress() {
  window.clearTimeout(longPressTimer)
  longPressTimer = null
  panStart = null
}

function onWrapperPointerDown(e) {
  if (e.button !== 0 || isCropperHandle(e.target)) return
  panStart = { x: e.clientX, y: e.clientY }
  isLongPressPanning = false
  longPressTimer = window.setTimeout(() => {
    isLongPressPanning = true
    if (cropperWrapperRef.value) {
      cropperWrapperRef.value.style.cursor = 'grabbing'
    }
    // 取消裁剪器当前的选中/拖拽动作，避免它跟我们的图片拖动冲突
    const canvas = cropperInstance.value?.getCropperCanvas?.()
    if (canvas && typeof canvas.$setAction === 'function') {
      canvas.$setAction('none')
    }
    window.addEventListener('pointermove', onWindowPointerMove)
    window.addEventListener('pointerup', onWindowPointerUp, { once: true })
  }, LONG_PRESS_THRESHOLD)
}

function onWrapperPointerMove(e) {
  if (!panStart || isLongPressPanning) return
  const dx = e.clientX - panStart.x
  const dy = e.clientY - panStart.y
  if (Math.abs(dx) > PAN_MOVE_THRESHOLD || Math.abs(dy) > PAN_MOVE_THRESHOLD) {
    cancelLongPress()
  }
}

function onWindowPointerMove(e) {
  if (!isLongPressPanning || !panStart) return
  const dx = e.clientX - panStart.x
  const dy = e.clientY - panStart.y
  const cropperImage = cropperInstance.value?.getCropperImage?.()
  if (cropperImage && typeof cropperImage.$translate === 'function') {
    cropperImage.$translate(dx * PAN_SENSITIVITY, dy * PAN_SENSITIVITY)
  }
  panStart = { x: e.clientX, y: e.clientY }
}

function onWindowPointerUp() {
  if (isLongPressPanning) {
    isLongPressPanning = false
    if (cropperWrapperRef.value) {
      cropperWrapperRef.value.style.cursor = ''
    }
  }
  window.removeEventListener('pointermove', onWindowPointerMove)
  cancelLongPress()
}

function attachPanListeners() {
  const wrapper = cropperWrapperRef.value
  if (!wrapper) return
  wrapper.addEventListener('pointerdown', onWrapperPointerDown, { capture: true })
  wrapper.addEventListener('pointermove', onWrapperPointerMove, { capture: true })
}

function detachPanListeners() {
  const wrapper = cropperWrapperRef.value
  if (!wrapper) return
  wrapper.removeEventListener('pointerdown', onWrapperPointerDown, { capture: true })
  wrapper.removeEventListener('pointermove', onWrapperPointerMove, { capture: true })
  window.removeEventListener('pointermove', onWindowPointerMove)
  window.removeEventListener('pointerup', onWindowPointerUp)
}

function initCropper() {
  if (cropperInstance.value) {
    cropperInstance.value.destroy()
    cropperInstance.value = null
  }
  detachPanListeners()
  targetAngle.value = 0
  const image = cropperImageRef.value
  if (!image) return
  cropperInstance.value = new Cropper(image, {
    template: CROPPER_TEMPLATE,
  })
  nextTick(() => attachPanListeners())
}

// 打开编辑界面：single=单张选择后；batch=批量列表条目
function openCropEditor(file, target) {
  fileBeforeCrop.value = file
  cropImageUrl.value = ''
  cropTarget.value = target
  cropModalVisible.value = true
  const reader = new FileReader()
  reader.onload = (e) => {
    cropImageUrl.value = e.target.result
    nextTick(() => initCropper())
  }
  reader.readAsDataURL(file)
}

function openBatchCrop(idx) {
  const bf = batchFiles.value[idx]
  if (!bf || bf.status === 'uploading' || bf.status === 'uploaded') return
  openCropEditor(bf.file, { type: 'batch', index: idx })
}

function normalizeRotation(deg) {
  let v = deg % 360
  if (v > 180) v -= 360
  if (v < -180) v += 360
  return v
}

function applyRotation(newAngle) {
  const cropperImage = cropperInstance.value?.getCropperImage?.()
  const delta = newAngle - targetAngle.value
  if (cropperImage && typeof cropperImage.$rotate === 'function' && delta !== 0) {
    // cropperjs v2 的 $rotate 数字参数为弧度，必须传 'deg' 字符串
    cropperImage.$rotate(delta + 'deg')
  }
  targetAngle.value = normalizeRotation(newAngle)
}

function rotateCropImage(deg) {
  applyRotation(targetAngle.value + deg)
}

function resetTransform() {
  const cropperImage = cropperInstance.value?.getCropperImage?.()
  if (cropperImage) {
    if (typeof cropperImage.$resetTransform === 'function') {
      cropperImage.$resetTransform()
    }
    if (typeof cropperImage.$center === 'function') {
      cropperImage.$center('contain')
    }
  }
  targetAngle.value = 0
}

function closeCropModal() {
  cropModalVisible.value = false
  cropTarget.value = null
  detachPanListeners()
  if (cropperInstance.value) {
    cropperInstance.value.destroy()
    cropperInstance.value = null
  }
  cropImageUrl.value = ''
  fileBeforeCrop.value = null
}

// 裁剪/压缩后的图片写回批量条目
async function confirmCrop() {
  if (!cropperInstance.value || !fileBeforeCrop.value) return
  showStatus('正在处理图片...', 'info')
  try {
    const selection = cropperInstance.value.getCropperSelection()
    if (!selection) {
      throw new Error('未找到裁剪选区')
    }
    // 按原图自然像素分辨率导出，避免裁剪输出分辨率低于原图导致 OCR 变糊
    let exportOptions
    try {
      const cropperImage = cropperInstance.value.getCropperImage()
      const [a, b] = cropperImage.$getTransform()
      const scaleFactor = Math.sqrt(a * a + b * b) || 1
      const exportWidth = Math.min(4096, Math.round(selection.offsetWidth / scaleFactor))
      if (exportWidth > selection.offsetWidth) {
        exportOptions = { width: exportWidth }
      }
    } catch (e) {
      // 获取变换失败时退回默认导出
    }
    const canvas = await selection.$toCanvas(exportOptions)
    if (!canvas || canvas.width === 0 || canvas.height === 0) {
      throw new Error('裁剪区域为空，请重新选择裁剪范围')
    }
    const originalSize = fileBeforeCrop.value.size
    const file = await compressCanvasToFile(canvas, fileBeforeCrop.value.name, MAX_UPLOAD_SIZE)
    const message = file.size < originalSize ? `图片已裁剪并压缩至 ${formatSize(file.size)}` : '图片已裁剪'
    finishCropEdit(file, message)
  } catch (err) {
    console.error('裁剪/压缩失败:', err)
    showStatus('图片处理失败: ' + (err.message || '请重试'), 'error')
  }
}

async function skipCrop() {
  if (!fileBeforeCrop.value) return
  showStatus('正在压缩图片...', 'info')
  try {
    const originalSize = fileBeforeCrop.value.size
    const file = await compressImageFile(fileBeforeCrop.value, MAX_UPLOAD_SIZE)
    const message = file.size < originalSize ? `图片已压缩至 ${formatSize(file.size)}` : '已使用原图'
    finishCropEdit(file, message)
  } catch (err) {
    console.error('压缩失败:', err)
    showStatus('图片处理失败: ' + (err.message || '请重试'), 'error')
  }
}

// 编辑结束：批量条目写回；单张模式直接上传
function finishCropEdit(file, message) {
  const target = cropTarget.value
  const bf = target?.type === 'batch' ? batchFiles.value[target.index] : null
  closeCropModal()
  if (target?.type === 'batch') {
    if (!bf) return
    if (bf.thumb) URL.revokeObjectURL(bf.thumb)
    bf.file = file
    bf.size = file.size
    bf.thumb = URL.createObjectURL(file)
    bf.status = 'ready'
    bf.error = ''
    showStatus(message, 'success')
  } else {
    showStatus(message + '，开始上传…', 'success')
    uploadOne(file, 'image', file.name)
  }
}

// ---- Word / PDF 上传 ----
function triggerDocInput() {
  docInputRef.value?.click()
}
function handleDocSelect(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  handleDocFiles(files)
}
function handleDocDrop(e) {
  docDragOver.value = false
  handleDocFiles(Array.from(e.dataTransfer.files || []))
}
function handleDocFiles(files) {
  for (const f of files) {
    const type = detectType(f)
    if (type === 'word' || type === 'pdf') {
      uploadOne(f, type, f.name)
    } else {
      showStatus(`《${f.name}》不是 Word / PDF 文档，图片请在上方图片区上传`, 'error')
    }
  }
}

// ---- 统一上传入口（单张图片 / 文档） ----
async function uploadOne(file, type, displayName) {
  const localId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  uploadingItems.value.push({
    localId,
    filename: displayName,
    source_type: type,
    size: file.size,
    status: 'uploading',
    progress: 0,
  })
  startPolling()
  try {
    let toUpload = file
    if (type === 'image' && file.size > MAX_UPLOAD_SIZE) {
      try {
        toUpload = await compressImageFile(file)
      } catch (err) {
        showStatus(`图片压缩失败：${displayName}，${err.message || err}`, 'error')
        uploadingItems.value = uploadingItems.value.filter((x) => x.localId !== localId)
        return
      }
    }
    const res = await uploadPendingFile(toUpload, selectedOCR.value, (p) => {
      const it = uploadingItems.value.find((x) => x.localId === localId)
      if (it) it.progress = p
    })
    // 上传成功：本地占位替换为服务端条目（后端已开始后台解析）
    const idx = uploadingItems.value.findIndex((x) => x.localId === localId)
    if (idx >= 0) uploadingItems.value.splice(idx, 1)
    if (res.data?.item && !serverItems.value.some((s) => s.id === res.data.item.id)) {
      serverItems.value.unshift(res.data.item)
    }
    showStatus(`《${displayName}》上传成功，后台正在解析…`, 'success')
    refreshPending()
  } catch (e) {
    const it = uploadingItems.value.find((x) => x.localId === localId)
    if (it) {
      it.status = 'error'
      it.error = e.message || '上传失败'
    }
    showStatus(`《${displayName}》上传失败：${e.message || '网络异常'}`, 'error')
  }
}

// ---- 轮询解析进展 ----
let pollTimer = null
function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(refreshPending, 2000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}
async function refreshPending() {
  try {
    const res = await listPendingUploads()
    serverItems.value = res.data.items || []
  } catch (e) {
    /* 静默：下个周期重试 */
  }
  const active = serverItems.value.some((i) => i.status === 'pending' || i.status === 'parsing')
  if (!active && uploadingItems.value.length === 0 && !batchUploading.value) stopPolling()
}

// ---- 文件操作：预览 / 重试 / 删除 ----
const previewImageSrc = ref('')

function canPreview(item) {
  if (item.status === 'uploading' || item.status === 'error') return false
  if (item.source_type === 'image') return true
  // 文档：PDF 随时可预览；Word 需解析阶段生成 PDF 预览后
  return item.source_type === 'pdf' || !!item.pdf_preview_path
}

function previewOriginal(item) {
  if (item.source_type === 'image') {
    previewImageSrc.value = buildImageUrl(item.file_path)
    return
  }
  const pdfName = item.pdf_preview_path
    ? baseName(item.pdf_preview_path)
    : item.source_type === 'pdf' ? baseName(item.file_path) : ''
  if (!pdfName) {
    showStatus('原始文档预览生成中，请稍候…', 'info')
    return
  }
  window.open(`/doc-preview?pdf=${encodeURIComponent(pdfName)}`, '_blank')
}

function pdfPreviewName(item) {
  return item?.pdf_preview_path ? baseName(item.pdf_preview_path) : ''
}
function pdfPreviewUrl(item) {
  return `/doc-preview?pdf=${encodeURIComponent(pdfPreviewName(item))}`
}
function pageLink(item, pageNumber) {
  return `${buildImageUrl(item.pdf_preview_path)}#page=${pageNumber}`
}

async function retryParse(item) {
  try {
    await retryPendingParse(item.id)
    showStatus(`《${item.filename}》已重新提交解析`, 'info')
    startPolling()
    refreshPending()
  } catch (e) {
    showStatus('重试失败：' + (e.response?.data?.detail || e.message), 'error')
  }
}

async function removeFile(item) {
  if (!item.id) {
    // 本地上传失败条目，直接移除
    uploadingItems.value = uploadingItems.value.filter((x) => x.localId !== item.localId)
    return
  }
  if (!confirm(`确定删除《${item.filename}》吗？原始文件将一并删除`)) return
  try {
    await deletePendingUpload(item.id)
    serverItems.value = serverItems.value.filter((x) => x.id !== item.id)
    if (selectedItemId.value === item.id) clearDetail()
    showStatus('已删除', 'info')
  } catch (e) {
    showStatus('删除失败：' + (e.response?.data?.detail || e.message), 'error')
  }
}

// ---- 知识入库模块 ----
const ingestItems = computed(() =>
  serverItems.value.filter((i) => i.status === 'done' || i.status === 'error')
)
const doneCount = computed(() => serverItems.value.filter((i) => i.status === 'done').length)

const selectedItemId = ref(null)
const detail = ref(null)
const ingestTitle = ref('')
const ingestBusy = ref(false)
const isPolishing = ref(false)
const polishingPages = ref(new Set())

function goIngest(item) {
  // 「去入库」：切换到知识入库 Tab 并选中该项
  switchToIngest()
  selectItem(item)
  setTimeout(() => {
    document.querySelector('.review-panel')?.scrollIntoView({ behavior: 'smooth' })
  }, 50)
}

async function selectItem(item) {
  selectedItemId.value = item.id
  ingestTitle.value = ''
  detail.value = null
  if (item.status === 'error') {
    detail.value = { ...item }
    return
  }
  try {
    const res = await getPendingUpload(item.id)
    detail.value = res.data.item
  } catch (e) {
    showStatus('加载解析结果失败：' + (e.response?.data?.detail || e.message), 'error')
  }
}

function clearDetail() {
  selectedItemId.value = null
  detail.value = null
  ingestTitle.value = ''
}

const detailTextLength = computed(() => {
  if (!detail.value?.pages?.length) return 0
  return detail.value.pages.reduce((sum, p) => sum + (p.text || '').length, 0)
})

// ---- 润色 ----
async function polishImageText() {
  if (!detail.value?.parsed_text?.trim()) return
  isPolishing.value = true
  try {
    const res = await polishOCR(detail.value.parsed_text)
    if (res.data.success && res.data.polished_text) {
      detail.value.parsed_text = res.data.polished_text
      showStatus('润色完成，请检查后入库', 'success')
    } else {
      showStatus(res.data.message || '润色失败', 'error')
    }
  } catch (e) {
    showStatus('润色失败：' + (e.message || '网络异常'), 'error')
  } finally {
    isPolishing.value = false
  }
}

async function polishPage(page) {
  if (!page.text?.trim()) return
  polishingPages.value.add(page.page_number)
  try {
    const res = await polishOCR(page.text)
    if (res.data.success && res.data.polished_text) {
      page.text = res.data.polished_text
      showStatus(`第 ${page.page_number} 页润色完成`, 'success')
    } else {
      showStatus(res.data.message || '润色失败', 'error')
    }
  } catch (e) {
    showStatus('润色失败：' + (e.message || '网络异常'), 'error')
  } finally {
    polishingPages.value.delete(page.page_number)
  }
}

// ---- 入库 ----
// 入库实时进度：{ total, done, fail, currentName, finished }
const ingestProgress = ref(null)
const ingestProgressPercent = computed(() => {
  const p = ingestProgress.value
  if (!p || !p.total) return 0
  return Math.round(((p.done + p.fail) / p.total) * 100)
})

async function ingestCurrent() {
  const d = detail.value
  const payload = {
    kb_id: selectedKbId.value,
    title: ingestTitle.value.trim(),
  }
  if (d.source_type === 'image') {
    payload.parsed_text = d.parsed_text
  } else {
    payload.pages = d.pages.map((p) => ({ page_number: p.page_number, text: p.text }))
  }
  const res = await ingestPendingUpload(d.id, payload)
  return res.data
}

async function ingestSelected() {
  if (!detail.value || ingestBusy.value) return
  ingestBusy.value = true
  ingestProgress.value = {
    total: 1,
    done: 0,
    fail: 0,
    currentName: detail.value.filename,
    finished: false,
  }
  try {
    const data = await ingestCurrent()
    serverItems.value = serverItems.value.filter((x) => x.id !== detail.value.id)
    const title = data.title || detail.value.filename
    ingestProgress.value.done = 1
    ingestProgress.value.currentName = title
    showStatus(
      `《${title}》已入库：${data.chunk_count} 个知识片段，后台正在编译 llm-wiki 百科卡片`,
      'success'
    )
    clearDetail()
  } catch (e) {
    ingestProgress.value.fail = 1
    showStatus('入库失败：' + (e.response?.data?.detail || e.message), 'error')
  } finally {
    ingestProgress.value.finished = true
    ingestBusy.value = false
  }
}

async function ingestAll() {
  const targets = serverItems.value.filter((i) => i.status === 'done')
  if (!targets.length || ingestBusy.value) return
  if (!confirm(`将 ${targets.length} 个文件依次入库到当前选中的知识库，继续吗？`)) return
  ingestBusy.value = true
  ingestProgress.value = {
    total: targets.length,
    done: 0,
    fail: 0,
    currentName: targets[0].filename,
    finished: false,
  }
  let ok = 0
  let fail = 0
  try {
    // 当前选中且有编辑内容的条目走编辑入库，其余用服务端已存文本
    if (detail.value && detail.value.status === 'done' && targets.some((t) => t.id === detail.value.id)) {
      ingestProgress.value.currentName = detail.value.filename
      try {
        await ingestCurrent()
        serverItems.value = serverItems.value.filter((x) => x.id !== detail.value.id)
        ok++
        ingestProgress.value.done = ok
      } catch (e) {
        fail++
        ingestProgress.value.fail = fail
      }
      clearDetail()
    }
    for (const item of targets) {
      if (detail.value && item.id === detail.value.id) continue
      if (!serverItems.value.some((x) => x.id === item.id)) continue
      ingestProgress.value.currentName = item.filename
      try {
        await ingestPendingUpload(item.id, { kb_id: selectedKbId.value })
        serverItems.value = serverItems.value.filter((x) => x.id !== item.id)
        ok++
        ingestProgress.value.done = ok
      } catch (e) {
        fail++
        ingestProgress.value.fail = fail
      }
    }
    showStatus(
      fail === 0
        ? `全部入库完成：${ok} 个文件已写入知识库，后台正在编译百科卡片`
        : `入库完成：成功 ${ok} 个，失败 ${fail} 个`,
      fail === 0 ? 'success' : 'error'
    )
  } finally {
    ingestProgress.value.finished = true
    ingestBusy.value = false
  }
}

// ---- 目标知识库 ----
const kbList = ref([])
const kbIsAdmin = ref(false)
const selectedKbId = ref('')
const kbVisibilityFilter = ref('public')
const filteredKbList = computed(() =>
  kbList.value.filter((kb) => kb.visibility === kbVisibilityFilter.value)
)

async function loadKnowledgeBases() {
  try {
    const res = await listKnowledgeBases()
    kbList.value = res.data.bases || []
    kbIsAdmin.value = !!res.data.is_admin
    if (!selectedKbId.value && kbList.value.length > 0) {
      selectedKbId.value = kbList.value[0].id
    }
    const selected = kbList.value.find((k) => k.id === selectedKbId.value)
    if (selected) kbVisibilityFilter.value = selected.visibility
  } catch (e) {
    console.error('加载知识库列表失败:', e)
  }
}

function onKbFilterChange() {
  if (!filteredKbList.value.some((kb) => kb.id === selectedKbId.value)) {
    selectedKbId.value = filteredKbList.value[0]?.id || ''
  }
}

const showCreateKbModal = ref(false)
const creatingKb = ref(false)
const newKb = reactive({ name: '', topic: '其他', visibility: 'private' })

function openCreateKbModal() {
  newKb.name = ''
  newKb.visibility = kbIsAdmin.value ? 'public' : 'private'
  showCreateKbModal.value = true
}

async function submitCreateKb() {
  if (!newKb.name.trim()) {
    showStatus('请输入知识库名称', 'error')
    return
  }
  creatingKb.value = true
  try {
    const res = await createKnowledgeBase({ ...newKb, name: newKb.name.trim() })
    showCreateKbModal.value = false
    await loadKnowledgeBases()
    selectedKbId.value = res.data.kb.id
    kbVisibilityFilter.value = res.data.kb.visibility || 'private'
  } catch (e) {
    showStatus('创建知识库失败：' + (e.response?.data?.detail || e.message), 'error')
  } finally {
    creatingKb.value = false
  }
}

// ---- 生命周期 ----
onMounted(() => {
  loadKnowledgeBases()
  refreshPending()
  startPolling()
})
onUnmounted(() => {
  stopPolling()
  if (statusTimer) clearTimeout(statusTimer)
})
</script>

<style scoped>
.upload-view {
  max-width: 1100px;
  margin: 0 auto;
  padding: 24px;
  /* 父容器 .main-content 为 100vh + overflow:hidden，页面需自身承担滚动 */
  height: 100%;
  overflow-y: auto;
}
.page-header { margin-bottom: 18px; }
.page-title-row {
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap; margin-bottom: 6px;
}
.page-title {
  font-size: 22px; font-weight: 700; color: #2d2a24;
}
.page-desc { font-size: 13px; color: #8a7e72; line-height: 1.6; }

/* ---------- 顶部模块切换 Tab ---------- */
.module-tabs { display: flex; gap: 12px; }
.module-tab {
  position: relative; padding: 8px 22px; border-radius: 10px; font-size: 14.5px; font-weight: 600;
  border: 2px solid #e0d5c5; background: #fff; color: #8a7e72; cursor: pointer;
  transition: all 0.15s;
}
.module-tab:hover { border-color: #c98a4b; color: #c98a4b; }
.module-tab.active {
  border-color: #c98a4b; background: #f9eddb; color: #84431f;
}
.tab-badge {
  position: absolute; top: -8px; right: -8px; min-width: 20px; height: 20px;
  padding: 0 5px; border-radius: 10px; background: #5a8a4a; color: #fff;
  font-size: 11px; font-weight: 700; display: flex; align-items: center; justify-content: center;
}
.tab-panel { display: flex; flex-direction: column; gap: 18px; }

/* ---------- 区块卡片 ---------- */
.upload-section {
  background: #fff; border: 1px solid #e8dfd2; border-radius: 14px; padding: 18px;
}

/* ---------- 模块头 ---------- */
.module-head {
  display: flex; align-items: flex-start; gap: 10px; margin-bottom: 14px;
}
.module-badge {
  flex-shrink: 0; width: 30px; height: 30px; border-radius: 50%;
  background: #c98a4b; color: #fff; font-size: 13px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.module-badge.img { background: #c98a4b; }
.module-badge.doc { background: #4a6fa5; }
.module-title { font-size: 16px; font-weight: 700; color: #2d2a24; }
.module-sub-inline { font-size: 12.5px; font-weight: 400; color: #8a7e72; margin-left: 6px; }
.module-sub { font-size: 12px; color: #8a7e72; margin-top: 3px; line-height: 1.5; }

/* ---------- OCR 引擎 / 子模式 ---------- */
.ocr-selector { margin-bottom: 12px; }
.ocr-label {
  display: block; font-size: 12.5px; color: #6b6156; margin-bottom: 6px; font-weight: 600;
}
.ocr-options { display: flex; gap: 8px; flex-wrap: wrap; }
.ocr-option {
  padding: 6px 12px; border-radius: 8px; font-size: 12.5px;
  border: 1px solid #e0d5c5; background: #fdfbf7; color: #6b6156; cursor: pointer;
  transition: all 0.15s;
}
.ocr-option:hover { border-color: #c98a4b; }
.ocr-option.selected {
  background: #f9eddb; border-color: #c98a4b; color: #84431f; font-weight: 600;
}
.submode-row { display: flex; gap: 8px; margin-bottom: 12px; }
.submode-btn {
  padding: 6px 16px; border-radius: 8px; font-size: 13px; cursor: pointer;
  border: 1px solid #e0d5c5; background: #fdfbf7; color: #6b6156; transition: all 0.15s;
}
.submode-btn:hover { border-color: #c98a4b; }
.submode-btn.active {
  background: #c98a4b; border-color: #c98a4b; color: #fff; font-weight: 600;
}

/* ---------- 知识库选择 ---------- */
.kb-selector-row { display: flex; gap: 8px; flex-wrap: wrap; }
.kb-select {
  flex: 1; min-width: 130px; padding: 7px 10px; border-radius: 8px; font-size: 13px;
  border: 1px solid #e0d5c5; background: #fdfbf7; color: #2d2a24; outline: none;
}
.kb-select:focus { border-color: #c98a4b; }
.kb-filter { flex: 0 0 130px; }
.btn-new-kb {
  padding: 7px 12px; border-radius: 8px; font-size: 12.5px; white-space: nowrap;
  border: 1.5px dashed #d9c9b4; background: #fff; color: #c98a4b; cursor: pointer;
}
.btn-new-kb:hover { background: #f9eddb; border-style: solid; }
.title-input {
  width: 100%; padding: 8px 12px; border-radius: 8px; font-size: 13px;
  border: 1px solid #e0d5c5; background: #fdfbf7; color: #2d2a24; outline: none;
}
.title-input:focus { border-color: #c98a4b; }

/* ---------- 上传区 ---------- */
.upload-zone {
  border: 2px dashed #d9c9b4; border-radius: 12px; background: #fdfbf7;
  padding: 26px 20px; text-align: center; cursor: pointer; transition: all 0.2s;
}
.upload-zone:hover, .upload-zone.drag-over {
  border-color: #c98a4b; background: #f9eddb;
}
.upload-zone.has-file { padding: 16px 20px; }
.upload-icon { color: #c98a4b; margin-bottom: 10px; }
.upload-icon.doc { color: #4a6fa5; }
.upload-text { font-size: 14.5px; font-weight: 600; color: #2d2a24; margin-bottom: 5px; }
.upload-hint { font-size: 12px; color: #8a7e72; margin-bottom: 12px; }
.upload-actions { display: flex; gap: 10px; justify-content: center; }
.btn-upload-action {
  display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px;
  border-radius: 8px; font-size: 13px; cursor: pointer;
  border: 1px solid #e0d5c5; background: #fff; color: #6b6156; transition: all 0.15s;
}
.btn-upload-action:hover { border-color: #c98a4b; color: #c98a4b; }
.file-input-hidden { display: none; }

/* ---------- 批量待选列表 ---------- */
.batch-summary { font-size: 14px; font-weight: 700; color: #84431f; margin-bottom: 4px; }
.batch-list { margin-top: 12px; }
.batch-item {
  display: flex; align-items: flex-start; gap: 10px; padding: 9px 12px;
  border: 1px solid #ede6dc; border-radius: 10px; background: #fdfbf7; margin-bottom: 8px;
}
.batch-item.uploaded { border-color: #cfe3c8; background: #f6faf4; }
.batch-item.error { border-color: #ecc9c9; background: #fdf6f6; }
.batch-actions { display: flex; gap: 10px; margin-top: 10px; }

/* ---------- 文件列表（上传进度 + 解析状态） ---------- */
.file-list-head {
  display: flex; justify-content: space-between; font-size: 13px; font-weight: 700;
  color: #2d2a24; margin-bottom: 10px;
}
.file-list-count { color: #8a7e72; font-weight: 400; }
.file-item {
  display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px;
  border: 1px solid #ede6dc; border-radius: 10px; background: #fdfbf7; margin-bottom: 8px;
  transition: border-color 0.15s;
}
.file-item.st-done { border-color: #cfe3c8; background: #f6faf4; }
.file-item.st-error { border-color: #ecc9c9; background: #fdf6f6; }
.fi-icon {
  flex-shrink: 0; width: 34px; height: 34px; border-radius: 8px; font-size: 13px;
  font-weight: 700; display: flex; align-items: center; justify-content: center;
  background: #f3ece2; color: #8a7e72;
}
.fi-icon.image { background: #f9eddb; color: #c98a4b; }
.fi-icon.word { background: #e4edf9; color: #4a6fa5; }
.fi-icon.pdf { background: #f9e2e2; color: #b05555; }
.fi-icon.small { width: 28px; height: 28px; font-size: 11px; }
.fi-main { flex: 1; min-width: 0; }
.fi-name {
  font-size: 13.5px; font-weight: 600; color: #2d2a24;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.fi-meta {
  display: flex; gap: 10px; align-items: center; font-size: 12px; color: #8a7e72;
  margin-top: 3px; flex-wrap: wrap;
}
.fi-status { display: inline-flex; align-items: center; gap: 5px; }
.status-ok { color: #5a8a4a; font-weight: 600; }
.status-err { color: #b05555; font-weight: 600; }
.fi-error {
  font-size: 11.5px; color: #b05555; margin-top: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.fi-actions { display: flex; flex-direction: column; gap: 5px; flex-shrink: 0; }
.progress-track {
  height: 5px; background: #ede6dc; border-radius: 3px; margin-top: 7px; overflow: hidden;
}
.progress-fill {
  height: 100%; background: #c98a4b; border-radius: 3px; transition: width 0.2s;
}

/* ---------- 待入库列表 ---------- */
.ingest-list { margin-top: 4px; }
.ingest-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 12px; cursor: pointer;
  border: 1px solid #ede6dc; border-radius: 10px; background: #fdfbf7; margin-bottom: 8px;
  transition: all 0.15s;
}
.ingest-item:hover { border-color: #c98a4b; }
.ingest-item.active { border-color: #c98a4b; background: #f9eddb; }
.ingest-item.is-error { border-color: #ecc9c9; background: #fdf6f6; }
.ii-main { flex: 1; min-width: 0; }
.ii-name {
  font-size: 13px; font-weight: 600; color: #2d2a24;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ii-meta { font-size: 11.5px; color: #8a7e72; margin-top: 2px; }
.ii-arrow { color: #b3a798; font-size: 10px; flex-shrink: 0; }
.ingest-all-btn { width: 100%; margin: 4px 0 12px; }

/* ---------- 处理效果检查 ---------- */
.review-panel {
  margin-top: 12px; border: 1px solid #e8dfd2; border-radius: 12px;
  background: #fdfbf7; padding: 14px;
}
.review-head { margin-bottom: 12px; display: flex; flex-direction: column; gap: 8px; }
.review-title { font-size: 13.5px; font-weight: 700; color: #2d2a24; }
.review-error {
  font-size: 13px; color: #b05555; background: #f9e2e2; border-radius: 8px;
  padding: 10px 12px; margin-bottom: 10px; line-height: 1.5;
}
.review-image-row { display: flex; gap: 12px; align-items: flex-start; }
.review-thumb {
  width: 180px; max-height: 320px; object-fit: contain; border-radius: 8px;
  border: 1px solid #e8dfd2; background: #fff; flex-shrink: 0;
}
.ocr-textarea {
  flex: 1; min-width: 0; padding: 10px 12px; border-radius: 8px; font-size: 13px;
  line-height: 1.7; border: 1px solid #e0d5c5; background: #fff; color: #2d2a24;
  outline: none; resize: vertical; font-family: inherit;
}
.ocr-textarea:focus { border-color: #c98a4b; }
.doc-pages-toolbar {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12.5px; color: #6b6156; margin-bottom: 10px; flex-wrap: wrap; gap: 8px;
}
.doc-preview-hint { color: #5a8a4a; }
.doc-pages-list { display: flex; flex-direction: column; gap: 10px; max-height: 420px; overflow-y: auto; }
.doc-page-card {
  border: 1px solid #e8dfd2; border-radius: 10px; background: #fff; padding: 10px 12px;
}
.doc-page-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;
}
.doc-page-title { font-size: 12.5px; font-weight: 700; color: #84431f; }
.doc-page-actions { display: flex; gap: 6px; }
.doc-page-textarea {
  width: 100%; padding: 8px 10px; border-radius: 8px; font-size: 13px; line-height: 1.7;
  border: 1px solid #ede6dc; background: #fdfbf7; color: #2d2a24; outline: none;
  resize: vertical; font-family: inherit;
}
.doc-page-textarea:focus { border-color: #c98a4b; background: #fff; }
.review-actions { display: flex; gap: 10px; margin-top: 12px; justify-content: flex-end; }

/* ---------- 空态 ---------- */
.result-empty {
  text-align: center; padding: 40px 20px; color: #b3a798;
}
.result-empty p { margin-top: 10px; font-size: 13px; }
.empty-tip { font-size: 12px !important; color: #c9bfae; }

/* ---------- 按钮 ---------- */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 8px 16px; border-radius: 8px; font-size: 13px; cursor: pointer;
  border: none; transition: all 0.15s;
}
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-sm { padding: 4px 10px; font-size: 12px; border-radius: 6px; }
.btn-primary { background: #c98a4b; color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b57a3f; }
.btn-success { background: #5a8a4a; color: #fff; }
.btn-success:hover:not(:disabled) { background: #4d7840; }
.btn-secondary { background: #f3ece2; color: #6b6156; }
.btn-secondary:hover:not(:disabled) { background: #e8dfd2; }
.btn-outline {
  background: #fff; border: 1px solid #e0d5c5; color: #6b6156;
}
.btn-outline:hover:not(:disabled) { border-color: #c98a4b; color: #c98a4b; }
.btn-outline-danger {
  background: #fff; border: 1px solid #ecc9c9; color: #b05555;
}
.btn-outline-danger:hover:not(:disabled) { background: #fdf6f6; }

/* ---------- 加载动画 ---------- */
.spinner-xs, .spinner-sm {
  display: inline-block; border-radius: 50%; border: 2px solid rgba(0,0,0,0.15);
  border-top-color: currentColor; animation: spin 0.7s linear infinite;
}
.spinner-xs { width: 12px; height: 12px; }
.spinner-sm { width: 15px; height: 15px; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ---------- 弹窗 ---------- */
.preview-modal {
  position: fixed; inset: 0; background: rgba(45, 42, 36, 0.55); z-index: 1000;
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.preview-modal-content {
  background: #fff; border-radius: 14px; max-width: 860px; width: 100%;
  max-height: 86vh; overflow: auto; padding: 18px;
}
.preview-modal-content.kb-modal { max-width: 420px; }
.preview-modal-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.preview-modal-header h3 { font-size: 15px; font-weight: 700; color: #2d2a24; }
.modal-close {
  background: none; border: none; color: #8a7e72; cursor: pointer; padding: 4px;
  border-radius: 6px; display: flex;
}
.modal-close:hover { background: #f3ece2; color: #2d2a24; }
.preview-img {
  max-width: 100%; max-height: 70vh; display: block; margin: 0 auto;
  border-radius: 8px; border: 1px solid #ede6dc;
}
.kb-modal-body { display: flex; flex-direction: column; gap: 10px; }

/* ---------- 状态提示 ---------- */
.status-toast {
  position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
  padding: 10px 20px; border-radius: 10px; font-size: 13px; z-index: 1100;
  background: #2d2a24; color: #fff; box-shadow: 0 6px 20px rgba(0,0,0,0.18);
  max-width: 80vw;
}
.status-toast.success { background: #5a8a4a; }
.status-toast.error { background: #b05555; }

/* ---------- 批量条目缩略图 ---------- */
.batch-thumb {
  flex-shrink: 0; width: 44px; height: 44px; object-fit: cover;
  border-radius: 8px; border: 1px solid #e8dfd2; background: #fff; cursor: pointer;
  transition: transform 0.15s;
}
.batch-thumb:hover { transform: scale(1.06); border-color: #c98a4b; }

/* ---------- 入库实时进度 ---------- */
.ingest-progress {
  border: 1px solid #e0d5c5; background: #f9eddb; border-radius: 10px;
  padding: 10px 14px; margin-bottom: 12px;
}
.ingest-progress.done { background: #f6faf4; border-color: #cfe3c8; }
.ingest-progress.has-fail { background: #fdf6f6; border-color: #ecc9c9; }
.ip-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  margin-bottom: 7px;
}
.ip-text {
  display: inline-flex; align-items: center; gap: 7px;
  font-size: 12.5px; font-weight: 600; color: #84431f;
}
.ingest-progress.done .ip-text { color: #5a8a4a; }
.ingest-progress.has-fail .ip-text { color: #b05555; }
.ip-close {
  background: none; border: none; color: #8a7e72; cursor: pointer;
  font-size: 13px; padding: 2px 6px; border-radius: 6px;
}
.ip-close:hover { background: rgba(0,0,0,0.06); color: #2d2a24; }
.progress-fill.fill-ok { background: #5a8a4a; }

/* ---------- 图片裁剪弹窗 ---------- */
.crop-modal {
  position: fixed; inset: 0; z-index: 1050; background: rgba(0, 0, 0, 0.85);
  display: flex; align-items: center; justify-content: center; padding: 20px;
  animation: fadeIn 0.2s ease;
}
.crop-modal-content {
  background: #fff; border-radius: 16px; width: 100%; max-width: 90vw;
  height: min(90vh, 900px); max-height: 90vh;
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}
.crop-modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px; border-bottom: 1px solid #e8dfd2; flex-shrink: 0;
}
.crop-modal-title { font-size: 16px; font-weight: 600; color: #2d2a24; }
.crop-modal-close {
  width: 32px; height: 32px; border: none; border-radius: 8px; background: transparent;
  cursor: pointer; color: #8a7e72; display: flex; align-items: center; justify-content: center;
}
.crop-modal-close:hover { background: #f3ece2; color: #2d2a24; }
.crop-modal-body {
  flex: 1; overflow: hidden; padding: 12px 16px;
  display: flex; flex-direction: column; gap: 8px; min-height: 0;
}
.cropper-wrapper {
  flex: 1; min-height: 0; max-height: 100%; background: #f3ece2; border-radius: 12px;
  overflow: hidden; display: flex; align-items: center; justify-content: center;
}
.cropper-image { display: block; max-width: 100%; max-height: 100%; }
:deep(cropper-canvas) { display: block; width: 100%; height: 100%; }
.crop-hint {
  font-size: 12px; color: #8a7e72; text-align: center; flex-shrink: 0;
}
.crop-toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  flex-shrink: 0; padding: 2px 0 10px;
}
.crop-toolbar .btn { display: inline-flex; align-items: center; gap: 4px; }
.crop-toolbar-hint {
  margin-left: auto; font-size: 12px; color: #8a7e72;
}
.rotate-angle {
  font-size: 12px; font-variant-numeric: tabular-nums; min-width: 36px;
  text-align: right; color: #2d2a24;
}
.rotate-reset {
  font-size: 12px; padding: 2px 8px; border-radius: 6px;
  border: 1px solid #e0d5c5; background: transparent; color: #8a7e72; cursor: pointer;
}
.rotate-reset:hover { background: rgba(0, 0, 0, 0.04); }
.crop-modal-footer {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 12px 20px; border-top: 1px solid #e8dfd2; flex-shrink: 0;
}
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
</style>
