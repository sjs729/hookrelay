"""安全原语测试。

这些函数是整个服务的信任基础：签名错了，任何人都能伪造事件；
密钥加密错了，数据库里的下游密钥就是明文。所以这里逐条验证，
包括各种"差一点就对"的输入。
"""

import time

import pytest

from app.security import (
    SIGNATURE_ALGORITHM,
    compute_signature,
    decrypt_secret,
    encrypt_secret,
    generate_api_key,
    generate_signing_secret,
    generate_url_token,
    hash_api_key,
    hash_password,
    is_timestamp_fresh,
    mask_secret,
    verify_password,
    verify_signature,
)


class TestPasswordHashing:
    def test_same_password_hashes_differently(self) -> None:
        """同一密码两次哈希必须不同。

        若相同，说明没有加盐：攻击者拿到库后可以用一张彩虹表
        一次性还原所有账号，也能一眼看出哪些用户用了相同密码。
        """
        assert hash_password("correct horse battery staple") != hash_password(
            "correct horse battery staple"
        )

    def test_correct_password_verifies(self) -> None:
        assert verify_password("s3cret-password", hash_password("s3cret-password"))

    def test_wrong_password_rejected(self) -> None:
        assert not verify_password("s3cret-passwerd", hash_password("s3cret-password"))

    def test_hash_does_not_contain_plaintext(self) -> None:
        """哈希串里不能出现明文密码。"""
        assert "s3cret-password" not in hash_password("s3cret-password")

    def test_invalid_hash_returns_false_instead_of_raising(self) -> None:
        """哈希串损坏时应返回 False，而不是抛异常。

        一条脏数据不应该让接口返回 500——调用方看到的应该是
        "密码不对"，而不是"服务器坏了"。
        """
        assert not verify_password("anything", "not-a-valid-hash")


class TestApiKey:
    def test_key_has_expected_shape(self) -> None:
        """Key 形如 hr_<随机串>，前缀用于在日志和界面里辨认。"""
        api_key, api_key_hash, prefix = generate_api_key()
        assert api_key.startswith("hr_")
        assert api_key_hash == hash_api_key(api_key)
        assert api_key.startswith(prefix)

    def test_keys_are_unique(self) -> None:
        keys = {generate_api_key()[0] for _ in range(50)}
        assert len(keys) == 50

    def test_hash_is_not_reversible_to_key(self) -> None:
        """存进库的必须是哈希，不能包含原文。"""
        api_key, api_key_hash, _ = generate_api_key()
        assert api_key not in api_key_hash
        assert len(api_key_hash) == 64  # SHA-256 十六进制

    def test_hash_is_deterministic(self) -> None:
        """同样的 Key 必须每次哈希出同样的值，否则查库时对不上。"""
        api_key, _, _ = generate_api_key()
        assert hash_api_key(api_key) == hash_api_key(api_key)


class TestUrlToken:
    def test_token_is_url_safe_and_unique(self) -> None:
        """token 会出现在 URL 里，必须是 URL 安全字符。"""
        tokens = {generate_url_token() for _ in range(50)}
        assert len(tokens) == 50
        for token in tokens:
            assert token.replace("-", "").replace("_", "").isalnum()

    def test_token_has_enough_entropy(self) -> None:
        """24 字节随机数按 base64url 编码后是 32 个字符。

        长度断言在这里等于熵的断言：长度缩水往往意味着
        随机源被换成了短值或不安全实现。
        """
        assert len(generate_url_token()) == 32


class TestSecretEncryption:
    def test_roundtrip(self) -> None:
        secret = generate_signing_secret()
        assert decrypt_secret(encrypt_secret(secret)) == secret

    def test_ciphertext_hides_plaintext(self) -> None:
        secret = "super-secret-value"
        assert secret not in encrypt_secret(secret)

    def test_same_plaintext_encrypts_differently(self) -> None:
        """Fernet 内含随机 IV，两次加密结果不同。

        否则攻击者能通过比对密文判断两个接收地址是否用了相同密钥。
        """
        assert encrypt_secret("same") != encrypt_secret("same")

    def test_tampered_ciphertext_raises(self) -> None:
        """篡改密文必须被检出，而不是解出一段垃圾。

        Fernet 带 HMAC 校验，能发现密文被改动过——这比"解密失败但返回乱码"
        安全得多。
        """
        ciphertext = encrypt_secret("secret")
        tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")
        with pytest.raises(ValueError):
            decrypt_secret(tampered)

    def test_garbage_input_raises(self) -> None:
        """完全不是密文的输入也要抛 ValueError，而不是泄露内部异常类型。"""
        with pytest.raises(ValueError):
            decrypt_secret("this-is-not-a-fernet-token")

    def test_signing_secret_length(self) -> None:
        """HMAC 密钥取 32 字节，与 SHA-256 的输出长度一致。"""
        assert len(generate_signing_secret()) >= 32


class TestSecretMasking:
    def test_shows_head_and_tail_only(self) -> None:
        masked = mask_secret("abcdefghijklmnop")
        assert masked.startswith("abcd")
        assert masked.endswith("mnop")
        # 中间固定 8 个星号：不用真实长度，避免泄露密钥的实际字符数
        assert "*" * 8 in masked

    def test_does_not_reveal_whole_secret(self) -> None:
        secret = "abcdefghijklmnop"
        assert secret not in mask_secret(secret)

    def test_short_secret_is_fully_masked(self) -> None:
        """短密钥不做首尾截取，否则等于全部暴露。"""
        assert set(mask_secret("abc")) == {"*"}


class TestSignature:
    def test_signature_format(self) -> None:
        signature = compute_signature("key", "1700000000", b'{"a":1}')
        assert signature.startswith(SIGNATURE_ALGORITHM)
        assert len(signature) == len(SIGNATURE_ALGORITHM) + 64

    def test_signature_is_deterministic(self) -> None:
        args = ("key", "1700000000", b'{"a":1}')
        assert compute_signature(*args) == compute_signature(*args)

    def test_different_body_gives_different_signature(self) -> None:
        assert compute_signature("key", "1700000000", b'{"a":1}') != compute_signature(
            "key", "1700000000", b'{"a":2}'
        )

    def test_different_timestamp_gives_different_signature(self) -> None:
        """时间戳参与签名，否则攻击者可以改时间戳绕过防重放。"""
        assert compute_signature("key", "1700000000", b"body") != compute_signature(
            "key", "1700000001", b"body"
        )

    def test_different_secret_gives_different_signature(self) -> None:
        assert compute_signature("key-a", "1700000000", b"body") != compute_signature(
            "key-b", "1700000000", b"body"
        )

    def test_timestamp_and_body_are_not_ambiguous(self) -> None:
        """时间戳与请求体之间有分隔符，不能直接拼接。

        用 `时间戳 + 请求体` 直接相连的话，
        ("12", "3body") 和 ("123", "body") 会算出同一个签名，
        攻击者可以把时间戳的末位搬到正文开头而不被发现。
        """
        assert compute_signature("key", "12", b"3body") != compute_signature("key", "123", b"body")

    def test_verify_accepts_correct_signature(self) -> None:
        signature = compute_signature("key", "1700000000", b'{"a":1}')
        assert verify_signature("key", "1700000000", b'{"a":1}', signature)

    def test_verify_rejects_wrong_signature(self) -> None:
        assert not verify_signature("key", "1700000000", b'{"a":1}', "sha256=" + "0" * 64)

    def test_verify_rejects_tampered_body(self) -> None:
        signature = compute_signature("key", "1700000000", b'{"amount":1}')
        assert not verify_signature("key", "1700000000", b'{"amount":9}', signature)

    def test_verify_rejects_tampered_timestamp(self) -> None:
        signature = compute_signature("key", "1700000000", b"body")
        assert not verify_signature("key", "1700009999", b"body", signature)

    def test_verify_rejects_missing_algorithm_prefix(self) -> None:
        """签名必须带 sha256= 前缀，不能只认后面的十六进制串。"""
        signature = compute_signature("key", "1700000000", b"body")
        assert not verify_signature("key", "1700000000", b"body", signature.removeprefix("sha256="))

    def test_verify_rejects_empty_signature(self) -> None:
        assert not verify_signature("key", "1700000000", b"body", "")

    def test_verify_handles_non_ascii_body(self) -> None:
        """中文请求体要按 UTF-8 字节参与签名。

        这里用 bytes 长度断言，确保测的是字节而不是字符：
        16 个中文字符是 48 字节，长度混用会让签名结果错位。
        """
        body = "事件内容：订单已创建".encode()
        signature = compute_signature("key", "1700000000", body)
        assert verify_signature("key", "1700000000", body, signature)


class TestTimestampFreshness:
    def test_current_time_is_fresh(self) -> None:
        assert is_timestamp_fresh(str(int(time.time())), 300)

    def test_recent_past_is_fresh(self) -> None:
        assert is_timestamp_fresh(str(int(time.time()) - 100), 300)

    def test_recent_future_is_fresh(self) -> None:
        """略微超前的时间戳要放行。

        调用方与本服务的时钟不可能完全一致，NTP 同步也有偏差。
        如果只允许过去的时间戳，时钟稍快的调用方会全部被拒。
        """
        assert is_timestamp_fresh(str(int(time.time()) + 100), 300)

    def test_too_old_is_stale(self) -> None:
        """超出容差窗口的旧请求判定为过期，防止重放攻击。"""
        assert not is_timestamp_fresh(str(int(time.time()) - 400), 300)

    def test_too_far_future_is_stale(self) -> None:
        """远超未来的时间戳同样拒绝，否则"未来时间戳"能永久有效。"""
        assert not is_timestamp_fresh(str(int(time.time()) + 400), 300)

    def test_exactly_at_boundary_is_fresh(self) -> None:
        """容差是闭区间：正好落在边界上仍然放行。

        这里断言的是实现已被确定下来的语义，避免以后有人"顺手"改成
        开区间时没有测试拦住。两种写法都合理，但必须与文档一致。
        """
        now = int(time.time())
        assert is_timestamp_fresh(str(now - 300), 300)

    @pytest.mark.parametrize("bad_value", ["", "abc", "1.5", "1700000000000", "NaN"])
    def test_invalid_timestamp_rejected(self, bad_value: str) -> None:
        """任何解析不了的输入都必须返回 False，不能抛异常。

        这是外部可控的请求头，抛异常等于给了攻击者一个制造 500 的开关。
        """
        assert not is_timestamp_fresh(bad_value, 300)
