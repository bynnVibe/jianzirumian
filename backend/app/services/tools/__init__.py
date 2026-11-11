"""
见字如面 - 聊天框工具调用框架（标准化注册表）

将图片识别/入库、文档解析/入库、知识总结、联网搜索、知识库检索、CLI 命令行封装等
能力统一封装为携带标准化元数据（命名空间/标签/适用场景/参数 Schema/权限/风险等级/
可用状态）的 Tool。导入本包并调用 bootstrap_registry() 即完成全部工具注册，
供 Tool Routing（场景感知路由）、调用前约束钩子、function calling schema 导出
与 Tool Routing 可观测性统一治理。
"""
from typing import List


def bootstrap_registry() -> List[str]:
    """导入全部工具模块（触发模块级 register_tool）并注册 CLI 规格工具。

    在应用启动（lifespan）时调用一次；导入置于函数内以避免包初始化的循环依赖。
    返回已注册的全部工具名列表。
    """
    # 附件/知识类工具：模块级 register_tool(...) 在导入时执行
    from app.services.tools import image_ocr  # noqa: F401
    from app.services.tools import image_upload  # noqa: F401
    from app.services.tools import document_import  # noqa: F401
    from app.services.tools import knowledge_summarize  # noqa: F401
    # 检索类工具：web_search / search_knowledge
    from app.services.tools import retrieval  # noqa: F401
    # CLI 封装工具：内置规格 + cli_specs/*.json 规格目录
    from app.services.tools.cli_spec import register_all_cli_tools
    register_all_cli_tools()

    from app.services.tools.base import list_tools
    return list_tools()
