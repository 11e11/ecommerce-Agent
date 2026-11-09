"""Execute the actual report renderer with malicious and multi-attempt data."""
import json
import subprocess
from pathlib import Path


def test_report_renderer_shows_findings_checks_and_escapes_content():
    root = Path(__file__).resolve().parents[1]
    script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("ecommerce_ai_skills/runtime/web/app.js", "utf8");
const ctx = {tr: value => value, escapeHtml: value => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf("function renderRunReport("), source.indexOf("function showRunDetail(")), ctx);
vm.runInContext(source.slice(source.indexOf("function latestReport("), source.indexOf("function liveCursorStorageKey(")), ctx);
const bundle = {
  run: {objective: "<script>alert(1)</script>", status: "completed", review_status: "approved"},
  tasks: [{id: "task", role: "platform_specialist", attempt_count: 2}],
  artifacts: [
    {task_id:"task", kind:"specialist_finding", attempt:1, content:{platform:"amazon", summary:"OBSOLETE"}},
    {task_id:"task", kind:"specialist_finding", attempt:2, content:{platform:"amazon", summary:"Platform summary", findings:[{title:"Finding", recommendation:"Recommendation", evidence_refs:["evidence-1"]}], data_gaps:["Missing data"]}},
    {kind:"weekly_ops_report", content:{executive_summary:"Report summary", priorities:[{rank:1,title:"Priority",why_now:"Reason",downstream_action:"Next action",evidence_refs:["evidence-1"]}],risks:[{risk:"Risk",mitigation:"Mitigation"}],limitations:["Limitation"]}}
  ],
  evaluations:[{details:{passed_count:1,checks:[{name:"evidence_references",passed:true},{name:"platform_isolation",passed:false,detail:"<img src=x onerror=alert(1)>"}]}}]
};
const runtimeBundle = {run:{provider:"openai",model:"configured-model"},tasks:[{agent_name:"platform_amazon",role:"platform_specialist",status:"completed",skill_ids:["listing"]}]};
const trace = {artifacts:[{agent:"platform_amazon",kind:"context_budget",content:{phase:"plan",injected_chars:1200,limit_chars:48000}},{agent:"platform_amazon",kind:"specialist_working_memory",content:{phase:"final",stop_reason:"evidence_gap",memory:{open_questions:["<script>missing</script>"],budget:{tool_calls_left:2}}}}],events:[{agent:"platform_amazon",type:"task.tool_call",payload:{tool:"opc.search_knowledge",duration_ms:12}},{agent:"platform_amazon",type:"task.loop.observe",payload:{tool:"opc.search_knowledge",succeeded:true}}]};
console.log(JSON.stringify({html:ctx.renderRunReport(bundle,true),empty:ctx.renderRunReport({run:{},tasks:[]}),runtime:ctx.renderRuntimeTrace(runtimeBundle,trace)}));
'''
    result = subprocess.run(["node", "-e", script], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)
    rendered = json.loads(result.stdout)
    for text in ("Report summary", "Priority", "Next action", "Platform summary", "Recommendation", "evidence-1", "Missing data", "1 / 2", "模拟模型"):
        assert text in rendered["html"]
    assert "OBSOLETE" not in rendered["html"]
    assert "<script>" not in rendered["html"]
    assert "<img src=x" not in rendered["html"]
    assert "&lt;script&gt;" in rendered["html"]
    assert "暂无分析报告" in rendered["empty"]
    for value in ("configured-model", "opc.search_knowledge", "1200 / 48000", "evidence_gap", "task.loop.observe"):
        assert value in rendered["runtime"]
    assert "<script>" not in rendered["runtime"]


def test_run_details_use_report_and_keep_raw_data_available():
    web = Path(__file__).resolve().parents[1] / "ecommerce_ai_skills/runtime/web"
    html = (web / "mission-control.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")
    assert 'id="detail-report"' in html and 'id="detail-raw"' in html
    assert '$("detail-raw").open = false' in script
    evaluation_action = script.split('case "evaluate-run":', 1)[1].split("break;", 1)[0]
    assert "loadRunDetail" in evaluation_action
    assert 'id="detail-runtime"' in html
    followup_action = script.split('case "follow-up-run":', 1)[1].split("case ", 1)[0]
    assert "/follow-ups" in followup_action and "/execute" in followup_action
