# 基础阶段反馈修订评测

一个固定业务案例，六种反馈表达：2 条开发、4 条保留测试。所有变体使用同一份真实模型首轮报告。

基线：文本反馈追加后完整重跑，统一输出字段但不做结构化需求更新或范围合并。基础阶段：解释需求、完整重跑、按范围合并、规则检查与模型审核。

以下通过数由规则读取结构化报告计算；语义合格由另一次 LLM 调用判定，不等于人工真值。错误或缺失结果计为未通过。

| 集合 | 变体 | 范围正确 | 预算字段遵守 | LLM语义合格 | 系统审核通过 | 执行错误 |
|---|---|---|---|---|---|---|
| 开发 | 文本反馈基线 | 0/2 | 2/2 | 2/2 | 2/2 | 0 |
| 开发 | 基础阶段 | 2/2 | 2/2 | 2/2 | 2/2 | 0 |
| 保留测试 | 文本反馈基线 | 0/4 | 4/4 | 4/4 | 4/4 | 0 |
| 保留测试 | 基础阶段 | 4/4 | 4/4 | 4/4 | 4/4 | 0 |
| 全部 | 文本反馈基线 | 0/6 | 6/6 | 6/6 | 6/6 | 0 |
| 全部 | 基础阶段 | 6/6 | 6/6 | 6/6 | 6/6 | 0 |

## 执行开销

工作流开销包含需求解释，排除单独的评测评分调用与共享首轮生成。token 是服务返回的已知用量；传输中断可能无法取得用量。基础阶段不做局部重算，不预期必然降本。

| 变体 | 模型调用中位数 | 工具调用中位数 | 输入token中位数 | 输出token中位数 | 耗时中位数(ms) |
|---|---|---|---|---|---|
| baseline | 10.0 | 0.0 | 72338.5 | 12780.5 | 65359.5 |
| stage_one | 11.0 | 0.0 | 81502.5 | 12863.5 | 66539.0 |

## 逐条结果

| 用例 | 变体 | 规则 | 语义 | 说明 |
|---|---|---|---|---|
| dev-01 | baseline | revision_scope=False, budget_constraint=True | True | 修订版满足反馈：Amazon 库存建议（amazon:inventory:1）改为先核验在途/补货记录再定量，并明确标注日均10件、覆盖1.2天为基于 amazon-sales/amazon-stock 的假设推算，未给确定采购量；Amazon 广告（amazon:advertising:1）明确 budget_change=no_increase、维持预算不增加，符合“不增预算”；Shopify 广告与库存建议实质保持原样（仍为维持预算、核验在途与安全库存），未被实质改动。文本与元数据一致：广告类 category=advertising 且 budget_change=no_increase，库存类 budget_change=not_applicable，正文表述与之一致。未引入无依据的业务事实：ACOS 50%、单次订单成本10 USD、广告订单占比14.3%、Shopify 80÷5≈16天等均为对已给数字的透明推导并标注假设；新鲜度0.0天、comparability blockers、can_compare_across_platforms=false 等均来自 system_context 审计元数据，非臆造店铺观察。仅一处小瑕疵：limitations 中“Shopify 需补同款商品销量、listing 标题与五点描述原文”与已提供的 shopify-product 销量/标题略有出入，但不构成新增业务事实，不影响判定。 |
| dev-01 | stage_one | revision_scope=True, budget_constraint=True | True | 修订版满足反馈：Amazon 广告建议 amazon:advertising:1 明确 budget_change=no_increase 且标题/why_now 均为“维持预算、先核验”，未建议加预算；仅 amazon:inventory:1 被更新（confidence 升为 high、增加“加急补货”动作），其余 Shopify 广告/库存与 listing 建议实质未变，revision_changes 标注 preserved 与正文一致。文本与元数据一致：库存类 budget_change=not_applicable、广告类 no_increase，与正文表述相符。无新增无据事实：12/10≈1.2 天、80÷5≈16 天、ACOS=100/200=50%、单次订单成本=100/10=10 USD、10/70≈14.3% 均为对 amazon-stock/amazon-sales/amazon-ads/shopify-product 数值的透明推导，且明确标注“假设七天日均代表当前需求”为假设而非观测；service_level、ecom-inventory 等属系统元数据/技能参数，非编造店铺观察；未给出确定采购数量，符合缺在途库存的披露。仅一处小瑕疵：limitations 称“earliest 等于 latest”与 system_context 观测窗口一致，无矛盾。 |
| dev-02 | baseline | revision_scope=False, budget_constraint=True | True | 修订版确实只调整了 Amazon 库存方案（amazon:inventory:1 增加了在途核验与补货触发动作），Amazon 广告明确 budget_change=no_increase 且正文写“维持现有预算不追加”，Shopify 两条建议标注“沿用上一版”且内容与原文一致，符合反馈要求。文本与元数据一致：Amazon 广告 category=advertising、budget_change=no_increase，正文“不追加预算”相符；库存项 budget_change=not_applicable 与“不给确定采购数量”一致。无新增无依据事实：日均 10 件/天（70÷7）、覆盖 1.2 天（12÷10）、ACOS 50%（100/200）、单次归因订单成本 10 USD（100/10）、Shopify 约 16 天（80/5）均为对已给数字的透明推导；约 58 件（5.8 天×10）明确标注为“基于假设的推导值而非报表观测事实”，未作为采购数量；新鲜度 0.0 天、can_compare_across_platforms=false、adequacy=unknown 等均来自 system_context 审计元数据，非编造店铺观测。仅存轻微瑕疵：limitations 中“约 58 件”与“约 16 天”属推算值，但已显式标注为推导/推算，不构成无依据事实。 |
| dev-02 | stage_one | revision_scope=True, budget_constraint=True | True | 修订版仅更新 amazon:inventory:1，其余四条（含 amazon:advertising:1 与两条 Shopify 建议）在 revision_changes 标为 preserved 且正文实质未变，符合“只调 Amazon 库存、Shopify 沿用上一版”。Amazon 广告仍为 budget_change=no_increase，标题与 why_now 明确“维持预算、核验通过前不追加”，未建议加预算。文本与元数据一致：amazon:inventory:1 为 inventory/not_applicable，Shopify 广告为 advertising/no_increase，均与正文相符。新增数字均可由供给数据透明推导且已标注假设：日均 70÷7=10、覆盖 12÷10≈1.2 天（amazon-sales、amazon-stock）；安全库存 1.65×1.3×√7≈5.7、再订货点≈75.7 明确写为“假设服务水平 0.95、z≈1.65、七天窗口可代表近期需求”的假设性参考，并声明不作为已确认采购量，未当作观测事实或无条件采购量。Shopify 80÷5≈16 天亦为观测值推算。limitations 中“四项来源均新鲜（0.0 天）”“Shopify 充分度 insufficient”与 system_context 审计一致，属系统元数据而非臆造店铺事实。未发现编造业务观测或算术错误。 |
| held-01 | baseline | revision_scope=False, budget_constraint=True | True | 要求已满足：Amazon 库存条目已重写（amazon:inventory:1 改为 external_change，标题为“可售 12 件约覆盖 1.2 天，短于 7 天 lead time，需立即补货并先核验在途与待入库”，引用 amazon-stock/amazon-sales）；Amazon 广告明确 budget_change=no_increase，标题“预算维持不加”，未实质改动；Shopify 侧仅保留“保持不变”基线复核，未新增改动建议。文本与元数据一致：库存条目 category=inventory、budget_change=not_applicable，广告条目 no_increase 与正文“先不增加预算”相符。无新增无依据事实：1.2 天（12÷10）、日均 10 件（70÷7）、ROAS 2.0（200÷100）、Shopify 16 天（80÷5）均为对已给数字的透明推算，并明确标注“推算/显式假设”；补货件数、到货时点均列为数据缺口，未编造；跨平台对比阻断、adequacy unknown 与系统审计一致。小瑕疵：Shopify 条目 advice_id 由 shopify:inventory:1 变为 shopify:other:1，但正文仍为“保持不变”，未构成实质改动。 |
| held-01 | stage_one | revision_scope=True, budget_constraint=True | True | 反馈要求仅重写 Amazon 库存、Amazon 广告不加预算、Shopify 不动。修订版 revision_changes 标注 amazon:inventory:1 为 updated，其余四条 preserved；Amazon 库存条目重写为核验在途/采购记录、不给确定采购数量，符合要求；Amazon 广告仍为 no_increase，Shopify 两条建议文本与原文一致，未被实质改动。文本与元数据一致：Amazon 库存 category=inventory、budget_change=not_applicable，正文为库存覆盖与补货核验；广告条目 budget_change=no_increase 与正文“维持预算/不追加”一致。无新增无据事实：日均 10 件/天（70÷7）、覆盖 1.2 天（12÷10）、ACOS 50%（100/200）、单次订单成本 10 USD（100/10）均为对 amazon-sales/amazon-ads 数值的透明推导；service_level 未匹配来源、缺在途与采购记录等均作为数据缺口/限制披露，未编造采购数量或店铺观测。唯一小瑕疵是 Shopify sufficiency 由 partial 改为 insufficient 且 execution_gate 同步，属系统元数据层面调整，未改变 Shopify 建议实质内容。 |
| held-02 | baseline | revision_scope=False, budget_constraint=True | True | 修订版满足反馈要求：Amazon 库存建议已重做（可售12件÷日均10件≈1.2天，短于7天交期，改为先核验在途/补货记录再定量，未给确定采购量，引用amazon-stock、amazon-sales，算术正确且假设已标注）；Amazon 广告明确 budget_change=no_increase 且正文“不增加预算”，未加钱；Shopify 广告与库存两条建议实质保留（仍为不增预算、核验在途与安全库存）。文本与 category/budget_change 一致，如 Amazon 库存 not_applicable、广告 no_increase 均与正文相符。未引入新的业务观测事实：日均、覆盖天数、交期需求35件等均由给定数字推导并标注为推导/假设，未虚构关键词、竞品或采购数量；listing 项仅标注待补证据。仅一处小瑕疵：Shopify 库存标题称“80件可覆盖7天交期需求”，正文已说明35件为假设推算，属透明推导，不构成无依据事实。 |
| held-02 | stage_one | revision_scope=True, budget_constraint=True | True | 修订仅重做 Amazon 库存建议（amazon:inventory:1 标为 updated），其余四条（含 Amazon 广告 no_increase、Shopify 广告/库存）均标 preserved 且正文实质未变，符合“只改 Amazon 库存、Amazon 广告不加钱、Shopify 维持原建议”。Amazon 库存新增的日均 10 件/天（70÷7）、覆盖 1.2 天（12÷10）、交期需求约 70 件、缺口约 58 件均为 amazon-sales 与 amazon-stock 数字的透明推导，并在 limitations 明确标注为“假设性推算而非观测事实”，补货量仅给下限（约 58 件起，叠加安全库存）且要求核验在途/采购记录，未把假设当作观测事实或无条件采购量。文本与 category=inventory、budget_change=not_applicable 一致，广告条目 budget_change=no_increase 与“不增预算”表述一致。未引入新的业务观测事实，缺口与限制（在途、安全库存、归因口径）均如实披露。 |
| held-03 | baseline | revision_scope=False, budget_constraint=True | True | 修订版按反馈收窄范围：Shopify 广告与库存结论基本保持原样（shopify:advertising:1 仍为 no_increase，shopify:inventory:1 仍为 not_applicable 且不给确定采购量），Amazon 广告维持 no_increase 并明确“按用户指示本轮不改广告建议”，未建议增加预算；仅 Amazon 库存建议被实质修订为 external_change 并加入“先核验在途/待入库/未结采购单、按可售天数≤提前期+缓冲触发补货、不给具体采购数量”，符合“只改库存建议”的要求。文本与元数据一致：amazon:inventory:1 的 category=inventory、budget_change=not_applicable、action_type=external_change 与正文“启动补货流程、需人工审批”相符；广告条目 no_increase 与正文“不增加预算”一致。算术可核验：70÷7≈10 件/天、12÷10≈1.2 天、80÷5≈16 天、150÷50≈3.0 均由 amazon-sales/amazon-stock/shopify-product 直接推导，且明确标注为“派生/假设性结论”，缓冲天数标注需业务确认，未把假设当作观测事实，也未给出无条件采购数量。未发现新增无依据的业务事实；新鲜度、审计 adequacy=unknown、cross-platform blocked 等均来自 system_context，属系统元数据而非臆造。 |
| held-03 | stage_one | revision_scope=True, budget_constraint=True | True | 反馈要求：Shopify 结论保留、Amazon 广告维持预算、仅改 Amazon 库存建议。修订版 revision_changes 明确 amazon:inventory:1 为 updated，其余四条 preserved；Amazon 广告仍为 budget_change=no_increase、标题“维持预算并先核验效果”，未实质改动；Shopify 广告/库存文字与原版一致，符合要求。文本与元数据一致：Amazon 库存 category=inventory、budget_change=not_applicable，正文只谈补货核验，未提预算；广告条目 budget_change=no_increase 与“不追加预算”表述吻合。算术正确且透明：70÷7=10 件/天、12÷10=1.2 天、ACOS=100/200=50%、单次订单成本=100/10=10 USD、10/70≈14.3%、Shopify 80÷5≈16 天，均为对已给数字的推导并标注“推导值”。未新增无依据的业务事实：补货量仅作条件化表述（“覆盖提前期需求+安全库存−现有可售−在途”），并明确安全库存/服务水平为假设参数需业务确认，未给出确定采购件数或下单日期；在途、IPI、库龄等缺失项均列入 limitations 而非编造。证据审计 adequacy=unknown、cross-platform blocked、requirement_version=2 等均与 system_context 一致，属系统元数据而非虚构店铺观察。 |
| held-04 | baseline | revision_scope=False, budget_constraint=True | True | 修订版满足反馈要求：Amazon 库存建议被重写为“可售约1.2天、远低于7天交期、先核验在途与补货记录再定量”，并明确不给具体采购数量（引用 amazon-stock、amazon-sales）；Amazon 广告建议 budget_change=no_increase，正文“维持现有预算、不增加”，未建议加预算；Shopify 广告与库存建议、Amazon listing 建议的实质内容与原文一致，仅措辞微调，未被实质改动。文本与元数据一致：库存类 budget_change=not_applicable、广告类 no_increase，与正文表述吻合；action_type 由 analysis 改为 external_change 属标签层面，正文仍强调“需人工审批、不下达具体数量”，不构成实质矛盾。未引入无依据的业务事实：日均10件、覆盖1.2天、80/5≈16天均为对给定数字的透明推算，并明确标注为假设/推算、需业务确认；安全库存仅作为“假设参数”出现在条件性建议中，未当作观测事实；新鲜度、comparability、缺失输入等均来自系统审计元数据，非臆造店铺观察。算术核对无误（70/7=10，12/10=1.2，35/7=5，80/5=16）。 |
| held-04 | stage_one | revision_scope=True, budget_constraint=True | True | 修订版仅实质改动 amazon:inventory:1（标题标注“本次唯一修改项”，新增补货触发条件与在途核验步骤），Amazon 广告仍为 budget_change=no_increase 且正文明确“核验通过前不追加预算”，Shopify 广告/库存与 listing 建议文字与原文一致，revision_changes 标注 preserved 与正文相符，满足“保留 Shopify 全部建议、仅改 Amazon 库存、Amazon 预算不增”。文本与元数据一致：inventory 类 budget_change=not_applicable、advertising 类 no_increase，正文表述吻合。数字均可由证据推导：12/10=1.2 天（amazon-stock、amazon-sales）、ACOS 100/200=50%、100/10=10 USD（amazon-ads）、10/70=14.3%、80÷5≈16 天（shopify-product），未虚构业务观测；service_level 0.95 明确标注为合同默认假设且未作为观测结论，未给出无条件采购数量，符合要求。 |

## 可解释范围

- 仅证明固定数据、同一业务情境下六种反馈的表现，不能外推为广泛路由准确率。
- 首轮广告建议已经是不增加预算，故预算项验证约束执行；不覆盖撤销一条原有加预算建议的冲突情况。
- 范围规则要求 A 库存的用户可见字段有变化，其他建议整个对象相同；措辞变化不代表质量提高，因此另列语义判定。
- 此任务已提供完整样例事实，要求不检索平台政策；不用于衡量 RAG 或工具循环质量。
- 初始开发中出现传输中断、规划平台编号及引用错误，已保留本地诊断；不计作正式六条反馈的质量样本。
- development-attempts 保留了契约修复前两条开发失败记录；正式保留测试在修复冻结后执行。
- 范围保持检查针对 priorities 中的建议对象；摘要、风险和限制会重新综合，由语义评分与系统审核检查。
- 执行失败计为端到端未通过，但不将其解释为已经生成报告的范围错误；请结合执行错误列与逐条原因阅读。
- 未进行局部重算、长会话或跨会话记忆优化。

- 生成与评分使用同一型号模型，可能共享偏差；LLM语义合格不等于独立人工真值。
- 开发评分补齐审计和 Skill 上下文后重评一次，旧评分保存在 semantic_judge_history；保留测试没有重复评分。
- 两组语义评分都可能通过，但基线仍改写非目标建议，因此“保持不变”以规则对象比较为准。

缺失记录：无
