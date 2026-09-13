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

安全原则提醒：本模块的函数不向日志输出任何凭据内容。
密钥类数据一旦进了日志文件，就等于泄露了。
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.config import get_settings

logger = logging.getLogger("hookrelay.security")
settings = get_settings()

# argon2 的具体参数由 pwdlib 的 recommended() 决定，
# 它会跟随业界推荐值调整，不需要我们自己维护参数
_password_hasher = PasswordHash.recommended()

API_KEY_PREFIX = "hr"
API_KEY_BYTES = 32
API_KEY_PREFIX_LENGTH = 12
URL_TOKEN_BYTES = 24
SIGNING_SECRET_BYTES = 32


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

    哈希串无法识别时返回 False，而不是向上抛异常：password_hash 来自数据库，
    可能因为历史实现或人工修改而损坏。此时调用方应该得到"密码不正确"，
    而不是一个 500；异常类型本身也会暴露哈希实现细节。
    """
    try:
        return _password_hasher.verify(password, password_hash)
    except UnknownHashError:
        logger.warning("密码哈希无法识别，判定为校验失败")
        return False


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


def generate_signing_secret() -> str:
    """生成 endpoint 的 HMAC 签名密钥（明文，入库前需先加密）。"""
    return secrets.token_urlsafe(SIGNING_SECRET_BYTES)


# ============================================================
# 对称加密：保护 endpoint 的签名密钥
# ============================================================


def _derive_fernet_key(secret_key: str) -> bytes:
    """从应用总密钥派生出专用于 Fernet 的子密钥。

    为什么不直接拿 SECRET_KEY 当 Fernet 密钥用？
    同一个密钥被多个用途共用（这里加密 endpoint 密钥，将来可能还要签别的），
    一旦某个用途的实现出问题，影响会横向扩散到其余所有用途。
    用 HKDF 做一次密钥派生、给每个用途分配独立子密钥，能把风险限制在单点。
    这正是 HKDF 的设计初衷，info 参数就是用来区分用途的标签。

    派生结果固定 32 字节，再转成 Fernet 要求的 urlsafe base64 形式。
    """
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"hookrelay:endpoint-secret:v1",
    ).derive(secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


_fernet = Fernet(_derive_fernet_key(settings.secret_key))


def encrypt_secret(plaintext: str) -> str:
    """加密 endpoint 的签名密钥，返回可直接入库的字符串。

    Fernet 保证密文带完整性校验（AES-CBC + HMAC），被篡改的密文解密时会直接失败，
    不会解出一段看似正常实则错误的内容。
    """
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """解密 endpoint 的签名密钥。"""
    try:
        return _fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # 最常见的原因是 SECRET_KEY 被换过：旧密文用新密钥解不开
        raise ValueError("密钥解密失败：SECRET_KEY 可能已被更换") from exc


def mask_secret(plaintext: str) -> str:
    """生成密钥掩码，供接口响应里展示。

    只保留前 4 位和后 4 位，中间用固定长度的星号填充。
    重点是**星号数量与真实长度无关**：如果按真实长度补星号，
    长度本身就成了泄露信息，攻击者能据此缩小暴力枚举范围。
    """
    if len(plaintext) <= 10:
        return "*" * 8
    return f"{plaintext[:4]}{'*' * 8}{plaintext[-4:]}"


# ============================================================
# HMAC 签名：验证入站请求的真实性与时效性
# ============================================================

SIGNATURE_HEADER = "X-HookRelay-Signature"
TIMESTAMP_HEADER = "X-HookRelay-Timestamp"
IDEMPOTENCY_HEADER = "X-HookRelay-Idempotency-Key"
SIGNATURE_ALGORITHM = "sha256="


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """计算 HMAC-SHA256 签名，返回形如 `sha256=<hex>` 的完整值。

    签名对象是「时间戳 + "." + 请求体」，而不是只签请求体本身。
    把时间戳纳入签名有两个作用：

    1. 防篡改：攻击者改了时间戳就对不上签名，无法把旧请求"改成新的"再重放。
    2. 防篡改请求体：请求体任何一字节的改动都会导致签名完全不同。

    中间那个 "." 是分隔符，不能省。否则攻击者可以把时间戳和请求体
    重新拼接成另一组能通过校验的组合（拼接歧义攻击）。
    """
    message = timestamp.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_ALGORITHM}{digest}"


def verify_signature(secret: str, timestamp: str, body: bytes, provided: str) -> bool:
    """校验签名是否正确。

    必须用 hmac.compare_digest 而不是 `==`：
    普通字符串比较会在遇到第一个不同字符时立即返回，比较耗时随"匹配前缀长度"
    变化。攻击者通过反复测量响应时间，可以逐字节猜出正确签名。
    compare_digest 无论内容如何都遍历完整长度，消除这个时间侧信道。
    """
    expected = compute_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, provided)


def is_timestamp_fresh(timestamp: str, tolerance_seconds: int) -> bool:
    """判断请求时间戳是否落在容差窗口内。

    容差窗口是双重考虑：
    - 太严：调用方与本服务存在时钟偏差时会被误拒
    - 太松：重放攻击的有效时间窗口被拉长

    用绝对值比较（而不是只判断"是否太旧"），同时拒绝时间戳在未来的请求。
    否则攻击者可以把时间戳设到很远的未来，让这个请求在很长时间内都能被重放。
    """
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(int(time.time()) - ts) <= tolerance_seconds
