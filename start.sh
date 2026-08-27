#!/bin/bash
# 见字如养 - 一键启动脚本
# 启动后端和前端服务

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "  见字如养 - 手写笔记知识库问答系统"
echo "=========================================="
echo ""

# 检查环境
check_command() {
    if ! command -v "$1" &> /dev/null; then
        echo "❌ 未找到 $1，请先安装"
        exit 1
    fi
}

check_command python3
check_command node

# 安装后端依赖
echo "[1/4] 安装后端依赖..."
cd "$PROJECT_DIR/backend"
pip install -r requirements.txt -q 2>/dev/null || pip3 install -r requirements.txt -q
echo "   ✓ 后端依赖就绪"

# 检查 .env
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "   ✓ 已创建 .env 配置文件，请根据需要修改"
fi

# 检查 Ollama
if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "   ✓ Ollama 服务运行中"
else
    echo "   ⚠  Ollama 未启动。如需本地模型，请启动: ollama serve"
    echo "     或修改 .env 中的 LLM_PROVIDER 和 EMBEDDING_PROVIDER 为 API 模式"
fi

# 安装前端依赖
echo "[2/4] 安装前端依赖..."
cd "$PROJECT_DIR/frontend"
if [ ! -d "node_modules" ]; then
    npm install --silent 2>/dev/null
fi
echo "   ✓ 前端依赖就绪"

echo ""
echo "[3/4] 启动后端服务 (http://localhost:8000)..."
cd "$PROJECT_DIR/backend"
python3 -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
echo "   ✓ 后端服务已启动 (PID: $BACKEND_PID)"

echo "[4/4] 启动前端开发服务器 (http://localhost:5173)..."
cd "$PROJECT_DIR/frontend"
npx vite --host 0.0.0.0 --port 5173 &
FRONTEND_PID=$!
echo "   ✓ 前端服务已启动 (PID: $FRONTEND_PID)"

echo ""
echo "=========================================="
echo "  启动完成!"
echo ""
echo "  前端: http://localhost:5173"
echo "  后端: http://localhost:8000"
echo "  API 文档: http://localhost:8000/docs"
echo ""
echo "  按 Ctrl+C 停止所有服务"
echo "=========================================="

# 捕获退出信号
trap "echo '正在停止服务...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" SIGINT SIGTERM

# 等待子进程
wait
