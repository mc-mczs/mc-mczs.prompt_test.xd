#!/usr/bin/env python3
"""
客服 FAQ 自动分类脚本
用途：对用户发来的问题进行自动分类，分配到对应的客服组
"""

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── API 配置 ──────────────────────────────────────────────
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)
MODEL = "deepseek-v4-flash"

# ── 重试配置 ──────────────────────────────────────────────
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # 秒，每次重试翻倍

# ── 并发配置 ──────────────────────────────────────────────
DEFAULT_WORKERS = 5

# ── 合法类别白名单 ────────────────────────────────────────
ALLOWED_CATEGORIES = frozenset({
    "退款退货",
    "物流查询",
    "账号问题",
    "商品咨询",
    "投诉建议",
    "其他",
})

# ── System Prompt ─────────────────────────────────────────
SYSTEM_PROMPT = """\
你是一个专业的客服问题分类器。你的任务是对用户问题进行精确分类，必须严格遵循以下规则。

## 分类标准（只能从以下6个类别中选择，禁止自创类别）

### 1. 退款退货
- 定义：用户要求退款、退货、换货，或咨询退款/退货的进度、流程、金额
- 典型场景："我要退货"、"钱什么时候退"、"怎么换货"、"退款进度查一下"、"退回来的钱少了"、"退货流程是什么"
- 边界规则：
  * "退款进度查询"、"退款什么时候到账" → 退款退货（关注点是钱，不是快递）
  * "退货的快递到哪了" → 退款退货（退货流程中的物流是附属诉求）
  * "怎么退货"、"退货要什么条件" → 退款退货（不是商品咨询）

### 2. 物流查询
- 定义：用户询问包裹运输位置、配送状态、预计送达时间、快递单号
- 典型场景："快递到哪了"、"什么时候能到"、"包裹显示签收但没收到"、"帮我查下物流"
- 边界规则：
  * 用户问的是"买东西"的物流 → 物流查询
  * 用户问的是"退货"的物流 → 归入退款退货
  * "修改收货地址" → 物流查询（涉及配送环节）

### 3. 账号问题
- 定义：用户遇到登录、注册、密码、账号安全、绑定/解绑手机号或邮箱等问题
- 典型场景："密码忘了"、"账号被锁了"、"怎么修改手机号"、"无法登录"、"实名认证失败"、"账号被盗"
- 边界规则：
  * "怎么注销账号" → 账号问题

### 4. 商品咨询
- 定义：用户在购买决策阶段询问商品信息、规格、库存、价格、使用方式
- 典型场景："这个有蓝色的吗"、"尺码怎么选"、"什么时候补货"、"和XX比哪个好"、"有优惠吗"
- 边界规则：
  * 发生在售前/购买决策阶段 → 商品咨询
  * 已购买后问"怎么用" → 仍是商品咨询（非售后投诉）
  * "退货流程是什么" → 退款退货（非商品咨询）

### 5. 投诉建议
- 定义：用户对服务、商品质量、物流体验表达不满或提出改进建议
- 典型场景："你们服务太差了"、"我要投诉"、"商品质量有问题"、"物流太慢了"、"建议加个XX功能"
- 边界规则：
  * 辱骂 + 具体投诉内容 → 投诉建议
  * 纯辱骂无实质内容 → 其他
  * 表达不满但同时有明确退款诉求（如"太差了，我要退款"）→ 以明确诉求为准，归退款退货

### 6. 其他
- 定义：不属于以上任何类别的输入
- 典型场景：纯闲聊、无法理解的表述、纯表情/符号、与购物完全无关的话题
- 使用条件：仅当其他5个类别全部不匹配时才使用

## 多类别优先级规则

当用户问题同时涉及多个类别时，按以下优先级判定：
1. 用户明确说"我要XX"→ 以动词指向的诉求为准（"我要退款" → 退款退货）
2. 问题中包含具体诉求关键词时，以诉求为准，情绪表达为辅
3. 无法判定主要诉求时 → 归入"其他"

## 输出格式（强制）

你必须输出且仅输出以下JSON格式，不得包含任何其他文字、标点或换行：

{"category":"<类别名称>","reasoning":"<一句话推理过程>"}

要求：
- category 的值必须严格等于上述6个类别名称之一：退款退货、物流查询、账号问题、商品咨询、投诉建议、其他
- reasoning 必须包含：用户核心诉求关键词 + 匹配到的类别判定依据，控制在30字以内
- 禁止输出类别名称以外的 category 值
- 禁止在JSON前后添加解释、markdown代码块标记或其他文本

## 示例

用户问题："我的钱什么时候退回来"
输出：{"category":"退款退货","reasoning":"核心诉求是退款到账时间，匹配退款退货"}

用户问题："快递显示签收了但我没收到货"
输出：{"category":"物流查询","reasoning":"关注包裹配送状态和签收异常，匹配物流查询"}

用户问题："你们APP做得太烂了，垃圾"
输出：{"category":"投诉建议","reasoning":"对产品表达不满且有具体指向，匹配投诉建议"}

用户问题："操你妈"
输出：{"category":"其他","reasoning":"纯辱骂无具体内容，归入其他"}

用户问题："我买的东西和退货的快递都到哪了"
输出：{"category":"退款退货","reasoning":"同时涉及物流和退货，退货为主诉求，归退款退货"}

用户问题："哈哈今天天气真好"
输出：{"category":"其他","reasoning":"与购物无关的闲聊，归入其他"}

用户问题：""
输出：{"category":"其他","reasoning":"输入为空，无法分类"}
"""


def _parse_response(raw: str) -> dict:
    """解析 LLM 返回的 JSON，带多层兜底。"""

    category = ""
    reasoning = ""

    # 第一层：直接解析
    try:
        data = json.loads(raw)
        category = str(data.get("category", "")).strip()
        reasoning = str(data.get("reasoning", "")).strip()
    except json.JSONDecodeError:
        # 第二层：从文本中提取 JSON 对象，应对模型包裹 markdown 代码块等情况
        match = re.search(r'\{[^{}]*"category"\s*:\s*"[^"]*"[^{}]*\}', raw)
        if match:
            try:
                data = json.loads(match.group())
                category = str(data.get("category", "")).strip()
                reasoning = str(data.get("reasoning", "")).strip()
            except json.JSONDecodeError:
                pass

    # 白名单校验
    if category not in ALLOWED_CATEGORIES:
        cleaned = re.sub(r'[，。！？、\s]', '', category)
        for allowed in ALLOWED_CATEGORIES:
            if cleaned == allowed:
                category = allowed
                break
        else:
            reasoning = f"解析失败，原始输出: {raw[:50]}"
            category = "其他"

    return {"category": category, "reasoning": reasoning}


def _call_api_with_retry(question: str) -> str:
    """调用 LLM API，带指数退避重试。

    可重试的错误：网络超时、连接断开、429 限流、5xx 服务端错误。
    不可重试的错误：401 认证失败、403 权限不足（直接抛出）。
    """
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"请对以下用户问题进行分类：\n\n{question}"},
                ],
                temperature=0,
            )
            return response.choices[0].message.content.strip()

        except Exception as e:
            last_error = e
            error_str = str(e).lower()

            # 不可重试的错误：认证/权限问题
            if any(kw in error_str for kw in ("401", "403", "invalid api key", "unauthorized")):
                raise

            if attempt < MAX_RETRIES:
                backoff = INITIAL_BACKOFF * (2 ** attempt)
                logger.warning(
                    "API 调用失败（第 %d/%d 次），%s 秒后重试：%s",
                    attempt + 1, MAX_RETRIES, backoff, e,
                )
                time.sleep(backoff)
            else:
                logger.error("API 调用重试 %d 次后仍失败：%s", MAX_RETRIES, e)

    raise last_error  # type: ignore[misc]


def classify_question(question: str) -> dict:
    """对单条用户问题进行分类，返回 {"category": str, "reasoning": str}。

    内置指数退避重试：网络抖动、限流(429)、服务端错误(5xx) 最多重试 3 次。
    """

    raw = _call_api_with_retry(question)
    logger.debug("LLM raw output: %s", raw)
    return _parse_response(raw)


# ── 线程安全的结果收集 ────────────────────────────────────
_save_lock = threading.Lock()


def _save_results(results: list, output_file: str) -> None:
    with _save_lock:
        # 按原始索引排序，保证输出顺序与输入一致
        results_sorted = sorted(results, key=lambda r: r.get("_index", 0))
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results_sorted, f, ensure_ascii=False, indent=2)


def _process_one(item: dict, index: int) -> dict:
    """处理单条问题，异常隔离。供线程池调用。"""
    try:
        question = str(item.get("question", "")).strip()
        if not question:
            result = {"category": "其他", "reasoning": "输入为空"}
        else:
            result = classify_question(question)
    except Exception as e:
        result = {"category": "其他", "reasoning": f"API异常: {str(e)[:30]}"}
        logger.error("第 %d 条处理失败（已重试 %d 次）: %s", index + 1, MAX_RETRIES, e)

    return {
        "_index": index,
        "id": item.get("id"),
        "question": str(item.get("question", "")).strip(),
        "predicted_category": result["category"],
        "reasoning": result.get("reasoning", ""),
        "error": result["reasoning"] if result["reasoning"].startswith("API异常") else None,
    }


def batch_classify(input_file: str, output_file: str, max_workers: int = DEFAULT_WORKERS) -> None:
    """批量分类，线程池并发处理，每 100 条增量写盘，单条异常隔离不中断。

    max_workers: 并发线程数，默认 5。建议不超过 API 限流阈值。
    """

    with open(input_file, "r", encoding="utf-8") as f:
        questions = json.load(f)

    total = len(questions)
    logger.info("开始批量分类，共 %d 条问题（并发线程: %d）", total, max_workers)

    results: list = []
    error_count = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_process_one, item, i): i
            for i, item in enumerate(questions)
        }

        for future in as_completed(futures):
            row = future.result()
            if row.get("error"):
                error_count += 1
                # 从结果中移除 error 内部字段，不写入输出
                del row["error"]
            results.append(row)
            completed += 1

            if completed % 100 == 0:
                _save_results(results, output_file)
                logger.info("已处理 %d/%d 条", completed, total)

    _save_results(results, output_file)

    if error_count:
        logger.warning("%d 条处理异常（已尽最大重试），已归入'其他'", error_count)
    logger.info("分类完成，共处理 %d 条问题，输出 %s", total, output_file)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python classifier.py <输入文件> <输出文件> [--workers N]")
        print("  --workers N  并发线程数，默认 5，建议不超过 10")
        sys.exit(1)

    workers = DEFAULT_WORKERS
    args = sys.argv[1:]
    if "--workers" in args:
        idx = args.index("--workers")
        workers = int(args[idx + 1])
        args.pop(idx + 1)
        args.pop(idx)

    batch_classify(args[0], args[1], max_workers=workers)
