# ============================================
# 见字如养 - 隔离浏览器渲染兜底 Dockerfile
# ============================================
# 用途：仅承载无网络（network_mode: none）的 Playwright Chromium 渲染进程，
#       通过共享 Unix socket 接受 Agent 的渲染指令；网页资源由 Agent 安全抓取器回传，
#       浏览器本身不直连出网、不加入业务网络、不映射任何 TCP 端口。
# 安全：非 root 运行、Chromium sandbox、只读根文件系统由 compose 层施加。
# 基础镜像源：与后端一致，默认走 DaoCloud 公共代理，可用 --build-arg REGISTRY 覆盖。
ARG REGISTRY=docker.m.daocloud.io/library/
FROM ${REGISTRY}python:3.11-slim

WORKDIR /app

# PyPI 源：默认阿里云镜像，可用 --build-arg PIP_INDEX_URL 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
# 浏览器统一安装到镜像内固定路径，供非 root 用户读取（不依赖 HOME 缓存）
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY renderer/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip -i ${PIP_INDEX_URL} \
    && pip install --no-cache-dir -r requirements.txt -i ${PIP_INDEX_URL} \
    # --with-deps 安装 Chromium 运行所需的最小系统库；随后清理 apt 缓存压缩体积
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && find /app -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

COPY renderer/worker.py .

# 非 root 运行；预建 /ipc 并归属渲染用户，命名卷首次初始化即继承该权限
RUN useradd -m -u 1000 renderer \
    && mkdir -p /ipc \
    && chown renderer:renderer /ipc \
    && chmod 770 /ipc
USER renderer

ENV URL_RENDERER_SOCKET=/ipc/renderer.sock

# 单进程串行渲染；init 由 compose 层提供（init: true）负责回收僵尸进程
CMD ["python", "worker.py"]
