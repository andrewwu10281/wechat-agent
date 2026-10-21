"""上下文预算与调用重试的单元测试（不花 API 费用）。

直接跑：
    .venv\\Scripts\\python test_context.py

覆盖点：
  1. estimate_tokens 粗估：中文按 1 字 1 token，英文按 4 字符 1 token
  2. _content_to_text 能处理字符串 / 积木列表
  3. compact_history：未超预算原样返回
  4. compact_history：超预算 → 压缩早期对话、保留最近几条、摘要被更新
  5. compact_history：摘要函数报错时降级丢弃，不抛异常
  6. _is_retryable：429/5xx/超时类可重试，400 这类参数错误不重试
  7. _create_message：临时失败会自动重试并最终成功；不可重试的错误立刻抛出
"""
import sys

import agent

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}   {detail}")


class FakeStatus(Exception):
    """模拟 anthropic SDK 带 status_code 的异常。"""

    def __init__(self, code: int):
        self.status_code = code
        super().__init__(f"status {code}")


class _FakeMessages:
    def __init__(self, seq):
        self.seq = seq
        self.calls = 0

    def create(self, **kwargs):
        idx = min(self.calls, len(self.seq) - 1)
        self.calls += 1
        item = self.seq[idx]
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, seq):
        self.messages = _FakeMessages(seq)


def _msg(role, text):
    return {"role": role, "content": [{"type": "text", "text": text}]}


def main() -> int:
    print("\n== 1. estimate_tokens ==")
    check("空字符串为 0", agent.estimate_tokens("") == 0)
    zh = agent.estimate_tokens("这是一句中文测试")
    check("中文约 1 字 1 token", 7 <= zh <= 10, zh)
    en = agent.estimate_tokens("abcdefgh" * 10)   # 80 个英文字符
    check("英文约 4 字符 1 token", 15 <= en <= 25, en)
    check("中文明显比等长英文贵", zh * 8 > en)

    print("\n== 2. _content_to_text ==")
    check("字符串原样返回", agent._content_to_text("你好") == "你好")
    blocks = [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]
    check("积木列表拼成文本", "第一段" in agent._content_to_text(blocks)
          and "第二段" in agent._content_to_text(blocks))
    check("空值不炸", agent._content_to_text(None) == "")

    print("\n== 3. compact_history：未超预算 ==")
    small = [_msg("user", "你好"), _msg("assistant", "你好呀")]
    hist, summ = agent.compact_history(small, "", budget=10000)
    check("条数不变", len(hist) == 2)
    check("摘要不产生", summ == "")

    print("\n== 4. compact_history：超预算会压缩 ==")
    # 造一段很长的历史：20 轮，每轮 500 字中文
    long_hist = []
    for i in range(20):
        long_hist.append(_msg("user", f"第{i}个问题：" + "测试内容" * 120))
        long_hist.append(_msg("assistant", f"第{i}个回答：" + "回复内容" * 120))
    before_tokens = agent._messages_tokens(long_hist)
    calls = {"n": 0}

    def fake_summarizer(messages, prev):
        calls["n"] += 1
        return (prev + f"｜摘要{calls['n']}（{len(messages)} 条）").strip("｜")

    hist, summ = agent.compact_history(long_hist, "", budget=3000, keep=4,
                                       summarizer=fake_summarizer)
    after_tokens = agent._messages_tokens(hist)
    check("历史确实变短了", len(hist) < len(long_hist), f"{len(hist)} vs {len(long_hist)}")
    check("token 明显下降", after_tokens < before_tokens, f"{after_tokens} vs {before_tokens}")
    check("保留了最近的消息", "第19个回答" in agent._content_to_text(hist[-1].get("content")))
    check("产生了摘要", bool(summ), summ[:60])
    check("摘要被多次合并", calls["n"] >= 1, calls)
    check("不超过预算太多", after_tokens + agent.estimate_tokens(summ) <= 3000 * 1.6,
          after_tokens + agent.estimate_tokens(summ))
    check("不改动传入的列表", len(long_hist) == 40)

    print("\n== 5. compact_history：摘要失败要降级而不是崩 ==")
    def broken_summarizer(messages, prev):
        raise RuntimeError("模型挂了")

    hist2, summ2 = agent.compact_history(long_hist, "", budget=3000, keep=4,
                                        summarizer=broken_summarizer)
    check("摘要失败也不抛异常", True)
    check("历史仍然被裁剪", len(hist2) < len(long_hist), len(hist2))
    check("摘要保持原值（空）", summ2 == "")

    print("\n== 6. _is_retryable ==")
    check("429 限流可重试", agent._is_retryable(FakeStatus(429)) is True)
    check("503 服务不可用可重试", agent._is_retryable(FakeStatus(503)) is True)
    check("400 参数错误不重试", agent._is_retryable(FakeStatus(400)) is False)
    check("超时异常可重试", agent._is_retryable(Exception("Request timeout")) is True)
    check("连接错误可重试", agent._is_retryable(Exception("Connection reset by peer")) is True)
    check("普通异常不重试", agent._is_retryable(ValueError("bad json")) is False)

    print("\n== 7. _create_message 重试逻辑 ==")
    orig_client, orig_sleep, orig_retries = agent.client, agent.time.sleep, agent.MAX_LLM_RETRIES
    try:
        agent.time.sleep = lambda s: None          # 不真的等
        agent.MAX_LLM_RETRIES = 3
        # 前两次 429，第三次成功
        agent.client = _FakeClient([FakeStatus(429), FakeStatus(429), "OK"])
        r = agent._create_message(model="x", max_tokens=1, messages=[])
        check("临时失败重试后成功", r == "OK", r)
        check("一共调用了 3 次", agent.client.messages.calls == 3, agent.client.messages.calls)

        # 不可重试的错误：只调用 1 次就抛
        agent.client = _FakeClient([FakeStatus(400), "OK"])
        raised = False
        try:
            agent._create_message(model="x", max_tokens=1, messages=[])
        except Exception:
            raised = True
        check("不可重试错误立刻抛出", raised)
        check("不可重试错误只调用 1 次", agent.client.messages.calls == 1, agent.client.messages.calls)

        # 一直失败：重试用尽后抛
        agent.client = _FakeClient([FakeStatus(503)])
        raised = False
        try:
            agent._create_message(model="x", max_tokens=1, messages=[])
        except Exception:
            raised = True
        check("重试用尽后抛出", raised)
        check("最多试 MAX_LLM_RETRIES 次", agent.client.messages.calls == 3, agent.client.messages.calls)
    finally:
        agent.client, agent.time.sleep, agent.MAX_LLM_RETRIES = orig_client, orig_sleep, orig_retries

    print("\n" + "=" * 46)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    print("=" * 46)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
