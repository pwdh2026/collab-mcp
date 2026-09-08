# 项目周会纪要 2026-07-28

## 议题一：Python 异步框架选型

讨论了 Python 异步框架的选型问题，候选包括 FastAPI、aiohttp 与 Starlette。
结论：后端统一采用 FastAPI + asyncpg，数据库连接池使用 SQLAlchemy 异步驱动。

## 议题二：检索服务优化

确定下一步对 search_documents 做中文语料实测，重点验证 trigram 分词在中文场景的表现。
