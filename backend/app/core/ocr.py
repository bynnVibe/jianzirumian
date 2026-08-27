"""
见字如面 - OCR 抽象层
支持:
  - local: 自动回退多后端 (EasyOCR → PaddleOCR → Tesseract)
  - aliyun: 阿里云 OCR API
  - custom_api: 自定义远程 OCR API
"""
import base64
import json
import logging
import os
import time
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.services.config_manager import effective_provider_config, runtime_config

warnings.filterwarnings("ignore", category=UserWarning, module="paddle")
warnings.filterwarnings("ignore", category=FutureWarning, module="torch")

logger = logging.getLogger("jianziruyang.ocr")

# 共享异步客户端：OCR 请求（尤其大图 base64）复用连接池，
# 避免每次识别重复 TCP+TLS 握手；读超时按请求单独指定
_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=60, pool=10),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _shared_client


class BaseOCR(ABC):
    """OCR 基类"""

    @abstractmethod
    async def recognize(self, image_path: str) -> str:
        """识别图片中的文字，返回识别结果文本"""
        ...


class AutoLocalOCR(BaseOCR):
    """本地 OCR — 自动尝试多个后端，优先使用效果最好的"""

    def __init__(self):
        self._reader = None
        self._backend_name = None
        self._init_error = None

    def _try_init(self):
        """尝试初始化 OCR 后端（按优先级）"""
        if self._reader is not None:
            return

        # ---- 尝试 1: EasyOCR ----
        logger.info("尝试初始化 EasyOCR ...")
        start_t = time.time()
        try:
            os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
            # SSL_CERT_FILE: EasyOCR 下载模型时需要 HTTPS
            # 在受限网络环境下，使用 certifi 提供的证书链
            _current_ssl = os.environ.get("SSL_CERT_FILE")
            if not _current_ssl:
                try:
                    import certifi
                    os.environ["SSL_CERT_FILE"] = certifi.where()
                    logger.debug("已设置 SSL_CERT_FILE=%s", os.environ["SSL_CERT_FILE"])
                except ImportError:
                    pass
            import easyocr
            self._reader = easyocr.Reader(
                ["ch_sim", "en"],
                gpu=False,
                verbose=False,
            )
            self._backend_name = "EasyOCR"
            elapsed = time.time() - start_t
            logger.info("EasyOCR 初始化成功 (%.2fs)", elapsed)
            return
        except ImportError:
            logger.info("EasyOCR 未安装，尝试下一后端")
            pass
        except Exception as e:
            self._init_error = f"EasyOCR 初始化失败: {e}"
            logger.warning("EasyOCR 初始化异常: %s", e)
        else:
            self._reader = None
            if _current_ssl is None:
                os.environ.pop("SSL_CERT_FILE", None)

        # ---- 尝试 2: PaddleOCR ----
        logger.info("尝试初始化 PaddleOCR ...")
        try:
            from paddleocr import PaddleOCR
            self._reader = PaddleOCR(lang=settings.PADDLE_OCR_LANG)
            self._backend_name = "PaddleOCR"
            logger.info("PaddleOCR 初始化成功")
            return
        except ImportError:
            logger.info("PaddleOCR 未安装，尝试下一后端")
            pass
        except Exception as e:
            self._init_error = f"PaddleOCR 初始化失败: {e}"
            logger.warning("PaddleOCR 初始化异常: %s", e)
        else:
            self._reader = None

        # ---- 尝试 3: Tesseract ----
        logger.info("尝试初始化 Tesseract ...")
        try:
            import pytesseract
            from PIL import Image
            # 验证 tesseract 是否可用
            pytesseract.get_tesseract_version()
            self._reader = pytesseract
            self._backend_name = "Tesseract"
            logger.info("Tesseract 初始化成功")
            return
        except ImportError:
            logger.info("Tesseract 未安装")
            pass
        except Exception as e:
            self._init_error = f"Tesseract 初始化失败: {e}"
            logger.warning("Tesseract 初始化异常: %s", e)
        else:
            self._reader = None

        # ---- 全部失败 ----
        logger.error("所有本地 OCR 后端均不可用")
        raise ImportError(
            "本地 OCR 不可用，请选择以下方案之一：\n"
            "  1) 安装 EasyOCR:  pip install easyocr torch\n"
            "  2) 安装 PaddleOCR: pip install paddlepaddle paddleocr\n"
            "  3) 安装 Tesseract:  brew install tesseract && pip install pytesseract\n"
            "  4) 切换 OCR_PROVIDER 为 aliyun 或 custom_api (云端)\n"
            f"\n  最近错误: {self._init_error or '无可用 OCR 后端'}"
        )

    async def recognize(self, image_path: str) -> str:
        # 本地 OCR 后端（EasyOCR/PaddleOCR/Tesseract）与 PIL 预处理均为同步重计算，
        # 移入线程池执行，避免长时间阻塞事件循环拖慢其他并发请求
        import asyncio

        return await asyncio.to_thread(self._recognize_sync, image_path)

    def _recognize_sync(self, image_path: str) -> str:
        """同步识别实现（由 recognize 通过线程池调用）"""
        logger.info("OCR 识别开始: path=%s", image_path)
        start_t = time.time()
        self._try_init()

        file_size = Path(image_path).stat().st_size if Path(image_path).exists() else 0

        # ---------- 图片预处理：缩小大图以提高 OCR 速度和准确率 ----------
        from PIL import Image as PILImage
        import tempfile

        _preprocessed = False
        _tmp_path = None
        try:
            img = PILImage.open(image_path)
            w, h = img.size
            max_dim = 1200  # 最长边限制
            if w > max_dim or h > max_dim:
                ratio = min(max_dim / w, max_dim / h)
                new_w, new_h = int(w * ratio), int(h * ratio)
                logger.info("图片压缩: %dx%d -> %dx%d (ratio=%.2f)", w, h, new_w, new_h, ratio)
                img = img.resize((new_w, new_h), PILImage.LANCZOS)
                fd, _tmp_path = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                img = img.convert("RGB")
                img.save(_tmp_path, "JPEG", quality=85)
                image_path = _tmp_path
                _preprocessed = True
        except Exception as e:
            logger.warning("图片预处理失败，使用原始图片: %s", e)

        logger.info("OCR 后端=%s | 原始大小=%.1fKB%s",
                     self._backend_name, file_size / 1024,
                     f" -> 压缩后识别" if _preprocessed else "")

        try:
            if self._backend_name == "EasyOCR":
                logger.debug("EasyOCR readtext ...")
                result = self._reader.readtext(image_path)
                lines = [item[1] for item in result]
                text = "\n".join(lines)
                logger.info("EasyOCR 识别完成: %d 个文本块, %d 字符", len(result), len(text))
                logger.debug("EasyOCR 原始结果: %s", result[:3] if result else "空")
                return text

            elif self._backend_name == "PaddleOCR":
                logger.debug("PaddleOCR 识别中 ...")
                result = self._reader.ocr(image_path, cls=True)
                if not result or not result[0]:
                    logger.info("PaddleOCR 未识别到文字")
                    return ""
                lines = [line[1][0] for line in result[0]]
                text = "\n".join(lines)
                logger.info("PaddleOCR 识别完成: %d 行, %d 字符", len(lines), len(text))
                return text

            elif self._backend_name == "Tesseract":
                from PIL import Image
                logger.debug("Tesseract 识别中 ...")
                text = self._reader.image_to_string(
                    Image.open(image_path),
                    lang="chi_sim+eng",
                )
                text = text.strip()
                logger.info("Tesseract 识别完成: %d 字符", len(text))
                return text

        except Exception as e:
            elapsed = time.time() - start_t
            logger.error("OCR 识别失败 (%.2fs): %s", elapsed, e)
            raise RuntimeError(
                f"{self._backend_name} 识别失败: {e}\n"
                f"请尝试其他 OCR 方案或检查图片是否有效。"
            )
        finally:
            elapsed = time.time() - start_t
            logger.info("OCR 识别结束 (%.2fs)", elapsed)
            # 清理临时文件
            if _tmp_path and os.path.exists(_tmp_path):
                try:
                    os.remove(_tmp_path)
                except Exception:
                    pass

        return ""


class AliyunOCR(BaseOCR):
    """阿里云百炼 OCR (OpenAI 兼容接口)

    使用 qwen3.5-ocr 等多模态大模型识别图片文字。
    调用格式为 OpenAI Chat Completions API，支持图片 base64 输入。
    """

    def __init__(self):
        cfg = effective_provider_config("ocr", "aliyun")
        self.api_base = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "")

        if not self.api_key:
            raise ValueError(
                "阿里云百炼 OCR 未配置 API Key。请在系统设置页面配置，或在 .env 中设置以下之一：\n"
                "  BAILIAN_OCR_API_KEY=你的百炼APIKey\n"
                "  或 ALIYUN_ACCESS_KEY_SECRET=你的百炼APIKey\n"
                "  或 EMBEDDING_API_KEY=...（与 Embedding 共用）"
            )
        logger.info("百炼 OCR 初始化: model=%s | base=%s", self.model, self.api_base)

    async def recognize(self, image_path: str) -> str:
        """调用百炼多模态模型识别图片文字"""
        # 读取图片并转为 base64
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")

        # 推断 MIME 类型
        ext = Path(image_path).suffix.lower()
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
        }.get(ext, "image/jpeg")

        data_url = f"data:{mime};base64,{img_b64}"

        # 构建 OpenAI 兼容的消息
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                        {
                            "type": "text",
                            "text": "提取图片中所有文字",
                        },
                    ],
                }
            ],
            "max_tokens": 2048,
        }

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.info("百炼 OCR 请求: model=%s | 图片大小=%.1fKB", self.model, len(img_bytes) / 1024)

        try:
            resp = await _get_client().post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            result = resp.json()
        except httpx.TimeoutException:
            raise RuntimeError("百炼 OCR 请求超时（120s），请检查网络或图片大小")
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.text[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"百炼 OCR API 返回错误 (HTTP {e.response.status_code}): {detail}"
            )
        except Exception as e:
            raise RuntimeError(f"百炼 OCR 请求失败: {e}")

        # 提取回复文本
        choices = result.get("choices", [])
        if not choices:
            logger.warning("百炼 OCR 返回空 choices: %s", str(result)[:200])
            return ""

        text = choices[0].get("message", {}).get("content", "").strip()
        logger.info("百炼 OCR 识别完成: %d 字符", len(text))
        return text


class CustomAPIOCR(BaseOCR):
    """自定义远程 OCR API"""

    def __init__(self):
        cfg = effective_provider_config("ocr", "custom_api")
        self.api_url = cfg.get("api_url", "")
        self.api_key = cfg.get("api_key", "")

    async def recognize(self, image_path: str) -> str:
        if not self.api_url:
            raise ValueError("未配置自定义 OCR API URL")

        with open(image_path, "rb") as f:
            files = {"image": f}
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            resp = await _get_client().post(self.api_url, files=files, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()

            text = (
                data.get("text")
                or data.get("result")
                or data.get("data", {}).get("text", "")
            )
            return text


class OCRFactory:
    """OCR 工厂"""

    @staticmethod
    def create(provider: str = None) -> BaseOCR:
        provider = provider or runtime_config.ocr_provider or settings.OCR_PROVIDER

        if provider == "local":
            return AutoLocalOCR()
        elif provider == "aliyun":
            return AliyunOCR()
        elif provider == "custom_api":
            return CustomAPIOCR()
        else:
            raise ValueError(f"不支持的 OCR 提供商: {provider}")
