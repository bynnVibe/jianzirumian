"""pytest 共享配置：确保 backend 目录在导入路径中"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if __name__ == '__main__':
    from dotenv import load_dotenv

    load_dotenv()
    import os
    from openai import OpenAI

    client = OpenAI(
      api_key=os.getenv("DASHSCOPE_API_KEY"),
      base_url="https://ws-8scr1ksvp56zxn0y.cn-beijing.maas.aliyuncs.com/compatible-api/v1",
    )

    results = client.post(
      "/reranks",
      body={
        "model": "qwen3-rerank",
        "query": "什么是重排序模型",
        "documents": [
          "重排序模型广泛应用于搜索引擎和推荐系统，按相关性对候选文本进行排序",
          "量子计算是计算科学的前沿领域",
          "预训练语言模型的发展为重排序模型带来了新的进展"
        ],
        "top_n": 2
      },
      cast_to=object
    )

    print(results)