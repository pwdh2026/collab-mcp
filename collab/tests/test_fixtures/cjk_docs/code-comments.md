# 代码注释样例

```python
# 异步重试逻辑：指数退避（exponential backoff）
async def retry_async(fn, max_retries=3):
    """执行异步函数并自动重试。"""
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
```

说明：以上注释演示了中英混排场景，中文说明 + 英文专有名词（exponential backoff）。
