"""密码与密钥处理。

本模块处理三类凭据，哈希策略刻意不同，这个区别是面试里很好展开的点：

1. 用户密码 —— 用 argon2（慢哈希）
   密码由人类选择，熵很低（"password123" 这类一猜就中），必须靠"算得慢"
   来抵抗离线暴力破解。argon2 同时消耗 CPU 和内存，让攻击者用 GPU 集群
   并行破解的成本大幅上升，是目前密码哈希的推荐算法。

2. API Key —— 用 SHA-256（快哈希）
   它是 32 字节密码学随机数，熵足够高，暴力枚举在物理上不可行。
   此时再用慢哈希只会让每个请求的鉴权都变慢，得不偿失。
   快哈希还有一个好处：结果长度固定，可以直接建唯一索引查询。

3. 接收地址 token —— 同样用 SHA-256，理由与 API Key 相同。

三者的共同点：数据库里存的都不是明文。即使数据库被拖走，
攻击者也无法直接拿去调用接口。

安全原则提醒：本模块的函数不做任何日志输出。
密钥类数据一旦进了日志文件，就等于泄露了。
"""

import hashlib
import secrets

from pwdlib import PasswordHash

# argon2 的具体参数由 pwdlib 的 recommended() 决定，
# 它会跟随业界推荐值调整，不需要我们自己维护参数
_password_hasher = PasswordHash.recommended()

API_KEY_PREFIX = "hr"
API_KEY_BYTES = 32
API_KEY_PREFIX_LENGTH = 12
URL_TOKEN_BYTES = 24


def hash_password(password: str) -> str:
    """对用户密码做单向哈希，返回可直接入库的字符串。

    argon2 的输出自带盐值和参数（形如 $argon2id$v=19$m=65536,t=3,p=4$...），
    所以不需要额外存盐。同一密码每次哈希结果都不同，这是正常的。
    """
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码是否正确。

    pwdlib 内部用恒定时间比较，不会因为"前几个字符匹配"而提前返回，
    因此攻击者无法通过测量响应耗时逐位猜出密码。
    """
    return _password_hasher.verify(password, password_hash)


def hash_api_key(api_key: str) -> str:
    """计算 API Key 的 SHA-256 摘要，返回 64 位十六进制字符串。

    数据库中 api_key_hash 字段长度定为 64 就是为了匹配它。
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """生成一把新的 API Key。

    返回三元组 (明文, 哈希, 前缀)：

    - 明文：只在注册时的响应里返回这一次，之后系统任何地方都拿不到
    - 哈希：入库，后续所有请求靠它比对来鉴权
    - 前缀：入库，用于在界面上显示"这是我的哪一把 Key"，本身不能用来鉴权
    """
    plaintext = f"{API_KEY_PREFIX}_{secrets.token_urlsafe(API_KEY_BYTES)}"
    return plaintext, hash_api_key(plaintext), plaintext[:API_KEY_PREFIX_LENGTH]


def generate_url_token() -> str:
    """生成接收地址用的 token，会拼进 /ingest/{token}。

    用 secrets 而不是 uuid4 或 random：
    - secrets 取自操作系统的密码学随机源，不可预测
    - random 模块的默认随机源是可预测的，绝不能用于生成凭据
    - token_urlsafe 返回 URL 安全字符集，不需要再做转义
    """
    return secrets.token_urlsafe(URL_TOKEN_BYTES)
