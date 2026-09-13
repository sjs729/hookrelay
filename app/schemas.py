"""请求与响应模型。

Pydantic 模型在这里承担两个职责：

1. 在业务逻辑之前校验外部输入。类型错误、邮箱格式非法、密码太短这类问题，
   应该在进入数据库操作之前就被拒绝，返回 422 并指明具体哪个字段有问题。
2. 生成 /docs 里展示的字段说明和示例，让接口文档自带使用说明。

一条硬性原则：**响应模型只暴露该暴露的字段**。
password_hash、api_key_hash、secret_encrypted 这类字段永远不出现在任何响应模型里。
宁可逐个字段显式声明，也不要图省事直接返回 ORM 对象——
那样一旦模型加了敏感字段，就会自动泄露出去。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """注册请求。"""

    email: EmailStr = Field(
        description="登录邮箱，同时也是账号唯一标识",
        examples=["dev@example.com"],
    )
    password: str = Field(
        min_length=8,
        max_length=128,
        description="密码，至少 8 位",
        examples=["a-strong-password"],
    )


class RegisterResponse(BaseModel):
    """注册响应。

    api_key 字段是这套系统里唯一的明文出现点：注册成功返回一次，
    之后服务端只保存哈希，再也没有办法取回。调用方必须自己存好。
    """

    id: UUID
    email: EmailStr
    api_key: str = Field(
        description="API Key 明文，仅此一次返回，请立即保存。服务端只存哈希，丢失无法找回。",
    )
    api_key_prefix: str = Field(
        description="用于在界面上辨识 Key 的前缀",
    )
    created_at: datetime


class UserResponse(BaseModel):
    """当前用户信息。刻意不包含任何哈希值。"""

    # from_attributes=True 让 Pydantic 能直接从 ORM 对象读取属性
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    api_key_prefix: str
    is_active: bool
    created_at: datetime
