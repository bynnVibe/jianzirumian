<template>
  <div class="upload-view">
    <!-- 头部 -->
    <div class="page-header">
      <h1 class="page-title">上传笔记</h1>
      <p class="page-desc">支持 OCR 图片识别、Word 文档、PDF 文档导入，自动提取文字并存入知识库</p>
    </div>

    <!-- 上传模块切换 -->
    <div class="mode-tabs">
      <button
        class="mode-tab"
        :class="{ active: uploadMode === 'ocr' }"
        @click="uploadMode = 'ocr'"
      >
        OCR 图片识别
      </button>
      <button
        class="mode-tab"
        :class="{ active: uploadMode === 'word' }"
        @click="uploadMode = 'word'"
      >
        Word 导入
      </button>
      <button
        class="mode-tab"
        :class="{ active: uploadMode === 'pdf' }"
        @click="uploadMode = 'pdf'"
      >
        PDF 导入
      </button>
    </div>

    <!-- OCR 子模式切换 -->
    <div v-if="uploadMode === 'ocr'" class="sub-mode-tabs">
      <button
        class="sub-mode-tab"
        :class="{ active: ocrSubMode === 'single' }"
        @click="ocrSubMode = 'single'"
      >
        单张识别
      </button>
      <button
        class="sub-mode-tab"
        :class="{ active: ocrSubMode === 'batch' }"
        @click="ocrSubMode = 'batch'"
      >
        批量识别
      </button>
    </div>

    <div class="upload-layout">
      <!-- 左侧：上传区域 -->
      <div class="upload-panel">
        <!-- OCR 提供商选择 -->
        <div v-if="uploadMode === 'ocr'" class="ocr-selector">
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

        <!-- 目标知识库 -->
        <div class="ocr-selector visibility-selector">
          <label class="ocr-label">目标知识库</label>
          <div class="kb-selector-row">
            <select v-model="kbVisibilityFilter" class="kb-select kb-filter" @change="onKbFilterChange">
              <option value="public">公共知识库</option>
              <option value="private">个人知识库</option>
            </select>
            <select v-model="selectedKbId" class="kb-select" @change="onKbChange">
              <option v-for="kb in filteredKbList" :key="kb.id" :value="kb.id">
                {{ kb.name }}（{{ kb.entry_count }} 条）
              </option>
            </select>
            <button class="btn-new-kb" @click="openCreateKbModal">+ 新建知识库</button>
          </div>
        </div>

        <!-- 保存标题（单张图片 / 文档） -->
        <div v-if="uploadMode !== 'ocr' || ocrSubMode === 'single'" class="ocr-selector title-selector">
          <label class="ocr-label">保存标题</label>
          <input
            v-model="saveTitle"
            type="text"
            class="title-input"
            placeholder="留空默认使用文件名"
          />
        </div>

        <!-- ===== 单文件 OCR 上传 ===== -->
        <template v-if="uploadMode === 'ocr' && ocrSubMode === 'single'">
          <!-- 拖拽上传 -->
          <div
            class="upload-zone"
            :class="{ 'drag-over': isDragOver, 'has-file': selectedFile }"
            @dragover.prevent="isDragOver = true"
            @dragleave="isDragOver = false"
            @drop.prevent="handleDrop"
            @click="triggerFileInput"
          >
            <input
              ref="fileInputRef"
              type="file"
              accept="image/*"
              class="file-input-hidden"
              @change="handleFileSelect"
            />
            <!-- 拍照专用 input：capture 属性在移动端直接唤起相机 -->
            <input
              ref="cameraInputRef"
              type="file"
              accept="image/*"
              capture="environment"
              class="file-input-hidden"
              @change="handleFileSelect"
            />

            <template v-if="!selectedFile">
              <div class="upload-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="17 8 12 3 7 8"/>
                  <line x1="12" y1="3" x2="12" y2="15"/>
                </svg>
              </div>
              <p class="upload-text">点击或拖拽图片到此处</p>
              <p class="upload-hint">支持 JPG、PNG、BMP 格式，单张最大 5MB（超出会自动压缩）</p>
              <!-- 移动端快捷入口：拍照直接唤起相机 -->
              <div class="upload-actions" @click.stop>
                <button type="button" class="btn-upload-action" @click="triggerCameraInput">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16">
                    <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
                    <circle cx="12" cy="13" r="4"/>
                  </svg>
                  拍照上传
                </button>
                <button type="button" class="btn-upload-action" @click="triggerFileInput">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16">
                    <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                    <circle cx="8.5" cy="8.5" r="1.5"/>
                    <polyline points="21 15 16 10 5 21"/>
                  </svg>
                  相册选择
                </button>
              </div>
            </template>

            <template v-else>
              <div class="file-preview">
                <img :src="filePreviewUrl" class="preview-image" alt="预览" />
                <div class="file-info">
                  <span class="file-name">{{ selectedFile.name }}</span>
                  <span class="file-size">{{ formatSize(selectedFile.size) }}</span>
                </div>
              </div>
            </template>
          </div>

          <!-- 单文件操作按钮 -->
          <div class="action-buttons" v-if="selectedFile">
            <button class="btn btn-secondary" @click="clearFile">
              重新选择
            </button>
            <button class="btn btn-secondary" @click="openUploadPreview" title="查看最终上传给 OCR 的图片">
              预览上传图片
            </button>
            <button
              class="btn btn-primary"
              :disabled="isProcessing"
              @click="previewOCRHandler"
            >
              <div v-if="isPreviewing" class="spinner-sm"></div>
              {{ isPreviewing ? '识别中...' : '预览识别结果' }}
            </button>
            <button
              class="btn btn-success"
              :disabled="isProcessing || !ocrText"
              @click="processAndSave"
            >
              <div v-if="isSaving" class="spinner-sm"></div>
              {{ isSaving ? '保存中...' : '保存到知识库' }}
            </button>
          </div>
        </template>

        <!-- ===== 批量 OCR 上传 ===== -->
        <template v-if="uploadMode === 'ocr' && ocrSubMode === 'batch'">
          <div
            class="upload-zone"
            :class="{ 'has-file': batchFiles.length > 0 }"
            @click="triggerBatchInput"
          >
            <input
              ref="batchInputRef"
              type="file"
              accept="image/*"
              multiple
              class="file-input-hidden"
              @change="handleBatchSelect"
            />
            <!-- 批量模式拍照：capture 唤起相机，拍摄的照片追加到待识别列表 -->
            <input
              ref="batchCameraInputRef"
              type="file"
              accept="image/*"
              capture="environment"
              class="file-input-hidden"
              @change="handleBatchCameraSelect"
            />

            <template v-if="batchFiles.length === 0">
              <div class="upload-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="17 8 12 3 7 8"/>
                  <line x1="12" y1="3" x2="12" y2="15"/>
                </svg>
              </div>
              <p class="upload-text">点击选择多张图片</p>
              <p class="upload-hint">可一次选择多张图片批量上传</p>
              <div class="upload-actions" @click.stop>
                <button type="button" class="btn-upload-action" :disabled="isBatchRunning" @click="triggerBatchCamera">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" width="16" height="16">
                    <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>
                    <circle cx="12" cy="13" r="4"/>
                  </svg>
                  拍照添加
                </button>
              </div>
            </template>

            <template v-else>
              <div class="batch-file-summary">
                <span class="batch-count">已选择 {{ batchFiles.length }} 个文件</span>
                <span class="batch-size">{{ formatSize(batchTotalSize) }}</span>
              </div>
            </template>
          </div>

          <!-- 批量文件列表 -->
          <div v-if="batchFiles.length > 0" class="batch-file-list">
            <div
              v-for="(bf, idx) in batchFiles"
              :key="idx"
              class="batch-file-item"
              :class="[bf.status, { selected: selectedBatchIndex === idx }]"
              @click="selectBatchItem(idx)"
            >
              <span class="bf-index">{{ idx + 1 }}</span>
              <span class="bf-name" :title="bf.file.name">{{ bf.file.name }}</span>
              <span class="bf-size">{{ formatSize(bf.file.size) }}</span>
              <span class="bf-actions" @click.stop>
                <button
                  class="btn btn-sm btn-outline"
                  :disabled="isBatchRunning || bf.status === 'recognizing' || bf.status === 'uploading'"
                  @click="openBatchCrop(idx)"
                  title="裁剪 / 旋转该图片"
                >编辑</button>
                <button
                  v-if="bf.status === 'recognized'"
                  class="btn btn-sm btn-success"
                  :disabled="!bf.ocrText"
                  @click="uploadBatchItem(idx)"
                >上传</button>
              </span>
              <span class="bf-status">
                <template v-if="bf.status === 'pending'">待识别</template>
                <template v-else-if="bf.status === 'recognizing'">
                  <span class="spinner-xs"></span> 识别中
                </template>
                <template v-else-if="bf.status === 'recognized'">
                  <span class="status-icon success-icon">&#10003;</span> {{ (bf.ocrText || '').length }} 字
                </template>
                <template v-else-if="bf.status === 'uploading'">
                  <span class="spinner-xs"></span> 上传中
                </template>
                <template v-else-if="bf.status === 'uploaded'">
                  <span class="status-icon success-icon">&#10003;</span> 已入库
                </template>
                <template v-else-if="bf.status === 'error'">
                  <span class="status-icon error-icon">&#10007;</span>
                </template>
              </span>
              <span v-if="bf.status === 'error'" class="bf-error" :title="bf.error">{{ bf.error }}</span>
            </div>
          </div>

          <p v-if="batchFiles.length > 0" class="batch-hint">识别完成后点击列表项，在右侧查看并编辑结果；之后可单条上传或一键全部上传入库</p>

          <!-- 批量操作按钮 -->
          <div class="action-buttons" v-if="batchFiles.length > 0">
            <button class="btn btn-secondary" @click="clearBatch" :disabled="isBatchRunning">
              重新选择
            </button>
            <button
              class="btn btn-primary"
              :disabled="isBatchRunning || isBatchCompressing || !batchHasWork"
              @click="startBatchRecognize"
            >
              <div v-if="isBatchRunning" class="spinner-sm"></div>
              {{ isBatchRunning ? `识别中 ${batchDoneCount}/${batchFiles.length}...` : '开始批量识别' }}
            </button>
            <button
              class="btn btn-success"
              :disabled="isBatchRunning || recognizedBatchCount === 0"
              @click="uploadAllRecognized"
            >
              上传全部已识别 ({{ recognizedBatchCount }})
            </button>
          </div>

          <!-- 批量进度条 -->
          <div v-if="isBatchRunning || batchDoneCount > 0" class="batch-progress-bar">
            <div class="progress-track">
              <div
                class="progress-fill"
                :style="{ width: batchProgressPercent + '%' }"
              ></div>
            </div>
            <span class="progress-text">{{ batchProgressPercent }}%</span>
          </div>
        </template>

        <!-- ===== Word 文档导入 ===== -->
        <template v-if="uploadMode === 'word'">
          <div
            class="upload-zone"
            :class="{ 'has-file': docFile }"
            @dragover.prevent="isDocDragOver = true"
            @dragleave="isDocDragOver = false"
            @drop.prevent="handleDocDrop"
            @click="triggerDocInput"
          >
            <input
              ref="docInputRef"
              type="file"
              accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              class="file-input-hidden"
              @change="handleDocSelect"
            />

            <template v-if="!docFile">
              <div class="upload-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                  <line x1="16" y1="13" x2="8" y2="13"/>
                  <line x1="16" y1="17" x2="8" y2="17"/>
                </svg>
              </div>
              <p class="upload-text">点击或拖拽 Word 文档到此处</p>
              <p class="upload-hint">支持 .docx 格式，最大 20MB</p>
            </template>

            <template v-else>
              <div class="file-preview">
                <div class="doc-icon">W</div>
                <div class="file-info">
                  <span class="file-name">{{ docFile.name }}</span>
                  <span class="file-size">{{ formatSize(docFile.size) }}</span>
                </div>
              </div>
            </template>
          </div>

          <div class="action-buttons" v-if="docFile">
            <button class="btn btn-secondary" @click="clearDocFile">
              重新选择
            </button>
            <button
              class="btn btn-success"
              :disabled="isDocPreviewing || isDocUploading"
              @click="previewDocHandler('word')"
            >
              <div v-if="isDocPreviewing" class="spinner-sm"></div>
              {{ isDocPreviewing ? '解析中...' : '解析文档' }}
            </button>
          </div>
        </template>

        <!-- ===== PDF 文档导入 ===== -->
        <template v-if="uploadMode === 'pdf'">
          <div
            class="upload-zone"
            :class="{ 'has-file': docFile }"
            @dragover.prevent="isDocDragOver = true"
            @dragleave="isDocDragOver = false"
            @drop.prevent="handleDocDrop"
            @click="triggerDocInput"
          >
            <input
              ref="docInputRef"
              type="file"
              accept=".pdf,application/pdf"
              class="file-input-hidden"
              @change="handleDocSelect"
            />

            <template v-if="!docFile">
              <div class="upload-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                  <line x1="16" y1="13" x2="8" y2="13"/>
                  <line x1="16" y1="17" x2="8" y2="17"/>
                </svg>
              </div>
              <p class="upload-text">点击或拖拽 PDF 文档到此处</p>
              <p class="upload-hint">支持 .pdf 格式，最大 20MB</p>
            </template>

            <template v-else>
              <div class="file-preview">
                <div class="doc-icon">P</div>
                <div class="file-info">
                  <span class="file-name">{{ docFile.name }}</span>
                  <span class="file-size">{{ formatSize(docFile.size) }}</span>
                </div>
              </div>
            </template>
          </div>

          <div class="action-buttons" v-if="docFile">
            <button class="btn btn-secondary" @click="clearDocFile">
              重新选择
            </button>
            <button
              class="btn btn-success"
              :disabled="isDocPreviewing || isDocUploading"
              @click="previewDocHandler('pdf')"
            >
              <div v-if="isDocPreviewing" class="spinner-sm"></div>
              {{ isDocPreviewing ? '解析中...' : '解析文档' }}
            </button>
          </div>
        </template>

        <!-- 状态消息 -->
        <div v-if="statusMessage" class="status-message" :class="statusType">
          {{ statusMessage }}
        </div>
      </div>

      <!-- 右侧面板 -->
      <div class="right-panel">
        <!-- 右侧选项卡 -->
        <div class="panel-tabs">
          <button
            class="panel-tab"
            :class="{ active: rightTab === 'result' }"
            @click="rightTab = 'result'"
          >
            识别结果
          </button>
          <button
            class="panel-tab"
            :class="{ active: rightTab === 'history' }"
            @click="rightTab = 'history'; loadRecords()"
          >
            上传历史
            <span v-if="recordTotal" class="tab-badge">{{ recordTotal }}</span>
          </button>
        </div>

        <!-- 识别结果 -->
        <div v-if="rightTab === 'result'" class="result-panel">
          <div class="result-header">
            <h3 class="result-title">识别 / 解析结果</h3>
            <span v-if="selectedBatchItem" class="batch-editing-tag" :title="selectedBatchItem.file.name">{{ selectedBatchItem.file.name }}</span>
            <span v-if="ocrText" class="char-count">{{ ocrText.length }} 字</span>
          </div>

          <div v-if="!ocrText && docPages.length === 0 && !isPreviewing && !isSaving" class="result-empty">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
            <p>上传图片或文档后查看提取结果</p>
          </div>

          <div v-else-if="isPreviewing" class="result-loading">
            <div class="spinner"></div>
            <p>正在提取文字...</p>
          </div>

          <div v-else class="result-content">
            <!-- ===== 文档分页预览 ===== -->
            <template v-if="docPages.length > 0">
              <div class="doc-pages-toolbar">
                <span class="doc-pages-info">
                  共 {{ docPreviewInfo.total_pages }} 页，已保存 {{ savedPageNumbers.size }} 页
                  <span v-if="docPreviewInfo.pdf_preview_path && docPreviewInfo.doc_type === 'word'" class="doc-preview-hint">（Word 已生成 PDF 预览）</span>
                </span>
                <div class="doc-pages-actions">
                  <button class="btn btn-sm btn-outline" @click="selectAllPages">全选未保存</button>
                  <button class="btn btn-sm btn-outline" @click="clearPageSelection">清空选择</button>
                  <button
                    class="btn btn-sm btn-success"
                    :disabled="selectedPages.size === 0 || isDocUploading"
                    @click="saveSelectedDocPages"
                  >
                    <span v-if="isDocUploading" class="spinner-xs"></span>
                    {{ isDocUploading ? `保存中 ${docSaveProgress.saved}/${docSaveProgress.total}...` : `保存选中页 (${selectedPages.size})` }}
                  </button>
                </div>
              </div>

              <div class="doc-pages-list">
                <div
                  v-for="page in docPages"
                  :key="page.page_number"
                  class="doc-page-card"
                  :class="{
                    'is-selected': selectedPages.has(page.page_number),
                    'is-saved': savedPageNumbers.has(page.page_number),
                  }"
                >
                  <div class="doc-page-header">
                    <label class="doc-page-checkbox">
                      <input
                        type="checkbox"
                        :checked="selectedPages.has(page.page_number)"
                        :disabled="savedPageNumbers.has(page.page_number)"
                        @change="togglePageSelected(page.page_number)"
                      />
                      <span class="doc-page-title">第 {{ page.page_number }} 页</span>
                      <span v-if="savedPageNumbers.has(page.page_number)" class="doc-page-saved-tag">已保存</span>
                    </label>
                    <div class="doc-page-actions">
                      <a
                        v-if="getDocumentPageLink(page)"
                        :href="getDocumentPageLink(page)"
                        target="_blank"
                        class="btn btn-sm btn-outline"
                      >
                        预览
                      </a>
                      <button
                        class="btn btn-sm btn-primary"
                        :disabled="polishingPageNumbers.has(page.page_number) || savedPageNumbers.has(page.page_number)"
                        @click="polishPageHandler(page)"
                      >
                        <span v-if="polishingPageNumbers.has(page.page_number)" class="spinner-xs"></span>
                        {{ polishingPageNumbers.has(page.page_number) ? '润色中...' : '一键润色' }}
                      </button>
                    </div>
                  </div>

                  <div class="doc-page-body">
                    <textarea
                      v-model="page.text"
                      class="doc-page-textarea"
                      rows="6"
                      :disabled="savedPageNumbers.has(page.page_number)"
                    ></textarea>
                  </div>
                </div>
              </div>
            </template>

            <!-- ===== 单图片 OCR 结果 ===== -->
            <template v-else>
              <!-- 可编辑的 OCR 文本 -->
              <div v-if="ocrTextEditMode" class="ocr-edit-area">
                <textarea
                  v-model="ocrText"
                  class="ocr-textarea"
                  rows="10"
                ></textarea>
                <div class="ocr-edit-actions">
                  <button class="btn btn-sm btn-secondary" @click="exitEditMode">
                    完成编辑
                  </button>
                </div>
              </div>
              <!-- 只读展示（支持 LaTeX 公式渲染） -->
              <div v-else class="ocr-text-display" v-html="renderedOcrText"></div>
              <!-- 操作栏 -->
              <div v-if="ocrText" class="ocr-actions-bar">
                <button class="btn btn-sm btn-outline" @click="toggleEditMode">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                  </svg>
                  {{ ocrTextEditMode ? '退出编辑' : '编辑结果' }}
                </button>
                <button class="btn btn-sm btn-primary" @click="handlePolish" :disabled="isPolishing">
                  <span v-if="isPolishing" class="spinner-xs"></span>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <polyline points="23 4 23 10 17 10"/>
                    <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                  </svg>
                  {{ isPolishing ? '润色中...' : '一键润色' }}
                </button>
              </div>
            </template>
          </div>
        </div>

        <!-- 上传历史 -->
        <div v-if="rightTab === 'history'" class="history-panel">
          <div v-if="isLoadingRecords" class="result-loading">
            <div class="spinner"></div>
            <p>加载上传记录...</p>
          </div>

          <div v-else-if="records.length === 0" class="result-empty">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="32" height="32">
              <rect x="3" y="3" width="18" height="18" rx="2"/>
              <line x1="9" y1="9" x2="15" y2="9"/>
              <line x1="9" y1="13" x2="15" y2="13"/>
              <line x1="9" y1="17" x2="12" y2="17"/>
            </svg>
            <p>暂无上传记录</p>
          </div>

          <div v-else class="history-list">
            <div
              v-for="rec in records"
              :key="rec.id"
              class="history-item"
              :class="{ expanded: expandedRecordId === rec.id }"
            >
              <div class="history-item-header" @click="toggleRecordExpand(rec.id)">
                <div class="hi-info">
                  <span class="hi-filename">{{ rec.filename }}</span>
                  <span class="hi-date">{{ formatDate(rec.created_at) }}</span>
                </div>
                <div class="hi-meta">
                  <span class="hi-source-type" :class="rec.source_type">{{ sourceTypeLabel(rec.source_type) }}</span>
                  <span class="hi-chunks">{{ rec.chunk_count }} 段</span>
                  <span class="hi-expand-arrow">{{ expandedRecordId === rec.id ? '&#9660;' : '&#9654;' }}</span>
                </div>
              </div>

              <div v-if="expandedRecordId === rec.id" class="history-item-body">
                <div class="hi-ocr-text" :class="{ editing: editingRecordId === rec.id }">
                  <template v-if="editingRecordId === rec.id">
                    <textarea
                      v-model="editOcrText"
                      class="edit-textarea"
                      rows="6"
                    ></textarea>
                    <div class="edit-actions">
                      <button class="btn btn-sm btn-secondary" @click="cancelEdit(rec.id)">
                        取消
                      </button>
                      <button class="btn btn-sm btn-primary" @click="saveEdit(rec.id)">
                        保存修改
                      </button>
                    </div>
                  </template>
                  <template v-else>
                    <pre class="hi-text-pre">{{ rec.ocr_text }}</pre>
                  </template>
                </div>
                <div class="hi-actions" v-if="editingRecordId !== rec.id">
                  <button class="btn btn-sm btn-outline" @click="startEdit(rec)">
                    编辑结果
                  </button>
                  <button class="btn btn-sm btn-outline-danger" @click="handleDeleteRecord(rec.id)">
                    删除
                  </button>
                </div>
              </div>
            </div>

            <!-- 加载更多 -->
            <div v-if="records.length < recordTotal" class="load-more">
              <button class="btn btn-outline" @click="loadMoreRecords">
                加载更多 ({{ records.length }}/{{ recordTotal }})
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 图片裁剪弹窗 -->
    <div v-if="cropModalVisible" class="crop-modal" @click.self="closeCropModal">
      <div class="crop-modal-content">
        <div class="crop-modal-header">
          <h3 class="crop-modal-title">裁剪图片</h3>
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

    <!-- 上传图片预览弹窗：展示最终上传给 OCR 的图片实际效果 -->
    <div v-if="uploadPreviewVisible" class="crop-modal" @click.self="closeUploadPreview">
      <div class="crop-modal-content">
        <div class="crop-modal-header">
          <h3 class="crop-modal-title">上传预览 · OCR 实际接收的图片</h3>
          <button class="crop-modal-close" @click="closeUploadPreview" aria-label="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
        <div class="crop-modal-body upload-preview-body" @wheel.prevent="handlePreviewWheel">
          <img
            :src="filePreviewUrl"
            class="upload-preview-img"
            :style="{ transform: 'scale(' + previewZoom + ')' }"
            alt="上传预览"
          />
        </div>
        <div class="crop-modal-footer">
          <span class="preview-meta">
            {{ selectedFile?.name }} · {{ formatSize(selectedFile?.size || 0) }} · 缩放 {{ Math.round(previewZoom * 100) }}%（滚轮可缩放）
          </span>
          <button class="btn btn-secondary" @click="previewZoom = 1">重置缩放</button>
          <button class="btn btn-primary" @click="closeUploadPreview">关闭</button>
        </div>
      </div>
    </div>

    <!-- 新建知识库弹窗 -->
    <div v-if="showCreateKbModal" class="kb-overlay" @click.self="showCreateKbModal = false">
      <div class="kb-modal">
        <div class="kb-modal-header">
          <h3>新建知识库</h3>
          <button class="kb-modal-close" @click="showCreateKbModal = false" aria-label="关闭">&times;</button>
        </div>
        <div class="kb-modal-body">
          <div class="kb-form-group">
            <label class="kb-form-label">知识库名称</label>
            <input v-model="newKb.name" class="kb-input" placeholder="如：糖尿病饮食管理" />
          </div>
          <div class="kb-form-group">
            <label class="kb-form-label">可见范围</label>
            <div class="kb-vis-options">
              <label class="kb-vis-option">
                <input type="radio" v-model="newKb.visibility" value="private" />
                <span>个人（仅自己可见）</span>
              </label>
              <label class="kb-vis-option" :class="{ disabled: !kbIsAdmin }">
                <input type="radio" v-model="newKb.visibility" value="public" :disabled="!kbIsAdmin" />
                <span>公共（所有人可见）</span>
                <span v-if="!kbIsAdmin" class="kb-vis-hint">仅管理员可建</span>
              </label>
            </div>
          </div>
        </div>
        <div class="kb-modal-footer">
          <button class="btn btn-secondary" @click="showCreateKbModal = false">取消</button>
          <button class="btn btn-primary" :disabled="creatingKb" @click="submitCreateKb">
            {{ creatingKb ? '创建中...' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import Cropper from 'cropperjs/dist/cropper.esm.js'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import {
  uploadImage,
  uploadDocument,
  previewDocument,
  saveDocumentPagesStream,
  previewOCR,
  getUploadRecords,
  updateUploadRecord,
  deleteUploadRecord,
  polishOCR,
  listKnowledgeBases,
  createKnowledgeBase,
  getImageUrl as buildImageUrl,
} from '@/api'
import { useChatStore } from '@/stores/chat'
import { compressCanvasToFile, compressImageFile, MAX_UPLOAD_SIZE } from '@/utils/image'

const router = useRouter()
const store = useChatStore()

// ---- 单文件上传状态 ----
const selectedOCR = ref('aliyun')
const selectedFile = ref(null)
const filePreviewUrl = ref('')
const ocrText = ref('')
const isDragOver = ref(false)
const isPreviewing = ref(false)
const isSaving = ref(false)
const isProcessing = computed(() => isPreviewing.value || isSaving.value)
const statusMessage = ref('')
const statusType = ref('info')
const fileInputRef = ref(null)
const cameraInputRef = ref(null)

// ---- OCR 编辑 & 润色 ----
const ocrTextEditMode = ref(false)
const isPolishing = ref(false)

/**
 * 将混合文本中的 LaTeX 公式渲染为 HTML
 * 支持 $$...$$ 块级公式和 $...$ 行内公式
 */
function renderOcrText(text) {
  if (!text) return ''
  // 先处理块级公式 $$...$$
  let html = text.replace(/\$\$([\s\S]*?)\$\$/g, (match, latex) => {
    try {
      return katex.renderToString(latex.trim(), { displayMode: true, throwOnError: false })
    } catch (err) {
      console.error('KaTeX 块级渲染失败:', err)
      return match
    }
  })
  // 再处理行内公式 $...$
  html = html.replace(/\$([\s\S]*?)\$/g, (match, latex) => {
    try {
      return katex.renderToString(latex.trim(), { displayMode: false, throwOnError: false })
    } catch (err) {
      console.error('KaTeX 行内渲染失败:', err)
      return match
    }
  })
  // 将换行符转为 <br>
  html = html.replace(/\n/g, '<br>')
  return html
}

const renderedOcrText = computed(() => renderOcrText(ocrText.value))

// ---- 上传模块状态 ----
const uploadMode = ref('ocr')
const ocrSubMode = ref('single')

// ---- 文档上传状态 ----
const docInputRef = ref(null)
const docFile = ref(null)
const isDocDragOver = ref(false)
const isDocUploading = ref(false)
const isDocPreviewing = ref(false)
const docPreviewInfo = ref(null)
const docPages = ref([])
const selectedPages = ref(new Set())
const savedPageNumbers = ref(new Set())
const polishingPageNumbers = ref(new Set())
// 保存实时进度（SSE 逐页回传）
const docSaveProgress = ref({ saved: 0, total: 0 })

// 切换上传模块时清理残留状态
watch(uploadMode, (_newMode, oldMode) => {
  statusMessage.value = ''
  if (oldMode === 'word' || oldMode === 'pdf') {
    docFile.value = null
    ocrText.value = ''
    docPreviewInfo.value = null
    docPages.value = []
    selectedPages.value = new Set()
    savedPageNumbers.value = new Set()
    polishingPageNumbers.value = new Set()
  }
})

// 批量模式下，右侧结果的编辑实时写回当前选中的批量条目
watch(ocrText, (val) => {
  if (uploadMode.value === 'ocr' && ocrSubMode.value === 'batch' && selectedBatchItem.value) {
    selectedBatchItem.value.ocrText = val
  }
})

// 切换单张/批量时重置右侧结果关联
watch(ocrSubMode, () => {
  selectedBatchIndex.value = null
  ocrText.value = ''
})

// ---- 批量上传状态 ----
const batchInputRef = ref(null)
const batchCameraInputRef = ref(null)
const batchFiles = ref([])
const isBatchRunning = ref(false)
const batchDoneCount = ref(0)
const selectedBatchIndex = ref(null)

const selectedBatchItem = computed(() =>
  selectedBatchIndex.value != null ? batchFiles.value[selectedBatchIndex.value] || null : null
)
const recognizedBatchCount = computed(() =>
  batchFiles.value.filter((bf) => bf.status === 'recognized').length
)
const batchHasWork = computed(() =>
  batchFiles.value.some((bf) => bf.status === 'pending' || bf.status === 'error')
)

// ---- 图片裁剪 ----
const cropModalVisible = ref(false)
// 裁剪弹窗服务对象：{ type: 'single' } 单张模式 或 { type: 'batch', index } 批量条目
const cropTarget = ref(null)
const cropImageUrl = ref('')
const cropperInstance = ref(null)
const cropperImageRef = ref(null)
const cropperWrapperRef = ref(null)

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

function onWindowPointerUp(e) {
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
const fileBeforeCrop = ref(null)

// 已裁剪/压缩后待上传的 File
const pendingUploadFile = ref(null)

const batchTotalSize = computed(() =>
  batchFiles.value.reduce((sum, bf) => sum + bf.file.size, 0)
)

const isBatchCompressing = ref(false)

const batchProgressPercent = computed(() => {
  if (batchFiles.value.length === 0) return 0
  return Math.round((batchDoneCount.value / batchFiles.value.length) * 100)
})

// ---- 上传历史状态 ----
const rightTab = ref('result')
const records = ref([])
const recordTotal = ref(0)
const isLoadingRecords = ref(false)
const expandedRecordId = ref(null)
const editingRecordId = ref(null)
const editOcrText = ref('')
const recordsOffset = ref(0)
const RECORDS_PAGE_SIZE = 20

// ---- OCR 选项 ----
const ocrOptions = [
  { value: 'local', label: '本地 OCR (EasyOCR)' },
  { value: 'aliyun', label: '阿里云 OCR' },
  { value: 'custom_api', label: '自定义 OCR API' },
]

// ---- 目标知识库 ----
const kbList = ref([])
const kbIsAdmin = ref(false)
const selectedKbId = ref('')
// 先按公共/个人筛选，再按名称选择知识库
const kbVisibilityFilter = ref('public')

// ---- 保存标题 ----
const saveTitle = ref('')

// 选择文件时自动将默认标题设为文件名（不含扩展名）
function autoFillTitle(file) {
  if (!file) return
  const name = file.name || ''
  const dotIndex = name.lastIndexOf('.')
  saveTitle.value = dotIndex > 0 ? name.slice(0, dotIndex) : name
}

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
    // 筛选器跟随当前选中知识库的可见性
    const selected = kbList.value.find((k) => k.id === selectedKbId.value)
    if (selected) kbVisibilityFilter.value = selected.visibility
    syncVisibilityFromKb()
  } catch (e) {
    console.error('加载知识库列表失败:', e)
  }
}

// 切换公共/个人筛选：当前选中项不在筛选结果中时自动选第一个
function onKbFilterChange() {
  if (!filteredKbList.value.some((kb) => kb.id === selectedKbId.value)) {
    selectedKbId.value = filteredKbList.value[0]?.id || ''
  }
  syncVisibilityFromKb()
}

// 可见性由所属知识库决定（兼容原上传接口的 visibility 参数）
function syncVisibilityFromKb() {
  const kb = kbList.value.find((k) => k.id === selectedKbId.value)
  visibility.value = kb ? kb.visibility : 'public'
}

function onKbChange() {
  syncVisibilityFromKb()
}

const showCreateKbModal = ref(false)
const creatingKb = ref(false)
// topic 不再由用户选择，后端要求必传时默认归入“其他”
const newKb = reactive({ name: '', topic: '其他', visibility: 'private' })

function openCreateKbModal() {
  newKb.name = ''
  newKb.visibility = kbIsAdmin.value ? 'public' : 'private'
  showCreateKbModal.value = true
}

async function submitCreateKb() {
  if (!newKb.name.trim()) {
    alert('请输入知识库名称')
    return
  }
  creatingKb.value = true
  try {
    const res = await createKnowledgeBase({ ...newKb, name: newKb.name.trim() })
    showCreateKbModal.value = false
    await loadKnowledgeBases()
    selectedKbId.value = res.data.kb.id
    kbVisibilityFilter.value = res.data.kb.visibility || 'private'
    syncVisibilityFromKb()
  } catch (e) {
    alert('创建知识库失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    creatingKb.value = false
  }
}

// 保留 visibility 状态：由目标知识库同步
const visibility = ref('public')

// ====== 单文件上传 ======

function triggerFileInput() {
  fileInputRef.value?.click()
}

// 移动端拍照入口：capture input 直接唤起相机（桌面端退化为文件选择）
function triggerCameraInput() {
  cameraInputRef.value?.click()
}

function handleFileSelect(e) {
  const files = e.target.files
  if (files && files.length > 0) {
    setFile(files[0])
  }
}

function handleDrop(e) {
  isDragOver.value = false
  const files = e.dataTransfer.files
  if (files && files.length > 0) {
    setFile(files[0])
  }
}

function setFile(file) {
  if (!file.type.startsWith('image/')) {
    showStatus('请上传图片文件', 'error')
    return
  }
  fileBeforeCrop.value = file
  autoFillTitle(file)
  cropImageUrl.value = ''
  cropTarget.value = { type: 'single' }
  cropModalVisible.value = true
  pendingUploadFile.value = null

  const reader = new FileReader()
  reader.onload = (e) => {
    cropImageUrl.value = e.target.result
    nextTick(() => initCropper())
  }
  reader.readAsDataURL(file)
}

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

// 当前目标旋转角度：0° 为图片原始方向，正值顺时针，负值逆时针，范围 -360°~360°
const targetAngle = ref(0)

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
    cropperImage.$rotate(delta + 'deg')
  }
  targetAngle.value = normalizeRotation(newAngle)
}

function rotateCropImage(deg) {
  // 按钮：每次相对当前角度顺时针/逆时针旋转 90°（与系统预览、相册一致）
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

// ---- 上传图片预览（查看最终传给 OCR 的实际图片） ----
const uploadPreviewVisible = ref(false)
const previewZoom = ref(1)

function openUploadPreview() {
  if (!selectedFile.value || !filePreviewUrl.value) return
  previewZoom.value = 1
  uploadPreviewVisible.value = true
}

function closeUploadPreview() {
  uploadPreviewVisible.value = false
}

function handlePreviewWheel(e) {
  const delta = e.deltaY > 0 ? -0.1 : 0.1
  previewZoom.value = Math.min(5, Math.max(0.2, +(previewZoom.value + delta).toFixed(2)))
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
    if (cropTarget.value?.type === 'batch') {
      applyBatchProcessedFile(file, message)
    } else {
      pendingUploadFile.value = file
      selectedFile.value = file
      filePreviewUrl.value = await readFileAsDataURL(file)
      ocrText.value = ''
      statusMessage.value = ''
      showStatus(message, 'success')
    }
    closeCropModal()
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
    if (cropTarget.value?.type === 'batch') {
      applyBatchProcessedFile(file, file.size < originalSize ? `图片已压缩至 ${formatSize(file.size)}` : '已使用原图')
    } else {
      pendingUploadFile.value = file
      selectedFile.value = file
      filePreviewUrl.value = await readFileAsDataURL(file)
      ocrText.value = ''
      statusMessage.value = ''
      if (file.size < originalSize) {
        showStatus(`图片已压缩至 ${formatSize(file.size)}`, 'success')
      }
    }
    closeCropModal()
  } catch (err) {
    console.error('压缩失败:', err)
    showStatus('图片处理失败: ' + (err.message || '请重试'), 'error')
  }
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = (e) => resolve(e.target.result)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

function clearFile() {
  selectedFile.value = null
  filePreviewUrl.value = ''
  ocrText.value = ''
  statusMessage.value = ''
  pendingUploadFile.value = null
  fileBeforeCrop.value = null
  saveTitle.value = ''
}

async function previewOCRHandler() {
  if (!selectedFile.value) return
  isPreviewing.value = true
  statusMessage.value = ''

  try {
    const res = await previewOCR(selectedFile.value, selectedOCR.value)
    ocrText.value = res.data.ocr_text || ''
    if (!ocrText.value) {
      showStatus('未识别到文字，请检查图片质量', 'warning')
    }
  } catch (err) {
    const status = err.response?.status
    const detail = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : ''
    const msg = detail || (status === 413
      ? '图片过大被服务器拒绝，请压缩后重试'
      : 'OCR 识别失败，请检查配置')
    showStatus(msg, 'error')
    ocrText.value = ''
  } finally {
    isPreviewing.value = false
  }
}

async function processAndSave() {
  if (!selectedFile.value || !ocrText.value) return
  isSaving.value = true
  statusMessage.value = ''

  try {
    const res = await uploadImage(selectedFile.value, selectedOCR.value, visibility.value, selectedKbId.value, ocrText.value, saveTitle.value)
    const data = res.data

    if (data.success) {
      showStatus(
        `已保存到知识库！识别 ${data.chunk_count} 段文本`,
        'success'
      )
      setTimeout(() => {
        clearFile()
      }, 1500)
    } else {
      showStatus('未识别到文字内容', 'warning')
    }
  } catch (err) {
    const msg = err.response?.data?.detail || '保存失败'
    showStatus(msg, 'error')
  } finally {
    isSaving.value = false
  }
}

// ====== OCR 文本编辑 & 润色 ======

function toggleEditMode() {
  ocrTextEditMode.value = !ocrTextEditMode.value
}

function exitEditMode() {
  ocrTextEditMode.value = false
}

async function handlePolish() {
  if (!ocrText.value || isPolishing.value) return
  isPolishing.value = true
  try {
    const res = await polishOCR(ocrText.value)
    if (res.data.success && res.data.polished_text) {
      ocrText.value = res.data.polished_text
      showStatus('润色完成，句子已连接通顺', 'success')
    } else {
      showStatus(res.data.message || '润色失败', 'warning')
    }
  } catch (err) {
    const msg = err.response?.data?.detail || '润色请求失败'
    showStatus(msg, 'error')
  } finally {
    isPolishing.value = false
  }
}

// ====== 批量上传 ======

function triggerBatchInput() {
  if (!isBatchRunning.value) {
    batchInputRef.value?.click()
  }
}

function triggerBatchCamera() {
  if (!isBatchRunning.value) {
    batchCameraInputRef.value?.click()
  }
}

// 拍照结果追加到待识别列表（不清空已选文件），拍完重置 input 以便连续拍摄
function handleBatchCameraSelect(e) {
  const files = Array.from(e.target.files || [])
  for (const f of files) {
    if (!f.type.startsWith('image/')) continue
    batchFiles.value.push({
      file: f,
      status: 'pending',
      chunk_count: 0,
      error: '',
      ocrText: '',
    })
  }
  e.target.value = ''
}

async function handleBatchSelect(e) {
  const rawFiles = Array.from(e.target.files || [])
  batchFiles.value = rawFiles.map((f) => ({
    file: f,
    status: 'pending',
    chunk_count: 0,
    error: '',
    ocrText: '',
  }))
  batchDoneCount.value = 0
  selectedBatchIndex.value = null
  ocrText.value = ''
  statusMessage.value = ''
  isBatchCompressing.value = true
  await compressBatchFiles()
  isBatchCompressing.value = false
}

async function compressBatchFiles() {
  if (batchFiles.value.length === 0) return
  for (const bf of batchFiles.value) {
    if (bf.file.size > MAX_UPLOAD_SIZE) {
      try {
        bf.file = await compressImageFile(bf.file, MAX_UPLOAD_SIZE)
      } catch (err) {
        console.error('批量压缩失败:', err)
        bf.error = '压缩失败'
        bf.status = 'error'
      }
    }
  }
}

function clearBatch() {
  batchFiles.value = []
  batchDoneCount.value = 0
  isBatchRunning.value = false
  selectedBatchIndex.value = null
  ocrText.value = ''
  statusMessage.value = ''
}

// 裁剪/压缩后的图片写回批量条目（图片已变化，需重新识别）
function applyBatchProcessedFile(file, message) {
  const idx = cropTarget.value?.index
  const bf = idx != null ? batchFiles.value[idx] : null
  if (!bf) return
  bf.file = file
  bf.status = 'pending'
  bf.ocrText = ''
  bf.error = ''
  bf.chunk_count = 0
  if (selectedBatchIndex.value === idx) {
    selectedBatchIndex.value = null
    ocrText.value = ''
  }
  showStatus(message, 'success')
}

// 复用单张模式的裁剪弹窗，编辑指定批量条目
function openBatchCrop(idx) {
  const bf = batchFiles.value[idx]
  if (!bf || isBatchRunning.value) return
  fileBeforeCrop.value = bf.file
  cropImageUrl.value = ''
  cropTarget.value = { type: 'batch', index: idx }
  cropModalVisible.value = true
  const reader = new FileReader()
  reader.onload = (e) => {
    cropImageUrl.value = e.target.result
    nextTick(() => initCropper())
  }
  reader.readAsDataURL(bf.file)
}

// 批量识别：仅调用预览 OCR，不入库；结果留在列表供查看/编辑
async function startBatchRecognize() {
  if (batchFiles.value.length === 0 || isBatchRunning.value) return

  isBatchRunning.value = true
  statusMessage.value = ''
  batchDoneCount.value = batchFiles.value.filter(
    (bf) => bf.status === 'recognized' || bf.status === 'uploaded'
  ).length

  try {
    for (let i = 0; i < batchFiles.value.length; i++) {
      const bf = batchFiles.value[i]
      if (bf.status !== 'pending' && bf.status !== 'error') continue
      bf.status = 'recognizing'
      bf.error = ''
      try {
        const res = await previewOCR(bf.file, selectedOCR.value)
        const text = res.data.ocr_text || ''
        if (text) {
          bf.ocrText = text
          bf.status = 'recognized'
        } else {
          bf.status = 'error'
          bf.error = '未识别到文字'
        }
      } catch (err) {
        bf.status = 'error'
        bf.error = err.response?.data?.detail || err.message || '识别失败'
      }
      batchDoneCount.value = batchFiles.value.filter(
        (b) => b.status !== 'pending' && b.status !== 'recognizing'
      ).length
    }

    const okCount = batchFiles.value.filter(
      (bf) => bf.status === 'recognized' || bf.status === 'uploaded'
    ).length
    const errCount = batchFiles.value.filter((bf) => bf.status === 'error').length
    if (errCount > 0) {
      showStatus(`批量识别完成：${okCount} 成功，${errCount} 失败，点击列表项查看结果`, 'warning')
    } else {
      showStatus(`全部 ${okCount} 个文件识别完成！点击列表项在右侧编辑结果，确认后上传`, 'success')
    }
    // 自动选中第一个已识别条目，便于立即在右侧查看/编辑
    const firstIdx = batchFiles.value.findIndex((bf) => bf.status === 'recognized')
    if (firstIdx >= 0) {
      selectedBatchIndex.value = firstIdx
      ocrText.value = batchFiles.value[firstIdx].ocrText || ''
      rightTab.value = 'result'
    }
  } finally {
    isBatchRunning.value = false
  }
}

// 点击批量列表项：在右侧查看/编辑该条目的识别结果
function selectBatchItem(idx) {
  const bf = batchFiles.value[idx]
  if (!bf || isBatchRunning.value) return
  if (bf.status === 'recognized' || bf.status === 'uploaded') {
    selectedBatchIndex.value = idx
    ocrText.value = bf.ocrText || ''
    rightTab.value = 'result'
  } else if (bf.status === 'error') {
    showStatus('该图片识别失败：' + (bf.error || '未知错误'), 'warning')
  }
}

// 单条上传入库（携带右侧编辑后的文本，跳过重复 OCR）
async function uploadBatchItem(idx) {
  const bf = batchFiles.value[idx]
  if (!bf || bf.status !== 'recognized' || !bf.ocrText) return
  bf.status = 'uploading'
  try {
    const res = await uploadImage(bf.file, selectedOCR.value, visibility.value, selectedKbId.value, bf.ocrText, saveTitle.value)
    if (res.data.success) {
      bf.status = 'uploaded'
      bf.chunk_count = res.data.chunk_count || 0
      showStatus(`「${bf.file.name}」已保存到知识库（${bf.chunk_count} 段）`, 'success')
      if (rightTab.value === 'history') {
        loadRecords()
      }
    } else {
      bf.status = 'recognized'
      showStatus('未识别到文字内容', 'warning')
    }
  } catch (err) {
    bf.status = 'recognized'
    showStatus('上传失败: ' + (err.response?.data?.detail || err.message), 'error')
  }
}

// 一键上传全部已识别条目
async function uploadAllRecognized() {
  const idxList = batchFiles.value
    .map((bf, i) => (bf.status === 'recognized' ? i : -1))
    .filter((i) => i >= 0)
  if (idxList.length === 0) return
  isBatchRunning.value = true
  try {
    for (const i of idxList) {
      await uploadBatchItem(i)
    }
    const uploaded = batchFiles.value.filter((bf) => bf.status === 'uploaded').length
    showStatus(`批量上传完成：${uploaded}/${batchFiles.value.length} 条已入库`, 'success')
  } finally {
    isBatchRunning.value = false
  }
}

// ====== 文档上传 ======

function triggerDocInput() {
  docInputRef.value?.click()
}

function handleDocSelect(e) {
  const files = e.target.files
  if (files && files.length > 0) {
    setDocFile(files[0])
  }
}

function handleDocDrop(e) {
  isDocDragOver.value = false
  const files = e.dataTransfer.files
  if (files && files.length > 0) {
    setDocFile(files[0])
  }
}

function setDocFile(file) {
  const expectedExt = uploadMode.value === 'word' ? '.docx' : '.pdf'
  const expectedType = uploadMode.value === 'word'
    ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    : 'application/pdf'
  const extOk = file.name.toLowerCase().endsWith(expectedExt)
  const typeOk = file.type === expectedType
  if (!extOk && !typeOk) {
    showStatus(`请上传 ${expectedExt} 文件`, 'error')
    return
  }
  docFile.value = file
  autoFillTitle(file)
  ocrText.value = ''
  statusMessage.value = ''
}

function clearDocFile() {
  docFile.value = null
  ocrText.value = ''
  statusMessage.value = ''
  saveTitle.value = ''
  docPreviewInfo.value = null
  docPages.value = []
  selectedPages.value = new Set()
  savedPageNumbers.value = new Set()
  polishingPageNumbers.value = new Set()
}

async function previewDocHandler(docType) {
  if (!docFile.value) return
  isDocPreviewing.value = true
  statusMessage.value = ''
  try {
    const res = await previewDocument(docFile.value, docType)
    const data = res.data
    docPreviewInfo.value = {
      filename: data.filename,
      file_path: data.file_path,
      doc_type: data.doc_type,
      total_pages: data.total_pages,
      pdf_preview_path: data.pdf_preview_path,
    }
    docPages.value = (data.pages || []).map((p) => ({ ...p, _dirty: false }))
    selectedPages.value = new Set()
    savedPageNumbers.value = new Set()
    if (!docPages.value.length) {
      showStatus('未提取到文字内容', 'warning')
    } else {
      rightTab.value = 'result'
      showStatus(`已解析 ${data.total_pages} 页，请选择要保存的页面`, 'success')
    }
  } catch (err) {
    const msg = err.response?.data?.detail || '文档解析失败'
    showStatus(msg, 'error')
  } finally {
    isDocPreviewing.value = false
  }
}

async function saveSelectedDocPages() {
  if (!docPreviewInfo.value || selectedPages.value.size === 0) return
  isDocUploading.value = true
  statusMessage.value = ''
  const pagesToSave = docPages.value
    .filter((p) => selectedPages.value.has(p.page_number))
    .map((p) => ({ page_number: p.page_number, text: p.text }))

  docSaveProgress.value = { saved: 0, total: pagesToSave.length }
  const errors = []
  let doneInfo = null

  try {
    // SSE 流式保存：每保存完一页立即回传，界面实时更新进度
    await saveDocumentPagesStream(
      {
        file_path: docPreviewInfo.value.file_path,
        doc_type: docPreviewInfo.value.doc_type,
        visibility: visibility.value,
        kb_id: selectedKbId.value,
        title: saveTitle.value,
        pages: pagesToSave,
      },
      (ev) => {
        if (ev.event === 'page_saved') {
          // 实时标记该页已保存，顶部计数与每页标签同步更新
          savedPageNumbers.value.add(ev.page_number)
          docSaveProgress.value = { saved: ev.saved, total: ev.total }
        } else if (ev.event === 'page_error') {
          errors.push(`第 ${ev.page_number || '?'} 页: ${ev.error}`)
        } else if (ev.event === 'save_done') {
          doneInfo = ev
        }
      }
    )

    if (doneInfo && doneInfo.success) {
      selectedPages.value = new Set()
      if (errors.length) {
        showStatus(`已保存 ${doneInfo.chunk_count} 段；${errors.length} 页失败（${errors[0]}）`, 'warning')
      } else {
        showStatus(`已保存 ${doneInfo.chunk_count} 段文本到知识库`, 'success')
      }
      loadRecords()
      // 全部页都已保存后清除文件
      if (savedPageNumbers.value.size === docPages.value.length) {
        docFile.value = null
      }
    } else if (errors.length) {
      showStatus(`保存失败：${errors[0]}`, 'error')
    } else {
      showStatus('未保存到知识库', 'warning')
    }
  } catch (err) {
    const msg = err.response?.data?.detail || err.message || '保存失败'
    showStatus(msg, 'error')
  } finally {
    isDocUploading.value = false
  }
}

async function polishPageHandler(page) {
  if (!page.text.trim()) return
  polishingPageNumbers.value.add(page.page_number)
  try {
    const res = await polishOCR({ ocr_text: page.text })
    page.text = res.data.polished_text || page.text
    page._dirty = true
  } catch (err) {
    const msg = err.response?.data?.detail || '润色失败'
    showStatus(msg, 'error')
  } finally {
    polishingPageNumbers.value.delete(page.page_number)
  }
}

function togglePageSelected(pageNumber) {
  const next = new Set(selectedPages.value)
  if (next.has(pageNumber)) {
    next.delete(pageNumber)
  } else {
    next.add(pageNumber)
  }
  selectedPages.value = next
}

function selectAllPages() {
  selectedPages.value = new Set(
    docPages.value
      .filter((p) => !savedPageNumbers.value.has(p.page_number))
      .map((p) => p.page_number)
  )
}

function clearPageSelection() {
  selectedPages.value = new Set()
}

function getDocumentPageLink(page) {
  if (!docPreviewInfo.value || !docPreviewInfo.value.pdf_preview_path) return null
  const filename = docPreviewInfo.value.pdf_preview_path.split('/').pop()
  // 带 token 访问受保护文件，#page= 定位到对应页
  return `${buildImageUrl(filename)}#page=${page.page_number}`
}

// ====== 上传历史 ======

async function loadRecords() {
  isLoadingRecords.value = true
  try {
    const res = await getUploadRecords(RECORDS_PAGE_SIZE, 0)
    records.value = res.data.records || []
    recordTotal.value = res.data.total || 0
    recordsOffset.value = records.value.length
  } catch (err) {
    console.error('加载上传记录失败:', err)
  } finally {
    isLoadingRecords.value = false
  }
}

async function loadMoreRecords() {
  isLoadingRecords.value = true
  try {
    const res = await getUploadRecords(RECORDS_PAGE_SIZE, recordsOffset.value)
    const newRecords = res.data.records || []
    records.value = records.value.concat(newRecords)
    recordsOffset.value = records.value.length
  } catch (err) {
    console.error('加载更多记录失败:', err)
  } finally {
    isLoadingRecords.value = false
  }
}

function toggleRecordExpand(recordId) {
  expandedRecordId.value = expandedRecordId.value === recordId ? null : recordId
  // 如果收起，同时取消编辑状态
  if (expandedRecordId.value !== recordId) {
    editingRecordId.value = null
    editOcrText.value = ''
  }
}

function startEdit(rec) {
  editingRecordId.value = rec.id
  editOcrText.value = rec.ocr_text
}

function cancelEdit(recordId) {
  editingRecordId.value = null
  editOcrText.value = ''
}

async function saveEdit(recordId) {
  try {
    await updateUploadRecord(recordId, editOcrText.value)
    // 更新本地记录
    const rec = records.value.find((r) => r.id === recordId)
    if (rec) {
      rec.ocr_text = editOcrText.value
    }
    editingRecordId.value = null
    showStatus('OCR 文本已更新', 'success')
  } catch (err) {
    const msg = err.response?.data?.detail || '保存失败'
    showStatus(msg, 'error')
  }
}

async function handleDeleteRecord(recordId) {
  try {
    await deleteUploadRecord(recordId)
    records.value = records.value.filter((r) => r.id !== recordId)
    if (recordTotal.value > 0) recordTotal.value--
    if (expandedRecordId.value === recordId) {
      expandedRecordId.value = null
    }
    showStatus('记录已删除', 'success')
  } catch (err) {
    const msg = err.response?.data?.detail || '删除失败'
    showStatus(msg, 'error')
  }
}

// ====== 工具函数 ======

function showStatus(msg, type) {
  statusMessage.value = msg
  statusType.value = type
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + 'B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB'
  return (bytes / (1024 * 1024)).toFixed(1) + 'MB'
}

function formatDate(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function sourceTypeLabel(type) {
  const map = {
    image: '图片',
    word: 'Word',
    pdf: 'PDF',
  }
  return map[type] || type || '未知'
}

onMounted(() => {
  // 默认使用阿里云 OCR
  selectedOCR.value = 'aliyun'
  loadKnowledgeBases()
})
</script>

<style scoped>
.upload-view {
  height: 100vh;
  overflow-y: auto;
  padding: 32px 40px;
  max-width: 1200px;
  margin: 0 auto;
}

/* ---- 目标知识库选择 ---- */
.kb-selector-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.kb-select {
  flex: 1;
  padding: 9px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: white;
  outline: none;
  cursor: pointer;
}

.kb-select:focus {
  border-color: var(--color-primary);
}

.btn-new-kb {
  flex-shrink: 0;
  padding: 8px 14px;
  border: 1px dashed var(--color-primary);
  border-radius: 8px;
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-new-kb:hover {
  background: var(--color-primary);
  color: white;
  border-style: solid;
}

/* ---- 保存标题输入 ---- */
.title-input {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: white;
  outline: none;
}

.title-input:focus {
  border-color: var(--color-primary);
}

/* ---- 新建知识库弹窗 ---- */
.kb-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.kb-modal {
  background: white;
  border-radius: 14px;
  width: 440px;
  max-width: 90vw;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.2);
  overflow: hidden;
}

.kb-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #ede6dc;
}

.kb-modal-header h3 {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
}

.kb-modal-close {
  border: none;
  background: transparent;
  font-size: 22px;
  line-height: 1;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.kb-modal-body {
  padding: 18px 20px;
}

.kb-form-group {
  margin-bottom: 14px;
}

.kb-form-label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}

.kb-input {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid #ede6dc;
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  box-sizing: border-box;
  background: white;
  color: var(--color-text);
}

.kb-input:focus {
  border-color: var(--color-primary);
}

.kb-vis-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.kb-vis-option {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
}

.kb-vis-option.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.kb-vis-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.kb-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 20px;
  border-top: 1px solid #ede6dc;
}

.page-header {
  margin-bottom: 24px;
}

.page-title {
  font-size: 24px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 6px;
}

.page-desc {
  font-size: 14px;
  color: var(--color-text-secondary);
}

/* 模式切换 */
.mode-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 24px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
  width: fit-content;
}

.mode-tab {
  padding: 8px 20px;
  font-size: 13px;
  font-weight: 500;
  border: none;
  background: white;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
}

.mode-tab:not(:last-child) {
  border-right: 1px solid var(--color-border);
}

.mode-tab.active {
  background: var(--color-primary);
  color: white;
}

.mode-tab:hover:not(.active) {
  background: #f3efe9;
}

/* 布局 */
.upload-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 32px;
}

/* 左面板 */
.upload-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ocr-selector {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ocr-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.ocr-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ocr-option {
  padding: 6px 14px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: white;
  font-size: 13px;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
}

.ocr-option:hover {
  border-color: var(--color-primary);
}

.ocr-option.selected {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
  font-weight: 500;
}

/* 可见性选项 */
.visibility-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.visibility-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: white;
  cursor: pointer;
  transition: all 0.2s;
}

.visibility-option:hover {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.visibility-option.selected {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.visibility-radio {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 2px solid var(--color-border);
  flex-shrink: 0;
  margin-top: 2px;
  position: relative;
  transition: all 0.2s;
}

.visibility-option.selected .visibility-radio {
  border-color: var(--color-primary);
}

.visibility-option.selected .visibility-radio::after {
  content: '';
  position: absolute;
  top: 3px;
  left: 3px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
}

.visibility-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.visibility-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.visibility-desc {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* 上传区域 */
.upload-zone {
  background: white;
  border: 2px dashed var(--color-border);
  border-radius: 16px;
  padding: 48px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  cursor: pointer;
  min-height: 200px;
  transition: all 0.2s;
}

.upload-zone:hover {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.upload-zone.drag-over {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.upload-zone.has-file {
  border-style: solid;
  padding: 16px;
}

.upload-icon {
  color: var(--color-primary);
  opacity: 0.6;
}

.upload-text {
  font-size: 15px;
  font-weight: 500;
  color: var(--color-text);
}

.upload-hint {
  font-size: 13px;
  color: var(--color-text-secondary);
}

.file-input-hidden {
  display: none;
}

/* 移动端快捷入口：拍照上传 / 相册选择 */
.upload-actions {
  display: flex;
  gap: 12px;
  margin-top: 14px;
}

.btn-upload-action {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  font-size: 14px;
  font-weight: 500;
  color: var(--color-primary);
  background: transparent;
  border: 1px solid var(--color-primary);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-upload-action:hover {
  background: var(--color-primary);
  color: white;
}

.btn-upload-action:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 文件预览 */
.file-preview {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  width: 100%;
}

.preview-image {
  max-height: 200px;
  max-width: 100%;
  border-radius: 8px;
  object-fit: contain;
}

.file-info {
  display: flex;
  gap: 12px;
  align-items: center;
}

.file-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
}

.file-size {
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* 操作按钮 */
.action-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 20px;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  border: 1px solid transparent;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-sm {
  padding: 6px 14px;
  font-size: 13px;
  border-radius: 8px;
}

.btn-primary {
  background: var(--color-primary);
  color: white;
  border-color: var(--color-primary);
}

.btn-primary:hover:not(:disabled) {
  background: #c56d23;
}

.btn-secondary {
  background: white;
  color: var(--color-text);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  background: #f3efe9;
}

.btn-success {
  background: #4caf50;
  color: white;
  border-color: #4caf50;
}

.btn-success:hover:not(:disabled) {
  background: #43a047;
}

.btn-outline {
  background: white;
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-outline:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-outline-danger {
  background: white;
  color: #c62828;
  border-color: #ffcdd2;
}

.btn-outline-danger:hover:not(:disabled) {
  background: #fce4ec;
  border-color: #c62828;
}

/* 旋转动画 */
.spinner-sm {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255,255,255,0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.spinner-xs {
  display: inline-block;
  width: 10px;
  height: 10px;
  border: 2px solid rgba(0,0,0,0.1);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: middle;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 状态消息 */
.status-message {
  padding: 12px 16px;
  border-radius: 10px;
  font-size: 14px;
}

.status-message.success {
  background: #e8f5e9;
  color: #2e7d32;
}

.status-message.error {
  background: #fce4ec;
  color: #c62828;
}

.status-message.warning {
  background: #fff3e0;
  color: #ef6c00;
}

.status-message.info {
  background: #e3f2fd;
  color: #1565c0;
}

/* ===== 批量上传 ===== */
.batch-file-summary {
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.batch-count {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-primary);
}

.batch-size {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.batch-file-list {
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: white;
}

.batch-file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  font-size: 13px;
  border-bottom: 1px solid var(--color-border);
  transition: background 0.15s;
  cursor: pointer;
}

.batch-file-item:last-child {
  border-bottom: none;
}

.batch-file-item.done,
.batch-file-item.recognized,
.batch-file-item.uploaded {
  background: #f1f8e9;
}

.batch-file-item.error {
  background: #fce4ec;
}

.batch-file-item.processing,
.batch-file-item.recognizing,
.batch-file-item.uploading {
  background: #fff8e1;
}

.batch-file-item.selected {
  background: #fff3e0;
  box-shadow: inset 3px 0 0 var(--color-primary);
}

.bf-index {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #f3efe9;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.bf-name {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 500;
  color: var(--color-text);
}

.bf-size {
  color: var(--color-text-secondary);
  font-size: 12px;
  flex-shrink: 0;
}

.bf-status {
  width: 84px;
  text-align: center;
  flex-shrink: 0;
  font-size: 12px;
}

.bf-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.batch-hint {
  margin: 8px 2px 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.batch-editing-tag {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--color-primary);
  background: #fff3e0;
  border-radius: 6px;
  padding: 2px 8px;
}

.bf-chunks {
  color: var(--color-primary);
  font-size: 12px;
  flex-shrink: 0;
}

.bf-error {
  color: #c62828;
  font-size: 11px;
  max-width: 120px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex-shrink: 0;
}

.status-icon {
  font-weight: 700;
  font-size: 14px;
}

.success-icon {
  color: #4caf50;
}

.error-icon {
  color: #c62828;
}

/* 批量进度条 */
.batch-progress-bar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.progress-track {
  flex: 1;
  height: 8px;
  background: var(--color-border);
  border-radius: 4px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: 4px;
  transition: width 0.3s ease;
}

.progress-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary);
  width: 40px;
  text-align: right;
}

/* ===== 右侧面板 ===== */
.right-panel {
  display: flex;
  flex-direction: column;
  background: white;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  overflow: hidden;
  max-height: calc(100vh - 200px);
}

.panel-tabs {
  display: flex;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.panel-tab {
  flex: 1;
  padding: 14px 16px;
  font-size: 14px;
  font-weight: 500;
  border: none;
  background: white;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.panel-tab.active {
  color: var(--color-primary);
  border-bottom: 2px solid var(--color-primary);
  background: var(--color-primary-light);
}

.panel-tab:hover:not(.active) {
  background: #faf8f5;
}

.tab-badge {
  background: var(--color-primary);
  color: white;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 8px;
  font-weight: 600;
}

/* 识别结果 */
.result-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.result-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text);
}

.char-count {
  font-size: 12px;
  color: var(--color-text-secondary);
  background: #f3efe9;
  padding: 2px 8px;
  border-radius: 8px;
}

.result-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 60px 24px;
  color: var(--color-text-secondary);
  opacity: 0.6;
}

.result-empty p {
  font-size: 14px;
}

.result-loading {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 60px 24px;
  color: var(--color-text-secondary);
}

.result-content {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.ocr-text-display {
  font-size: 14px;
  line-height: 1.8;
  color: var(--color-text);
  white-space: pre-wrap;
  word-wrap: break-word;
}

/* OCR 编辑 & 润色 */
.ocr-edit-area {
  margin-bottom: 16px;
}

.ocr-textarea {
  width: 100%;
  min-height: 160px;
  padding: 12px;
  border: 1px solid var(--color-primary);
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.8;
  font-family: inherit;
  resize: vertical;
  outline: none;
  background: #fffcf8;
}

.ocr-textarea:focus {
  box-shadow: 0 0 0 2px rgba(215, 130, 50, 0.2);
}

.ocr-edit-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.ocr-actions-bar {
  display: flex;
  gap: 8px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border);
}

/* ===== 上传历史 ===== */
.history-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.history-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.history-item {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  margin-bottom: 8px;
  overflow: hidden;
  transition: all 0.2s;
}

.history-item:hover {
  border-color: #ccc;
}

.history-item.expanded {
  border-color: var(--color-primary);
}

.history-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  cursor: pointer;
  transition: background 0.15s;
}

.history-item-header:hover {
  background: #faf8f5;
}

.hi-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}

.hi-filename {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.hi-date {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.hi-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.hi-chunks {
  font-size: 12px;
  color: var(--color-primary);
  background: var(--color-primary-light);
  padding: 2px 8px;
  border-radius: 8px;
}

.hi-expand-arrow {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.history-item-body {
  border-top: 1px solid var(--color-border);
  padding: 14px;
}

.hi-ocr-text {
  margin-bottom: 12px;
}

.hi-text-pre {
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text);
  white-space: pre-wrap;
  word-wrap: break-word;
  font-family: inherit;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
}

.edit-textarea {
  width: 100%;
  min-height: 120px;
  padding: 12px;
  border: 1px solid var(--color-primary);
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
  font-family: inherit;
  resize: vertical;
  outline: none;
}

.edit-textarea:focus {
  box-shadow: 0 0 0 2px rgba(215, 130, 50, 0.2);
}

.edit-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.hi-actions {
  display: flex;
  gap: 8px;
}

.load-more {
  text-align: center;
  padding: 12px;
}

/* ===== 图片裁剪弹窗 ===== */
.crop-modal {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(0, 0, 0, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  animation: fadeIn 0.2s ease;
}

.crop-modal-content {
  background: white;
  border-radius: 16px;
  width: 100%;
  max-width: 90vw;
  height: min(90vh, 900px);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}

.crop-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.crop-modal-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.crop-modal-close {
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s;
}

.crop-modal-close:hover {
  background: #f3efe9;
  color: var(--color-text);
}

.crop-modal-body {
  flex: 1;
  overflow: hidden;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}

.cropper-wrapper {
  flex: 1;
  min-height: 0;
  max-height: 100%;
  background: #f3efe9;
  border-radius: 12px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}

.cropper-image {
  display: block;
  max-width: 100%;
  max-height: 100%;
}

:deep(cropper-canvas) {
  display: block;
  width: 100%;
  height: 100%;
}

.crop-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  text-align: center;
  flex-shrink: 0;
}

/* 裁剪弹窗旋转工具栏 */
.crop-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex-shrink: 0;
  padding: 2px 0 10px;
}

.crop-toolbar .btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.crop-toolbar-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.rotate-angle {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  min-width: 36px;
  text-align: right;
  color: var(--color-text-primary);
}

.rotate-reset {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 6px;
  border: 1px solid var(--color-border, #e5ddd0);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.rotate-reset:hover {
  background: rgba(0, 0, 0, 0.04);
}

/* 上传图片预览弹窗 */
.upload-preview-body {
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f3efe9;
}

.upload-preview-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  transition: transform 0.1s ease-out;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.15);
  background: #fff;
}

.preview-meta {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-right: auto;
}

.crop-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border);
  flex-shrink: 0;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* ---- OCR 子模式切换 ---- */
.sub-mode-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.sub-mode-tab {
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 500;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: white;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.2s;
}

.sub-mode-tab:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.sub-mode-tab.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* ---- 文档上传图标 ---- */
.doc-icon {
  width: 64px;
  height: 64px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 700;
  color: white;
  background: #4caf50;
  flex-shrink: 0;
}

/* ---- 来源类型标签 ---- */
.hi-source-type {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 6px;
  background: #e3f2fd;
  color: #1565c0;
}

.hi-source-type.word {
  background: #e8f5e9;
  color: #2e7d32;
}

.hi-source-type.pdf {
  background: #fff3e0;
  color: #ef6c00;
}

/* ---- 移动端适配 ---- */
@media (max-width: 768px) {
  .upload-view {
    padding: 16px;
  }

  .page-title {
    font-size: 20px;
  }

  .page-desc {
    font-size: 13px;
  }

  .mode-tabs {
    width: 100%;
  }

  .mode-tab {
    flex: 1;
    text-align: center;
    padding: 8px 12px;
    font-size: 12px;
  }

  .upload-layout {
    grid-template-columns: 1fr;
    gap: 16px;
  }

  .upload-zone {
    padding: 32px 16px;
    min-height: 160px;
  }

  .upload-text {
    font-size: 14px;
  }

  .action-buttons {
    flex-direction: column;
  }

  .action-buttons .btn {
    justify-content: center;
  }

  .right-panel {
    max-height: none;
  }

  .result-content {
    padding: 12px;
  }

  .ocr-textarea {
    min-height: 120px;
  }

  .batch-file-item {
    flex-wrap: wrap;
    gap: 6px;
    padding: 8px 10px;
    font-size: 12px;
  }

  .bf-name {
    width: 100%;
    order: -1;
  }

  .bf-size {
    font-size: 11px;
  }

  .batch-file-summary {
    padding: 8px 0;
  }

  .history-item-header {
    padding: 10px 12px;
  }

  .hi-filename {
    font-size: 13px;
  }

  .history-item-body {
    padding: 10px;
  }

  .ocr-actions-bar {
    flex-wrap: wrap;
  }

  .visibility-options {
    grid-template-columns: 1fr;
  }

  .crop-modal {
    padding: 0;
  }

  .crop-modal-content {
    max-width: 100vw;
    width: 100vw;
    height: 100vh;
    max-height: 100vh;
    border-radius: 0;
  }

  .crop-modal-header {
    padding: 10px 16px;
  }

  .crop-modal-body {
    padding: 8px 12px;
  }

  .cropper-wrapper {
    min-height: 0;
    border-radius: 8px;
  }

  .crop-modal-footer {
    flex-direction: row;
    padding: 10px 16px;
  }

  .crop-modal-footer .btn {
    justify-content: center;
    flex: 1;
  }
}

/* ---- 文档分页预览 ---- */
.doc-pages-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: #f8f9fa;
  border-radius: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.doc-pages-info {
  font-size: 13px;
  color: var(--color-text-secondary);
}

.doc-preview-hint {
  color: var(--color-primary);
  margin-left: 6px;
}

.doc-pages-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.doc-pages-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.doc-page-card {
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: white;
  overflow: hidden;
  transition: box-shadow 0.2s, border-color 0.2s;
}

.doc-page-card.is-selected {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.doc-page-card.is-saved {
  background: #f8fff8;
  border-color: #81c784;
}

.doc-page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: #fafafa;
  border-bottom: 1px solid var(--color-border);
  flex-wrap: wrap;
}

.doc-page-checkbox {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-weight: 500;
}

.doc-page-checkbox input[type="checkbox"] {
  width: 16px;
  height: 16px;
  accent-color: var(--color-primary);
}

.doc-page-title {
  font-size: 14px;
  color: var(--color-text);
}

.doc-page-saved-tag {
  font-size: 11px;
  color: #2e7d32;
  background: #e8f5e9;
  padding: 2px 8px;
  border-radius: 6px;
}

.doc-page-actions {
  display: flex;
  gap: 8px;
}

.doc-page-body {
  padding: 16px;
}

.doc-page-textarea {
  width: 100%;
  min-height: 120px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 14px;
  line-height: 1.7;
  color: var(--color-text);
  resize: vertical;
  font-family: inherit;
}

.doc-page-textarea:disabled {
  background: #f5f5f5;
  color: var(--color-text-secondary);
}

.doc-page-tables {
  margin-top: 16px;
}

.doc-page-table {
  margin-bottom: 12px;
}

.markdown-table {
  background: #f8f9fa;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  font-size: 13px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
</style>
