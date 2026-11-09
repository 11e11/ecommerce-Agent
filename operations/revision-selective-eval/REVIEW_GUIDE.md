# 进阶阶段检查入口与场景输出

## 先看这些文件

1. [设计和复用边界](../revision-stage-two.md)
2. [Baseline → 进阶逐字稿](../revision-stage-two-interview.md)
3. [三组同批真实评测](README.md)
4. [固定真实首轮及输入数据](initial.json)
5. [六个角色输出与复用来源](example-held-01-stage_two.json)
6. 对照 held-01-baseline.json、held-01-stage_one.json、held-01-stage_two.json。

## 一个具体输出示例

用户：“Amazon 广告预算先别加了，请仅重写 Amazon 库存这一条，Shopify 那边不要动。”

实际执行计划：

```json
{
  "mode": "selective",
  "decisions": [
    {"agent": "platform_amazon_operator", "action": "execute", "reason": "target_modified"},
    {"agent": "platform_shopify_operator", "action": "reuse", "reason": "dependencies_valid"}
  ]
}
```

该次输出中 Analyst、Amazon Specialist、跨平台协调、Manager、Reviewer 实际执行；Shopify 的任务以复用完成，没有新的模型执行。

Manager 对 Shopify 的说明以“Shopify 专员本轮为复用既有发现，未重新执行计划”开头，保留原来的 partial 和缺口。Amazon 库存建议更新，其他建议对象与首轮相同。原始角色结果、规则结果都能在样例文件中核对，不是固定 Demo 响应。

## 实测收益与代价

| 指标（六条表达） | 文本反馈 baseline | 完整重跑 | 进阶选择执行 |
|---|---:|---:|---:|
| 范围规则通过 | 0/6 | 6/6 | 6/6 |
| 预算规则通过 | 6/6 | 6/6 | 6/6 |
| 模型语义评分通过 | 6/6 | 6/6 | 6/6 |
| Shopify 实际复用 | 0/6 | 0/6 | 6/6 |
| 模型调用中位数 | 10 | 11 | 8 |
| 输入 token 中位数 | 72,862.5 | 82,014.5 | 63,828.5 |
| 耗时中位数（秒） | 65.8 | 71.3 | 70.9 |

相对 baseline，调用减少 20%、输入 token 中位数减少约 12.4%。相对完整重跑，调用减少约 27.3%、输入 token 中位数减少约 22.2%。没有把 token 减少直接当成账单金额，也没有证明延迟降低。

## 如何核对“安全复用”

- tests/test_reuse.py 验证内容/时间、模型、约束、租户、完整性、跨平台依赖和旧工具上下文等失效路径。
- tests/test_revision_integration.py 验证实际跳过 Shopify，attempt_count=0、无 started 事件；也验证快照缺失时两个平台均重跑。
- execution_plan 只是计划；统计依据真实 started/reused 事件，避免 Reviewer 返修后仍把它误算成跳过。
- 结果是对相同输入报表的分析复用，不保证店铺实时数据没变。部分缺口不被消除或隐藏；未知/不足结果不复用。
- 规则与模型评分分列。生成和评分使用同一型号模型，语义评分存在共享偏差；六条同义表达不能外推为广泛成功率。

## 开发过程保留

development-attempts 保存了首条开发对照。原型已减少到 8 次调用，但 Manager 将可修改的库存建议也原样抄回，范围规则失败，Reviewer 因此拒绝。修复后要求在目标建议内回应反馈：完善步骤或解释复核结果；不得为制造变化编造数字。随后冻结实现，再运行四条保留测试。

本轮三组正式结果共享同一父运行、数据集和执行合同，均真实运行；不以旧版本基础评测的耗时代替当前对照。外部工具统一禁用以隔离反馈处理，不能推断 RAG 或动态工具任务的收益。

相关回归 120 项通过。API 默认 selective，可用 execution_mode=full 回退。未实现最终阶段的长期记忆。
