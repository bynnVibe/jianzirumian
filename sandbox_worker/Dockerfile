# 构建时必须传入管理员核验的 Python 3.11 镜像 digest；不在任务运行时拉取镜像。
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
COPY --chmod=0444 runner.py /opt/runner.py
USER 65532:65532
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/runner.py"]
