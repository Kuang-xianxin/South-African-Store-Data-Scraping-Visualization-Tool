# 发布测试报告

执行时间：2026-07-21 13:01:19 +08:00  
操作系统：Windows  
Python：3.11.15

## 工具版本

| 工具 | 版本 |
|---|---:|
| pytest | 9.1.1 |
| Ruff | 0.15.22 |
| Mypy | 2.3.0 |
| build | 1.5.0 |
| Playwright | 1.61.0 |

## 最终命令与结果

```powershell
uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pytest --cov=takealot_ops --cov-report=term-missing --cov-report=json:coverage.json -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m build
```

结果：

- 测试：`123 passed`
- 端到端发布验收：`12 passed`
- Ruff：`All checks passed!`
- Mypy：`Success: no issues found in 28 source files`
- 构建：wheel 和 sdist 均成功
- PowerShell 计划任务脚本：语法解析通过
- Playwright Chromium：安装/检查命令退出码 0

## 覆盖率

| 范围 | 覆盖率 | 要求 | 结果 |
|---|---:|---:|---|
| 全部 `takealot_ops` | 90% | ≥85% | 通过 |
| `api/client.py` | 95% | ≥90% | 通过 |
| `storage/repository.py` | 97% | ≥90% | 通过 |
| `metrics/service.py` | 97% | ≥90% | 通过 |

## 构建产物

| 文件 | 大小 | SHA-256 |
|---|---:|---|
| `dist/takealot_ops-0.1.0-py3-none-any.whl` | 56,372 bytes | `6851927B33D685D615FDC00D16E7E0E480CEAFA697AFDCACB9FB19578F2C8A84` |
| `dist/takealot_ops-0.1.0.tar.gz` | 49,276 bytes | `070F221694B1692A61E2D7D34EE4FB5FAEE114466893CFEA534C6F8D31830211` |

## 验收点

- 10 SKU 数据库、HTML、Excel 汇总一致。
- 31 天、310 条流量快照与源 Offer 值一致。
- 两笔订单正确落入 SAST 日界线两侧。
- 重复订单项更新后只保留最新状态。
- 未知状态产生非零质量结果。
- 缺失流量保持缺失，库存为 0 的 Offer 可识别。
- 重复采集的业务表行数和内容哈希保持一致。
- HTML 无网络依赖；Excel 重复打开且图表关系完整。
- 输出和日志不包含测试 Key 或认证头。
- Windows 看板启动器退出后不残留后代进程或监听端口。

本报告不包含真实凭据或原始卖家数据。真实 API 只读联调结果将在用户本机配置 Key 后补充。
