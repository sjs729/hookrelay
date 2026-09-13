"""出站投递：把事件真正发到目标地址。

这个模块只负责"投一次"，不关心重试节奏（那是 retry.py 和 worker.py 的事）。
保持单一职责的好处是：投递的失败分类可以被单独测试，
而失败分类一旦判断错，后果很具体——把不该重试的错误反复重试，
或者把本该重试的临时故障直接判死。
"""

import logging
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, compute_signature

logger = logging.getLogger("hookrelay.delivery")

# 响应体只保留前 1KB。
# 投递记录是给人排查用的，前 1KB 足够看出对方返回了什么错误；
# 完整保存会让 delivery_attempts 表被少数返回巨大 HTML 错误页的下游撑爆。
RESPONSE_BODY_LIMIT = 1024

DELIVERY_ID_HEADER = "X-HookRelay-Delivery-Id"
ATTEMPT_HEADER = "X-HookRelay-Attempt"

# 这些头绝对不能从入站请求透传到出站请求。
#
# content-length 和 transfer-encoding 描述的是「原始请求体」的大小与传输方式，
# 而我们出站发送的 body 是重新序列化的：数据库里存的是 JSONB（已解析的结构），
# 取出来再用紧凑分隔符 dump 一次，字节数与上游发来的原始请求不同。
# 照搬这两个头会让 httpx 按错误的长度发请求，h11 在收尾时发现实际写入
# 的字节数少于声明值，直接抛 LocalProtocolError。
#
# 这个错误在本地开发时极难复现：上游请求体如果恰好等于重新序列化后的长度
# （比如键序和空格都没变），一切正常；一旦长度不等就整个 Worker 轮次异常。
#
# host 同理：它描述的是入站请求的目标主机，而出站的目标是另一个地址。
# 其余是 HTTP/1.1 的连接管理头（hop-by-hop），语义上只能作用于单次连接，
# 代理转发时必须剥掉。
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# 这两个 4xx 需要重试，其余 4xx 都是调用方的问题，重试没有意义
RETRYABLE_CLIENT_STATUS = frozenset({408, 429})


class FailureKind(StrEnum):
    """投递失败的分类。

    分类的目的不是给日志好看，而是决定"要不要重试"：
    网络抖动、下游临时过载、下游内部报错 —— 这些都值得再试；
    签名不匹配、接口路径写错、对方明确拒绝 —— 再试一百次也是一样的结果，
    只会浪费资源并拖长事件的生命周期。
    """

    NONE = "none"
    """成功。"""

    TIMEOUT = "timeout"
    """超时。下游可能正在处理但来不及响应，属于典型的临时故障。"""

    CONNECTION = "connection"
    """连接失败。DNS 解析不了、端口不通、TLS 握手失败等。"""

    CLIENT_ERROR = "client_error"
    """4xx。请求本身有问题，重试通常无意义（408 与 429 除外）。"""

    SERVER_ERROR = "server_error"
    """5xx。下游自己出错了，通常重试就能成功。"""

    UNEXPECTED = "unexpected"
    """未归类的异常情况。保留作为兜底，正常路径不会产生。"""


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """一次投递的结果。"""

    ok: bool
    kind: FailureKind
    status_code: int | None
    duration_ms: int
    response_body: str | None
    error: str | None

    @property
    def retryable(self) -> bool:
        """这次失败是否值得重试。

        成功当然不需要重试；能不能重试由失败类型决定。
        """
        if self.ok:
            return False
        return self.kind in {
            FailureKind.TIMEOUT,
            FailureKind.CONNECTION,
            FailureKind.SERVER_ERROR,
            FailureKind.UNEXPECTED,
        } or (self.kind is FailureKind.CLIENT_ERROR and self.status_code in RETRYABLE_CLIENT_STATUS)


def _build_headers(
    inbound_headers: dict[str, str],
    *,
    timestamp: str,
    signature: str,
    event_id: str,
    attempt: int,
) -> dict[str, str]:
    """组装出站请求头。

    在原始入站请求头的基础上覆盖几项。保留原始头是有用的：
    很多下游需要知道事件来源（GitHub 的 X-GitHub-Event、GitLab 的
    X-Gitlab-Event），这些信息我们不应该在转发时丢掉。

    但连接管理类的头必须剥掉，详见 _HOP_BY_HOP_HEADERS 的注释：
    其中 content-length 处理不当会让 httpx 发出发不出请求体长度的请求，
    直接导致投递全部失败。

    历史上已经入库的事件里可能存着 content-length，所以这道过滤放在出站层，
    而不是只在上游接收时过滤——否则旧事件重放时会再次撞上同样的问题。
    """
    forwarded = {
        name: value
        for name, value in inbound_headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS
    }

    return {
        **forwarded,
        "content-type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature,
        DELIVERY_ID_HEADER: event_id,
        ATTEMPT_HEADER: str(attempt),
    }


async def deliver_once(
    client: httpx.AsyncClient,
    *,
    target_url: str,
    secret: str,
    event_id: str,
    attempt: int,
    payload_bytes: bytes,
    inbound_headers: dict[str, str],
    timeout_seconds: float,
) -> DeliveryResult:
    """向下游投递一次，返回本次结果。

    关于请求体：数据库里存的是 JSONB（已解析的结构化数据），
    这里重新序列化后再发送。这么做是为了让 payload 可查询、可索引，
    代价是发送的字节与原始请求不完全一致——换行、键顺序、空格都可能不同。

    这也解释了为什么入站校验签名必须用**原始字节**而不能用解析后的数据：
    一旦重新序列化，签名就对不上了。

    关于重定向：显式关闭自动跟随。跟随 3xx 会把请求体连同签名一起
    发到另一台主机，而那个主机不一定是调用方预期的地方。
    投递目标应该由配置决定，不该由下游的响应决定。
    """
    timestamp = str(int(time.time()))
    signature = compute_signature(secret, timestamp, payload_bytes)

    headers = _build_headers(
        inbound_headers,
        timestamp=timestamp,
        signature=signature,
        event_id=event_id,
        attempt=attempt,
    )

    started = time.perf_counter()

    try:
        response = await client.post(
            target_url,
            content=payload_bytes,
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
    except httpx.TimeoutException as exc:
        # 必须先于 HTTPError 捕获：TimeoutException 是它的子类
        return DeliveryResult(
            ok=False,
            kind=FailureKind.TIMEOUT,
            status_code=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            response_body=None,
            error=f"请求超时（{timeout_seconds}s）: {type(exc).__name__}",
        )
    except httpx.HTTPError as exc:
        # 连接被拒、DNS 解析失败、TLS 握手失败等
        return DeliveryResult(
            ok=False,
            kind=FailureKind.CONNECTION,
            status_code=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            response_body=None,
            error=f"连接失败: {type(exc).__name__}: {exc}",
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    # 截断而不是丢弃：出错时对方返回的错误信息往往就在响应体里
    body_snippet = response.text[:RESPONSE_BODY_LIMIT] if response.text else None

    if 200 <= response.status_code < 300:
        return DeliveryResult(
            ok=True,
            kind=FailureKind.NONE,
            status_code=response.status_code,
            duration_ms=duration_ms,
            response_body=body_snippet,
            error=None,
        )

    if 300 <= response.status_code < 400:
        # 归为不可重试：重定向说明 target_url 配错了，重试一百次还是会重定向。
        # 让它直接进死信并在查询接口显示这条明确的错误，用户改完配置重放即可。
        # 反过来如果当成可重试，这条事件会白白跑完整个重试预算才停下。
        return DeliveryResult(
            ok=False,
            kind=FailureKind.CLIENT_ERROR,
            status_code=response.status_code,
            duration_ms=duration_ms,
            response_body=body_snippet,
            error=f"目标地址返回重定向 {response.status_code}，请检查配置的地址是否正确",
        )

    if response.status_code >= 500:
        return DeliveryResult(
            ok=False,
            kind=FailureKind.SERVER_ERROR,
            status_code=response.status_code,
            duration_ms=duration_ms,
            response_body=body_snippet,
            error=f"目标服务返回 {response.status_code}",
        )

    return DeliveryResult(
        ok=False,
        kind=FailureKind.CLIENT_ERROR,
        status_code=response.status_code,
        duration_ms=duration_ms,
        response_body=body_snippet,
        error=f"目标服务拒绝了请求（{response.status_code}）",
    )
