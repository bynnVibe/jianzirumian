"""显式能力命令解析；JSON、multipart 与服务层使用同一规则。"""
import re
from dataclasses import dataclass

_PREFIX = re.compile(r"^@(skills|tools?)(?=\s|$)", re.I)


@dataclass(frozen=True)
class ChatCommand:
    name: str = ""
    description: str = ""


def parse_command(text: str, command: str = "") -> ChatCommand:
    explicit = (command or "").lower().removeprefix("@")
    explicit = "tool" if explicit == "tools" else explicit
    if explicit not in ("", "skills", "tool"):
        raise ValueError("不支持的命令，请使用 @skills 或 @tool")
    match = _PREFIX.match(text or "")
    parsed = ("tool" if match[1].lower() == "tools" else match[1].lower()) if match else ""
    if explicit and parsed and explicit != parsed:
        raise ValueError("命令字段与消息中的命令不一致")
    name = explicit or parsed
    description = (text[match.end():] if match else text or "").strip()
    if name and not description:
        example = "@skills 帮我添加一个文章总结技能" if name == "skills" else "@tool 查询指定城市的天气"
        raise ValueError(f"请补充功能描述，例如：{example}")
    return ChatCommand(name, description)
