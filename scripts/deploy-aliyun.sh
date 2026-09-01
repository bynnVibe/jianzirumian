#!/bin/bash
# ============================================
# 见字如面 - 阿里云 Docker 部署助手
# ============================================
# 用法：
#   bash scripts/deploy-aliyun.sh build     # 本地构建镜像
#   bash scripts/deploy-aliyun.sh save      # 导出镜像为 tar 文件
#   bash scripts/deploy-aliyun.sh upload    # 上传到阿里云服务器
#   bash scripts/deploy-aliyun.sh all       # 构建 + 导出 + 上传（一键）
# ============================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_NAME="jianzirumian"

# ============ 配置项（按需修改）============
# 阿里云服务器信息
REMOTE_USER="root"
REMOTE_HOST="114.55.62.152"
REMOTE_DIR="/opt/jianziruyang"

# 镜像标签
BACKEND_IMAGE="${PROJECT_NAME}-backend:latest"
FRONTEND_IMAGE="${PROJECT_NAME}-frontend:latest"

# 导出目录
EXPORT_DIR="${PROJECT_DIR}/docker-images"

# 基础镜像源覆盖（Dockerfile 默认走 DaoCloud 代理；如该源失效可换源重建）：
#   REGISTRY=docker.io/library/ bash scripts/deploy-aliyun.sh build
REGISTRY_BUILD_ARG=""
if [ -n "${REGISTRY:-}" ]; then
    REGISTRY_BUILD_ARG="--build-arg REGISTRY=${REGISTRY}"
fi

# PyPI 源覆盖（后端镜像依赖安装默认走阿里云镜像；境外机器可切官方源）：
#   PIP_INDEX_URL=https://pypi.org/simple bash scripts/deploy-aliyun.sh build
PIP_INDEX_BUILD_ARG=""
if [ -n "${PIP_INDEX_URL:-}" ]; then
    PIP_INDEX_BUILD_ARG="--build-arg PIP_INDEX_URL=${PIP_INDEX_URL}"
fi

# 后端镜像瘦身开关（默认开启）：不装 torch / sentence-transformers / LibreOffice，
# 实测镜像从 2.27GB 降到 574MB（导出 tar.gz 从 ~745MB 降到 ~206MB），
# 前提：.env 中 RERANKER_PROVIDER 使用远端提供商（如 dashscope），
# Word 预览自动降级为 mammoth 渲染。
# 需要本地重排序 + 原始排版 PDF 预览时：
#   SLIM=0 bash scripts/deploy-aliyun.sh build
SLIM="${SLIM:-1}"
SLIM_BUILD_ARG="--build-arg SLIM=${SLIM}"

# 颜色
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
info() { echo -e "${BLUE}[i]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }

# ============ 辅助函数 ============

check_docker() {
    if ! command -v docker &> /dev/null; then
        err "Docker 未安装，请先安装 Docker Desktop"
        exit 1
    fi
    info "Docker 版本: $(docker --version)"
}

cmd_build() {
    check_docker

    echo ""
    info "=========================================="
    info "  构建 Docker 镜像"
    info "=========================================="
    echo ""

    cd "$PROJECT_DIR"

    # 构建后端镜像（SLIM=1 瘦身模式默认开启，导出包体积大幅缩小）
    if [ "$SLIM" = "1" ]; then
        info "构建后端镜像（瘦身模式：不含本地重排序 / LibreOffice）..."
    else
        info "构建后端镜像（全量模式：含本地重排序 / LibreOffice）..."
    fi
    docker build \
        -t "$BACKEND_IMAGE" \
        -f Dockerfile.backend \
        --platform linux/amd64 \
        $REGISTRY_BUILD_ARG \
        $PIP_INDEX_BUILD_ARG \
        $SLIM_BUILD_ARG \
        .
    log "后端镜像构建完成: $BACKEND_IMAGE"

    # 构建前端镜像
    info "构建前端镜像..."
    docker build \
        -t "$FRONTEND_IMAGE" \
        -f Dockerfile.frontend \
        --platform linux/amd64 \
        $REGISTRY_BUILD_ARG \
        .
    log "前端镜像构建完成: $FRONTEND_IMAGE"

    echo ""
    log "镜像列表："
    docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" | grep "$PROJECT_NAME"
}

cmd_save() {
    check_docker

    echo ""
    info "=========================================="
    info "  导出镜像为 tar 文件"
    info "=========================================="
    echo ""

    # 检查镜像是否存在
    if ! docker image inspect "$BACKEND_IMAGE" &>/dev/null; then
        err "后端镜像不存在，请先执行: bash scripts/deploy-aliyun.sh build"
        exit 1
    fi
    if ! docker image inspect "$FRONTEND_IMAGE" &>/dev/null; then
        err "前端镜像不存在，请先执行: bash scripts/deploy-aliyun.sh build"
        exit 1
    fi

    mkdir -p "$EXPORT_DIR"

    info "导出后端镜像..."
    docker save "$BACKEND_IMAGE" | gzip > "$EXPORT_DIR/${PROJECT_NAME}-backend.tar.gz"
    log "已导出: $EXPORT_DIR/${PROJECT_NAME}-backend.tar.gz ($(du -h "$EXPORT_DIR/${PROJECT_NAME}-backend.tar.gz" | cut -f1))"

    info "导出前端镜像..."
    docker save "$FRONTEND_IMAGE" | gzip > "$EXPORT_DIR/${PROJECT_NAME}-frontend.tar.gz"
    log "已导出: $EXPORT_DIR/${PROJECT_NAME}-frontend.tar.gz ($(du -h "$EXPORT_DIR/${PROJECT_NAME}-frontend.tar.gz" | cut -f1))"

    # 复制部署文件
    cp "$PROJECT_DIR/docker-compose.yml" "$EXPORT_DIR/docker-compose.yml"
    cp "$PROJECT_DIR/nginx.conf" "$EXPORT_DIR/nginx.conf"
    cp "$PROJECT_DIR/backend/.env.example" "$EXPORT_DIR/.env.example"

    echo ""
    log "所有文件已导出到: $EXPORT_DIR/"
    ls -lh "$EXPORT_DIR/"
    echo ""

    info "打包所有文件为单个压缩包..."
    cd "$PROJECT_DIR"
    # COPYFILE_DISABLE=1：禁止 macOS bsdtar 写入 AppleDouble/扩展属性，
    # 避免 Linux 服务器解压时报 "Ignoring unknown extended header keyword `LIBARCHIVE.xattr...'"
    # --exclude：避免把上一次生成的部署包也打进新包（会导致包体积翻倍膨胀）
    COPYFILE_DISABLE=1 tar czf "${PROJECT_NAME}-deploy.tar.gz" \
        --exclude="${PROJECT_NAME}-deploy.tar.gz" \
        -C "$EXPORT_DIR" \
        .
    mv "${PROJECT_NAME}-deploy.tar.gz" "$EXPORT_DIR/"
    log "部署包: $EXPORT_DIR/${PROJECT_NAME}-deploy.tar.gz ($(du -h "$EXPORT_DIR/${PROJECT_NAME}-deploy.tar.gz" | cut -f1))"
}

cmd_upload() {
    echo ""
    info "=========================================="
    info "  上传部署包到阿里云服务器"
    info "=========================================="
    echo ""

    DEPLOY_PACKAGE="$EXPORT_DIR/${PROJECT_NAME}-deploy.tar.gz"

    if [ ! -f "$DEPLOY_PACKAGE" ]; then
        err "部署包不存在，请先执行: bash scripts/deploy-aliyun.sh save"
        exit 1
    fi

    if [ "$REMOTE_HOST" = "your-server-ip" ]; then
        err "请先配置服务器信息："
        err "  编辑脚本顶部 REMOTE_HOST、REMOTE_USER、REMOTE_DIR 变量"
        err "  或手动执行: scp $DEPLOY_PACKAGE root@your-server-ip:/opt/"
        exit 1
    fi

    info "上传到 ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
    info "连接测试中..."
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p ${REMOTE_DIR}/scripts" || {
        err "SSH 连接失败，请检查："
        err "  1. 服务器地址: ${REMOTE_HOST}"
        err "  2. 用户名: ${REMOTE_USER}"
        err "  3. 安全组是否开放 22 端口"
        err "  4. 是否已配置 SSH 密钥登录"
        exit 1
    }

    info "上传部署包（大文件可能需要几分钟）..."
    scp "$DEPLOY_PACKAGE" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"
    log "上传完成"

    info "上传部署辅助文件..."
    scp "$PROJECT_DIR/scripts/deploy-aliyun.sh" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/scripts/"
    log "上传完成"

    echo ""
    warn "=========================================="
    warn "  上传完成！请在服务器上执行以下命令："
    warn "=========================================="
    echo ""
    echo "  ssh ${REMOTE_USER}@${REMOTE_HOST}"
    echo "  cd ${REMOTE_DIR}"
    echo "  tar xzf ${PROJECT_NAME}-deploy.tar.gz"
    echo "  bash scripts/deploy-aliyun.sh server-load"
    echo ""
}

cmd_all() {
    cmd_build
    cmd_save
    cmd_upload
}

# ============ 服务器端命令 ============

cmd_server_load() {
    echo ""
    info "=========================================="
    info "  在服务器上加载 Docker 镜像"
    info "=========================================="
    echo ""

    check_docker

    # 加载镜像
    info "加载后端镜像..."
    gunzip -c "${PROJECT_NAME}-backend.tar.gz" | docker load
    log "后端镜像加载完成"

    info "加载前端镜像..."
    gunzip -c "${PROJECT_NAME}-frontend.tar.gz" | docker load
    log "前端镜像加载完成"

    echo ""
    docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" | grep "$PROJECT_NAME"
}

cmd_server_start() {
    echo ""
    info "=========================================="
    info "  启动服务"
    info "=========================================="
    echo ""

    if [ ! -f "docker-compose.yml" ]; then
        err "未找到 docker-compose.yml，请先解压部署包"
        exit 1
    fi

    # 创建必要目录（含 SQLite 持久化目录与日志目录）
    mkdir -p data/uploads data/vector_store data/knowledge data/db logs/backend logs/frontend

    # 配置 .env
    if [ ! -f "backend/.env" ]; then
        mkdir -p backend
        cp .env.example backend/.env
        warn "已创建 backend/.env，请编辑配置后重新启动"
        warn "  vim backend/.env"
        exit 1
    fi

    info "启动所有服务..."
    docker compose up -d
    log "服务已启动"

    echo ""
    info "服务状态："
    docker compose ps

    echo ""
    info "访问地址：http://$(curl -s ifconfig.me):5173"
    info "查看日志：docker compose logs -f"
}

cmd_server_status() {
    echo ""
    info "服务状态："
    docker compose ps 2>/dev/null || echo "服务未运行"

    echo ""
    info "资源占用："
    docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"
}

cmd_server_logs() {
    docker compose logs --tail=50 -f
}

cmd_server_stop() {
    docker compose stop
    log "服务已停止"
}

cmd_server_restart() {
    docker compose restart
    log "服务已重启"
}

cmd_server_update() {
    # 更新：加载新镜像后重启
    cmd_server_load
    cmd_server_start
}

# ============ 帮助 ============

cmd_help() {
    echo ""
    echo "见字如面 - 阿里云部署助手"
    echo ""
    echo "本地操作（在开发机上执行）："
    echo ""
    echo "  bash scripts/deploy-aliyun.sh build        # 构建 Docker 镜像（amd64 架构）"
    echo "  bash scripts/deploy-aliyun.sh save         # 导出镜像为 tar.gz 部署包"
    echo "  bash scripts/deploy-aliyun.sh upload       # 上传部署包到阿里云"
    echo "  bash scripts/deploy-aliyun.sh all          # 构建 → 导出 → 上传（一键）"
    echo ""
    echo "服务器操作（在阿里云 ECS 上执行）："
    echo ""
    echo "  bash scripts/deploy-aliyun.sh server-load     # 加载 Docker 镜像"
    echo "  bash scripts/deploy-aliyun.sh server-start    # 启动服务"
    echo "  bash scripts/deploy-aliyun.sh server-status   # 查看服务状态"
    echo "  bash scripts/deploy-aliyun.sh server-logs     # 查看实时日志"
    echo "  bash scripts/deploy-aliyun.sh server-stop     # 停止服务"
    echo "  bash scripts/deploy-aliyun.sh server-restart  # 重启服务"
    echo "  bash scripts/deploy-aliyun.sh server-update   # 更新镜像后重启"
    echo ""
    echo "首次部署的完整流程："
    echo ""
    echo "  # 1. 在本地开发机"
    echo "  bash scripts/deploy-aliyun.sh all"
    echo ""
    echo "  # 2. SSH 登录服务器后"
    echo "  cd /opt/jianziruyang"
    echo "  tar xzf jianzirumian-deploy.tar.gz"
    echo "  bash scripts/deploy-aliyun.sh server-load"
    echo "  vim backend/.env              # 配置环境变量"
    echo "  bash scripts/deploy-aliyun.sh server-start"
    echo ""
}

# ============ 命令行分发 ============

case "${1:-help}" in
    build)          cmd_build ;;
    save)           cmd_save ;;
    upload)         cmd_upload ;;
    all)            cmd_all ;;
    server-load)    cmd_server_load ;;
    server-start)   cmd_server_start ;;
    server-status)  cmd_server_status ;;
    server-logs)    cmd_server_logs ;;
    server-stop)    cmd_server_stop ;;
    server-restart) cmd_server_restart ;;
    server-update)  cmd_server_update ;;
    help|--help|-h) cmd_help ;;
    *)
        err "未知命令: $1"
        echo "可用命令: build, save, upload, all"
        echo "          server-load, server-start, server-status, server-logs"
        echo "          server-stop, server-restart, server-update"
        exit 1
        ;;
esac
