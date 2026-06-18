# 客服FAQ自动分类 — LLM Prompt 优化

对客服FAQ自动分类脚本进行全链路优化，准确率从线上不稳定状态提升至 **100%（30/30）**，消除偶发解析报错，增加异常容错与并发处理。

## 改进思路

```
Code Review 诊断 → Prompt 重设计 → 代码加固 → 并发优化 → 回归验证
```

| 阶段 | 动作 | 效果 |
|------|------|------|
| **Prompt 重设计** | System Prompt 加入 6 类完整定义、边界互斥规则、JSON 强约束、Few-shot 示例 | 边界场景（#23 "抱怨退货流程"）从错分为"退款退货"修正为"投诉建议"，准确率 96.7%→100% |
| **JSON 结构化输出** | 强制 `{"category":"...","reasoning":"..."}` + 三层解析兜底 + 白名单校验 | 杜绝 LLM 输出标点/前缀/不存在的类别，消除线上解析报错 |
| **指数退避重试** | API 调用失败自动重试 3 次（1s→2s→4s），401/403 立即停止 | 消除网络抖动、429 限流导致的批处理崩溃 |
| **并发批处理** | `ThreadPoolExecutor` 多线程并发 + 每 100 条增量写盘 + 线程安全锁 | 30 条从 27s 降至 10s（2.7x），大批量效果更显著 |
| **安全加固** | API Key 从硬编码默认值 → 环境变量注入，缺失直接报错 | 避免密钥泄露至 Git 仓库 |

**新旧版效果对比**（30 条边界测试集）：

| 指标 | 旧版 | 新版 |
|------|:---:|:---:|
| 分类准确率 | 96.7% (29/30) | **100% (30/30)** |
| 输出格式 | 裸文本（偶发带标点/前缀） | 结构化 JSON + reasoning |
| 异常重试 | 无 | 指数退避 3 次 |
| 并发 | 串行 | 5 线程（2.7x） |
| API Key | 硬编码在源码 | 环境变量注入 |

## 运行

### 安装

```bash
pip install openai
```

### 配置 API Key

```bash
# Linux / macOS
export DEEPSEEK_API_KEY="sk-你的密钥"

# Windows PowerShell
$env:DEEPSEEK_API_KEY="sk-你的密钥"

# Windows CMD
set DEEPSEEK_API_KEY=sk-你的密钥
```

### 运行

```bash
# 批量分类（默认 5 线程并发）
python task1_classifier.py task1_test_samples.json output.json

# 自定义线程数
python task1_classifier.py task1_test_samples.json output.json --workers 10

# 运行旧版对比
python task1_classifier_old.py task1_test_samples.json output_old.json
```

**输入格式** (`task1_test_samples.json`)：

```json
[
  {"id": 1, "question": "我的快递到哪了"},
  {"id": 2, "question": "怎么退货"}
]
```

**输出格式** (`output.json`)：

```json
[
  {
    "id": 1,
    "question": "我的快递到哪了",
    "predicted_category": "物流查询",
    "reasoning": "核心诉求是查询包裹位置，匹配物流查询"
  }
]
```

## AI 工具使用

本项目全程由 Claude Code 辅助完成：

| 步骤 | AI 工具角色 |
|------|-----------|
| **Code Review** | 逐行审查 `task1_classifier.py`，按 P0/P1/P2 三级输出 10 项缺陷（根因 + 业务影响 + 复现场景） |
| **Prompt 重设计** | 读取 `categories.md` + `task1_prompt.md`，生成新版 System Prompt（类别定义/边界规则/JSON 约束/Few-shot/兜底规则） |
| **代码集成** | 将 Prompt 写入代码，添加 `_parse_response()` 解析兜底、异常隔离、增量写盘、logging |
| **追加优化** | 基于已发现缺陷，添加指数退避重试 + ThreadPoolExecutor 并发处理 |
| **回归验证** | 运行 3 次旧版 + 3 次新版对比，定位旧版 #23 边界错分，确认新版 100% |
| **文档输出** | 生成本 README |
