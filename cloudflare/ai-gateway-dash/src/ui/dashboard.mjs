/** Dashboard page: Signal Panel shell around the existing read APIs.

  Only presentation changes here. The Worker routes, session handling and API
  proxy remain in index.mjs. Fetching is isolated in the `ROUTES` adapter so a
  later switch to the V2 resources is a one-place change.
 */

import { LOCALES } from "./locales.mjs";
import { CSS } from "./styles.mjs";
import {
  escapeHtml,
  finiteNumber,
  clampPercent,
  quotaRowModel,
  healthReadouts,
  tokensFromQuota,
  safeHttpUrl,
  jsObjectLiteral,
} from "./format.mjs";
import {
  codexRemainingPercent,
  codexWindowLabel,
  codexWindowDisplay,
  codexRemainingClass,
  requiresHumanAudit,
} from "../metrics.mjs";
import { BOOT_SCRIPT } from "./shared.mjs";

const zh = LOCALES["zh-CN"];

function text(key) {
  return escapeHtml(zh[key] ?? key);
}

const HELPER_SOURCE = [
  codexRemainingPercent,
  codexWindowLabel,
  codexWindowDisplay,
  codexRemainingClass,
  requiresHumanAudit,
  finiteNumber,
  clampPercent,
  quotaRowModel,
  healthReadouts,
  tokensFromQuota,
  safeHttpUrl,
].map((helper) => helper.toString().replace(/^export\s+/, "")).join("\n");

const CLIENT = String.raw`
var API="/api";
/* Data adapter: the only place that names read resources. Switch to /v2/ai/*
   here once those resources are merged; the panels do not change. */
var ROUTES={
  health:"/v1/ai/health",
  orgHealth:"/v1/ai/org-health",
  quota:"/v1/ai/quota",
  changes:"/v1/ai/changes",
  effectiveness:"/v1/ai/changes/effectiveness",
  shadow:"/v1/ai/feedback/shadow",
  diagnoses:"/diagnoses"
};
var state={
  health:{status:"loading"},
  org:{status:"loading"},
  quota:{status:"loading"},
  changes:{status:"loading"},
  eff:{status:"loading"},
  shadow:{status:"loading"},
  diagnoses:{status:"loading"}
};
var root=document.documentElement;
var lang=root.lang==="en"?"en":"zh-CN";
var theme=root.getAttribute("data-theme")==="dark"?"dark":"light";
var t=I18N[lang]||I18N["zh-CN"];

function qs(id){return document.getElementById(id);}
function clear(node){if(!node)return;while(node.firstChild)node.removeChild(node.firstChild);}
function el(tag,cls,textValue){var node=document.createElement(tag);if(cls)node.className=cls;if(textValue!==undefined&&textValue!==null)node.textContent=String(textValue);return node;}
function append(parent){for(var i=1;i<arguments.length;i++){if(arguments[i])parent.appendChild(arguments[i]);}return parent;}
function locale(){return lang==="zh-CN"?"zh-CN":"en-US";}
function fmtNum(value){var n=finiteNumber(value);return n===null?"—":n.toLocaleString(locale());}
function fmtPct(value,digits){var n=finiteNumber(value);if(n===null)return "—";return n.toFixed(digits===undefined?0:digits)+"%";}
function fmtMs(value){var n=finiteNumber(value);return n===null?"—":Math.round(n).toLocaleString(locale())+" ms";}
function fmtMoney(value,currency){var n=finiteNumber(value);if(n===null)return "—";var code=String(currency||"usd").toUpperCase();return code==="USD"?"$"+n.toFixed(2):n.toFixed(2)+" "+code;}
function fmtTokens(value){var n=finiteNumber(value);if(n===null)return "—";if(n>=1000000)return (n/1000000).toFixed(1);if(n>=1000)return (n/1000).toFixed(1);return String(n);}
function tokenUnit(value){var n=finiteNumber(value);if(n===null)return "";if(n>=1000000)return "M";if(n>=1000)return "k";return "";}
function shortError(error){var message=error&&error.message?error.message:String(error||"");return message.split("\n").join(" ").slice(0,180);}
function toast(message){var node=qs("toast");if(!node)return;node.textContent=message||"";node.style.display=message?"block":"none";}

function statusClass(status){if(status==="healthy")return "st-ok";if(status==="degraded")return "st-warn";if(status==="unhealthy")return "st-bad";return "st-unk";}
function statusKey(status){if(status==="healthy")return "s_ok";if(status==="degraded")return "s_deg";if(status==="unhealthy")return "s_bad";return "s_unk";}
function statusText(status){if(status==="loading")return t.loading;if(status==="unavailable")return t.unavailable;return t[statusKey(status)]||t.s_unk;}
function actionText(action){var key="a_"+String(action||"");return t[key]||t.a_change;}
function riskText(risk){var key="r_"+String(risk||"");return t[key]||t.r_unknown;}
function effectText(effect){var key="e_"+String(effect||"");return t[key]||t.e_pending;}
function diagStatusText(status){var key="diag_status_"+String(status||"unknown");return t[key]||t.diag_status_unknown;}

function meter(id,filled,hot,unknown){var node=qs(id);if(!node)return;clear(node);node.className="meter"+(unknown?" na":"");var n=finiteNumber(filled);var cells=n===null?0:Math.max(0,Math.min(12,Math.round(n)));for(var i=0;i<12;i++){var cell=document.createElement("i");if(i<cells)cell.className=hot?"h":"f";node.appendChild(cell);}}
function setReadout(id,value,unit,foot,unknown){var node=qs("v-"+id);if(!node)return;clear(node);node.className="v"+(unknown?" unk":"");node.appendChild(document.createTextNode(value));if(unit)node.appendChild(el("small","",unit));var footNode=qs("f-"+id);if(footNode)footNode.textContent=foot||"";}
function loadingPanel(id){var target=qs(id);if(!target)return;clear(target);target.appendChild(el("div","loading",t.loading));}
function panelError(id,title){var target=qs(id);if(!target)return;clear(target);var box=el("div","panel-error");append(box,el("b","",title||t.error_title),el("span","",t.error_detail));target.appendChild(box);}
function emptyPanel(id,title,body){var target=qs(id);if(!target)return;clear(target);var box=el("div","empty");append(box,el("b","",title||t.unknown),el("span","",body||""));target.appendChild(box);}
function stat(label,value){var box=el("div","stat");append(box,el("span","lbl",label),el("b","",value));return box;}
function kvRow(label,value){var row=el("div","r");append(row,el("span","",label),el("span","",value));return row;}

function renderStatus(){
  var pulse=qs("pulse"),textNode=qs("status-text");
  if(!pulse||!textNode)return;
  var status="loading",cls="st-unk";
  if(state.health.status==="ok"){status=String((state.health.data&&state.health.data.status)||"unknown");cls=statusClass(status);if(status==="unknown")cls="st-warn";}
  else if(state.health.status==="error"){status="unavailable";cls="st-bad";}
  pulse.className="lamp "+cls;
  textNode.textContent=statusText(status);
}

function renderReadouts(){
  var health=state.health.status==="ok"?healthReadouts(state.health.data):null;
  if(health){
    setReadout("req",fmtNum(health.requests),"",t.k_req_f,false);
    meter("m-req",health.requests>0?12:0,false,false);
    if(health.p95===null){setReadout("p95","—","",t.k_p95_f,true);meter("m-p95",0,false,true);}
    else{setReadout("p95",fmtNum(health.p95),"ms",t.k_p95_f,false);meter("m-p95",Math.max(health.p95>0?1:0,Math.ceil(Math.min(1,health.p95/30000)*12)),health.p95>10000,false);}
    if(health.errorRate===null){setReadout("err","—","",t.no_requests,true);meter("m-err",0,false,true);}
    else{setReadout("err",health.errorRate.toFixed(2),"%",fmtNum(health.errors)+" "+t.err_failed+" / "+fmtNum(health.requests)+" "+t.err_total,false);meter("m-err",Math.max(health.errorRate>0?1:0,Math.ceil(Math.min(1,health.errorRate/5)*12)),health.errorRate>=5,false);}
  }else{
    var note=state.health.status==="error"?t.unavailable:state.health.status==="loading"?t.loading:t.no_data;
    setReadout("req","—","",note,true);meter("m-req",0,false,true);
    setReadout("p95","—","",t.k_p95_f,true);meter("m-p95",0,false,true);
    setReadout("err","—","",note,true);meter("m-err",0,false,true);
  }
  var tokens=state.quota.status==="ok"?tokensFromQuota(state.quota.data):null;
  if(tokens){
    setReadout("tok",fmtTokens(tokens.total),tokenUnit(tokens.total),t.tok_in+" "+fmtNum(tokens.input)+" · "+t.tok_out+" "+fmtNum(tokens.output),false);
    meter("m-tok",tokens.total>0?12:0,false,false);
  }else{
    var tokNote=state.quota.status==="error"?t.unavailable:state.quota.status==="loading"?t.loading:t.no_data;
    setReadout("tok","—","",tokNote,true);meter("m-tok",0,false,true);
  }
  setReadout("bal","—","",t.k_bal_f,true);meter("m-bal",0,false,true);
}

function profileText(endpoint){var profile=endpoint&&endpoint.latency_profile;if(profile==="background_job")return t.p_bg;if(profile)return String(profile);return "—";}
function endpointStatus(endpoint,reasons){
  var raw=String((endpoint&&endpoint.status)||"");
  if(["healthy","degraded","unhealthy","unknown"].indexOf(raw)>=0)return raw;
  // An explicit unavailable/stale state must not be inferred away from old metrics.
  if(raw)return "unknown";
  var pathName=String((endpoint&&endpoint.path)||"");
  for(var i=0;i<reasons.length;i++){if(reasons[i]&&String(reasons[i].path||"")===pathName)return "degraded";}
  // Request counts (even zero errors) do not establish current endpoint health.
  return "unknown";
}
function healthReasonText(reason){var pathName=String((reason&&reason.path)||"—");if(reason&&reason.reason==="error_rate")return pathName+" "+t.k_err+" "+fmtPct(reason.value,2)+" ≥ "+fmtPct(reason.threshold,2);if(reason&&reason.reason==="p95_latency_ms")return pathName+" P95 "+fmtMs(reason.value)+" ≥ "+fmtMs(reason.threshold);return pathName+" "+String((reason&&reason.reason)||t.or_attention);}
function renderHealthTable(){
  var target=qs("health");if(!target)return;
  if(state.health.status==="loading"){loadingPanel("health");return;}
  if(state.health.status==="error"){panelError("health",t.p_title+" · "+t.error_title);return;}
  var health=state.health.data||{};
  var endpoints=Array.isArray(health.endpoints)?health.endpoints:[];
  var reasons=Array.isArray(health.degradation_reasons)?health.degradation_reasons:[];
  clear(target);
  if(!endpoints.length){emptyPanel("health",t.p_none,t.no_data);return;}
  var total=0;var i;
  for(i=0;i<endpoints.length;i++){var totalValue=finiteNumber(endpoints[i]&&endpoints[i].total);total+=totalValue===null?0:totalValue;}
  var table=el("table","t-prov");
  var thead=el("thead","");var headRow=el("tr","");
  append(headRow,el("th","",t.p_col_ep),el("th","",t.p_col_status),el("th","n",t.p_col_share),el("th","n",t.p_col_p95),el("th","n",t.p_col_errors));
  thead.appendChild(headRow);table.appendChild(thead);
  var body=el("tbody","");
  for(i=0;i<endpoints.length;i++){
    var endpoint=endpoints[i]||{};
    var status=endpointStatus(endpoint,reasons);
    var row=el("tr","");
    var nameCell=el("td","");
    var prov=el("div","prov");
    var info=el("div","");
    append(info,el("b","",String(endpoint.path||"—")),document.createElement("br"),el("span","",profileText(endpoint)));
    append(prov,el("span","lamp "+statusClass(status)),info);
    nameCell.appendChild(prov);
    var statusCell=el("td","");
    statusCell.appendChild(el("span","tag "+statusClass(status),statusText(status)));
    var endpointTotal=finiteNumber(endpoint.total);
    var endpointErrors=finiteNumber(endpoint.errors);
    append(row,nameCell,statusCell,
      el("td","n",total>0&&endpointTotal!==null?fmtPct(endpointTotal/total*100,0):"—"),
      el("td","n",fmtMs(endpoint.p95_ms)),
      el("td","n",endpointTotal!==null&&endpointTotal>0&&endpointErrors!==null?fmtPct(endpointErrors/endpointTotal*100,2):"—"));
    body.appendChild(row);
  }
  table.appendChild(body);target.appendChild(table);
  if(reasons.length){var box=el("div","reasons");for(i=0;i<Math.min(reasons.length,5);i++)box.appendChild(kvRow(healthReasonText(reasons[i]),""));target.appendChild(box);}
  else if(health.last_error&&health.last_error.message){var errorBox=el("div","reasons");errorBox.appendChild(kvRow(t.error_title,String(health.last_error.message).slice(0,120)));target.appendChild(errorBox);}
}

function providerAccountText(account){
  var parts=[];var days=finiteNumber(account&&account.window_days);if(days!==null)parts.push(days+"d");
  var costs=(account&&account.costs)||{};var cost=finiteNumber(costs.total_cost);if(cost!==null)parts.push(fmtMoney(cost,costs.currency));
  var completions=(account&&account.completions)||{};var messages=(account&&account.messages)||{};
  var requests=finiteNumber(completions.num_model_requests);if(requests===null)requests=finiteNumber(messages.num_model_requests);
  if(requests!==null)parts.push(fmtNum(requests)+" "+t.requests_suffix);
  var usage=completions.total_tokens!==undefined?completions:messages;var tokens=finiteNumber(usage.total_tokens);if(tokens!==null)parts.push(fmtNum(tokens)+" "+t.tokens_suffix);
  return parts.length?parts.join(" · "):t.unknown;
}
function codexAccountText(account){
  var limits=(account&&account.rate_limits)||{};var parts=[];
  var primary=codexWindowDisplay(limits.primary||{},t.remaining);if(primary)parts.push(primary);
  var secondary=codexWindowDisplay(limits.secondary||{},t.remaining);if(secondary)parts.push(secondary);
  if(limits.plan_type)parts.push(String(limits.plan_type));
  var credits=limits.credits||{};if(credits.balance!==null&&credits.balance!==undefined)parts.push("credits "+credits.balance);
  return parts.length?parts.join(" · "):t.unknown;
}
function usageText(entry){
  var calls=finiteNumber(entry&&entry.calls);var cost=finiteNumber(entry&&entry.total_cost_usd);
  var input=finiteNumber(entry&&entry.tokens_input);var output=finiteNumber(entry&&entry.tokens_output);
  var parts=[];
  if(cost!==null)parts.push(fmtMoney(cost,"usd"));
  if(calls!==null)parts.push((entry&&entry.calls_incomplete?"≥ ":"")+fmtNum(calls)+" "+t.requests_suffix);
  if(input!==null||output!==null)parts.push(t.tok_in+" "+fmtNum(input||0)+" / "+t.tok_out+" "+fmtNum(output||0));
  return parts.length?parts.join(" · "):t.cost_na;
}
function renderQuota(){
  var target=qs("quota");if(!target)return;
  if(state.quota.status==="loading"){loadingPanel("quota");return;}
  if(state.quota.status==="error"){panelError("quota",t.q_title+" · "+t.error_title);return;}
  var quota=state.quota.data||{};var repos=quota.repos&&typeof quota.repos==="object"?quota.repos:{};
  clear(target);
  var list=el("div","quota");var names=Object.keys(repos);
  if(!names.length){
    list.appendChild(el("div","empty",t.q_none));
    var naRow=el("div","q");var naHead=el("div","row");
    append(naHead,el("b","",t.q_title),el("span","st-unk",t.not_configured));
    append(naRow,naHead,el("div","bar na"),el("small","",t.q_na));
    list.appendChild(naRow);
  }
  for(var i=0;i<names.length;i++){
    var entry=repos[names[i]];var model=quotaRowModel(entry);
    var row=el("div","q");var head=el("div","row");
    var label=el("b","",String(names[i]).split("/").pop()||names[i]);var status=el("span","");
    var bar=el("div","bar");
    if(model.status==="observed"){
      status.textContent=Math.round(model.percent)+"% · "+t.q_observed;
      var fill=el("i",model.percent>=80?"h":"");
      fill.style.width=clampPercent(model.percent)+"%";
      bar.appendChild(fill);
      if(model.percent>=80)row.appendChild(el("small","",t.q_near));
    }else if(model.status==="unknown"){
      status.className="st-unk";status.textContent=t.unknown;bar.className="bar na";
      row.appendChild(el("small","",t.q_unknown_note));
    }else{
      status.className="st-unk";status.textContent=t.not_configured;bar.className="bar na";
      row.appendChild(el("small","",t.q_na));
    }
    append(head,label,status);append(row,head,bar);list.appendChild(row);
  }
  target.appendChild(list);
  var summary=quota.summary||{};
  var details=el("details","details");details.appendChild(el("summary","",t.acct_title));
  var body=el("div","body");var box=el("div","reasons");var hasDetail=false;
  var openai=summary.openai_account&&summary.openai_account.status==="available"?summary.openai_account:null;
  var anthropic=summary.anthropic_account&&summary.anthropic_account.status==="available"?summary.anthropic_account:null;
  var codex=summary.codex_account&&summary.codex_account.status==="available"?summary.codex_account:null;
  if(openai){box.appendChild(kvRow(t.provider_gpt,providerAccountText(openai)));box.appendChild(kvRow(t.provider_cost,fmtMoney(openai.organization_costs&&openai.organization_costs.total_cost,openai.organization_costs&&openai.organization_costs.currency)));hasDetail=true;}
  if(anthropic){box.appendChild(kvRow(t.provider_claude,providerAccountText(anthropic)));box.appendChild(kvRow(t.provider_cost,fmtMoney(anthropic.costs&&anthropic.costs.total_cost,anthropic.costs&&anthropic.costs.currency)));hasDetail=true;}
  if(codex){box.appendChild(kvRow(t.codex_account,codexAccountText(codex)));hasDetail=true;}
  if(summary.api_key){box.appendChild(kvRow(t.internal_api_key,usageText(summary.api_key)));hasDetail=true;}
  if(summary.codex){box.appendChild(kvRow(t.internal_codex,usageText(summary.codex)));hasDetail=true;}
  if(summary.combined&&usageText(summary.combined)!==t.cost_na){box.appendChild(kvRow(t.internal_combined,usageText(summary.combined)));hasDetail=true;}
  if(summary.legacy_unknown){box.appendChild(kvRow(t.internal_legacy,usageText(summary.legacy_unknown)));hasDetail=true;}
  if(!hasDetail)box.appendChild(el("div","empty",t.acct_none));
  body.appendChild(box);details.appendChild(body);target.appendChild(details);
}

function renderOrg(){
  var target=qs("org");if(!target)return;
  if(state.org.status==="loading"){loadingPanel("org");return;}
  if(state.org.status==="error"){panelError("org",t.org_title+" · "+t.error_title);return;}
  var health=state.org.data||{};var provider=health.provider||{};var summary=health.summary||{};
  var repos=Array.isArray(health.repositories)?health.repositories:[];
  clear(target);
  if(provider.status==="unavailable"){emptyPanel("org",t.org_unavailable_title,t.org_unavailable_body);return;}
  var total=finiteNumber(summary.total_repositories);
  var unhealthy=finiteNumber(summary.unhealthy_repositories);
  var unknown=finiteNumber(summary.unknown_repositories);
  var degraded=finiteNumber(summary.degraded_repositories);
  if(total===null||unhealthy===null||unknown===null||degraded===null||total<unhealthy+unknown+degraded){emptyPanel("org",t.org_unknown_value,t.org_healthy_of_total);return;}
  var grid=el("div","stat-grid");
  grid.appendChild(stat(t.org_healthy_of_total,fmtNum(total-unhealthy-unknown-degraded)+" / "+fmtNum(total)));
  grid.appendChild(stat(t.org_unhealthy,fmtNum(unhealthy)));
  grid.appendChild(stat(t.org_degraded,fmtNum(degraded)));
  grid.appendChild(stat(t.org_unknown,fmtNum(unknown)));
  grid.appendChild(stat(t.org_failed,fmtNum(summary.failed_workflow_runs)));
  grid.appendChild(stat(t.org_running,fmtNum(summary.in_progress_workflow_runs)));
  target.appendChild(grid);
  var problems=repos.filter(function(repo){return repo&&repo.status!=="healthy";}).slice(0,5);
  if(!problems.length){target.appendChild(el("div","empty",t.org_none));return;}
  var list=el("div","reasons");
  problems.forEach(function(repo){
    var latest=repo.latest_run||{};var problem=repo.problem_run||{};
    var label=problem.name||latest.name||orgReason((repo.reasons||[])[0]);
    var url=safeHttpUrl(problem.url||latest.url);
    var row=el("div","r");var value=el("span","");
    if(url){var link=el("a","",label);link.href=url;link.target="_blank";link.rel="noreferrer";link.style.textDecoration="underline";value.appendChild(link);}else value.textContent=label;
    append(row,el("span","",String(repo.repo||"").split("/").pop()||"—"),value);
    list.appendChild(row);
  });
  target.appendChild(list);
}
function orgReason(reason){if(reason==="latest_run_in_progress")return t.or_in_progress;if(reason==="no_workflow_runs")return t.or_no_runs;if(reason==="github_api_error")return t.or_api_error;return String(reason||t.or_attention).split("_").join(" ");}

function renderChanges(){
  var target=qs("changes");var summary=qs("autonomy-summary");if(!target||!summary)return;
  if(state.changes.status==="loading"){loadingPanel("autonomy-summary");loadingPanel("changes");return;}
  if(state.changes.status==="error"){panelError("autonomy-summary",t.aut_title+" · "+t.error_title);panelError("changes",t.chg_title+" · "+t.error_title);return;}
  var data=state.changes.data||{};var items=Array.isArray(data.changes)?data.changes:[];
  clear(summary);
  if(!items.length){summary.appendChild(el("div","empty",t.aut_none));}
  else{
    var grid=el("div","stat-grid");
    grid.appendChild(stat(t.aut_registered,fmtNum(items.length)));
    grid.appendChild(stat(t.aut_merge,fmtNum(items.filter(function(change){return change.action==="auto_merge";}).length)));
    grid.appendChild(stat(t.aut_pr,fmtNum(items.filter(function(change){return change.action==="auto_pr";}).length)));
    grid.appendChild(stat(t.aut_manual,fmtNum(items.filter(requiresHumanAudit).length)));
    grid.appendChild(stat(t.aut_high,fmtNum(items.filter(function(change){return change.risk==="critical"||change.risk==="high";}).length)));
    grid.appendChild(stat(t.aut_pending,fmtNum(items.filter(function(change){return (change.effect||"pending")==="pending";}).length)));
    summary.appendChild(grid);
    summary.appendChild(el("div","mini",t.aut_fail_unknown));
  }
  clear(target);
  if(!items.length){target.appendChild(el("div","empty",t.chg_none));return;}
  var table=el("table","");var thead=el("thead","");var headRow=el("tr","");
  append(headRow,el("th","",t.chg_repo),el("th","",t.chg_action),el("th","",t.chg_risk),el("th","",t.chg_effect),el("th","n",t.chg_conf),el("th","n",t.chg_time));
  thead.appendChild(headRow);table.appendChild(thead);
  var body=el("tbody","");
  items.slice(0,15).forEach(function(change){
    var row=el("tr","");
    var repoCell=el("td","mono");
    var repoName=String(change.repo||"").split("/").pop()||"—";
    var url=safeHttpUrl(change.external_url);
    if(url){var link=el("a","",repoName);link.href=url;link.target="_blank";link.rel="noreferrer";link.style.textDecoration="underline";repoCell.appendChild(link);}else repoCell.textContent=repoName;
    var actionCell=el("td","");actionCell.appendChild(el("span","tag "+(change.action==="auto_merge"?"st-ok":change.action==="auto_pr"?"st-warn":"st-unk"),actionText(change.action)));
    var riskCell=el("td","");riskCell.appendChild(el("span","tag "+(change.risk==="critical"||change.risk==="high"?"st-bad":change.risk==="medium"?"st-warn":change.risk==="low"?"st-ok":"st-unk"),riskText(change.risk)));
    var effectCell=el("td","");effectCell.appendChild(el("span","tag "+(change.effect==="improved"?"st-ok":change.effect==="degraded"?"st-bad":"st-unk"),effectText(change.effect)));
    var confidence=finiteNumber(change.confidence);
    append(row,repoCell,actionCell,riskCell,effectCell,el("td","n",confidence===null?"—":fmtPct(confidence*100,0)),el("td","n",change.created_at?new Date(Number(change.created_at)*1000).toLocaleDateString(locale()):"—"));
    body.appendChild(row);
  });
  table.appendChild(body);target.appendChild(table);
}

function renderEffectiveness(){
  var target=qs("effectiveness");if(!target)return;
  if(state.eff.status==="loading"){loadingPanel("effectiveness");return;}
  if(state.eff.status==="error"){panelError("effectiveness",t.eff_title+" · "+t.error_title);return;}
  var data=state.eff.data||{};
  var evaluated=finiteNumber(data.evaluated);var pending=finiteNumber(data.pending);
  clear(target);
  if(evaluated===null||pending===null){emptyPanel("effectiveness",t.unknown,t.no_data);return;}
  var total=finiteNumber(data.total_changes);if(total===null)total=evaluated+pending;
  if(total===0){emptyPanel("effectiveness",t.eff_none,"");return;}
  var grid=el("div","stat-grid");
  if(evaluated===0){
    grid.appendChild(stat(t.eff_registered,fmtNum(total)));
    grid.appendChild(stat(t.eff_pending,fmtNum(pending)));
    grid.appendChild(stat(t.eff_rate,t.eff_after));
    target.appendChild(grid);return;
  }
  var rate=finiteNumber(data.improvement_rate);
  grid.appendChild(stat(t.eff_rate,rate===null?"—":fmtPct(rate*100,0)));
  grid.appendChild(stat(t.eff_evaluated,fmtNum(evaluated)));
  grid.appendChild(stat(t.eff_improved,fmtNum(data.improved)));
  grid.appendChild(stat(t.eff_degraded,fmtNum(data.degraded)));
  grid.appendChild(stat(t.eff_neutral,fmtNum(data.neutral)));
  grid.appendChild(stat(t.eff_pending,fmtNum(pending)));
  target.appendChild(grid);
}

function attentionRow(kind,item){
  var row=el("div","att-row");
  var badge=el("span","tag "+(kind==="human"?"st-warn":"st-unk"),kind==="human"?t.att_human:t.att_shadow);
  var body=el("div","att-body");
  var title=kind==="human"?actionText(item.action):String(item.plugin||item.repo||t.att_shadow);
  var repo=String(item.repo||item.source_repo||t.unknown);
  var createdAt=finiteNumber(item.created_at);
  var time=createdAt===null?t.unknown:new Date(createdAt*1000).toLocaleString(locale());
  append(body,el("div","att-title",title),el("div","att-meta",repo+" · "+time));
  var detail=el("div","att-detail");
  detail.hidden=true;
  detail.textContent=kind==="human"
    ?t.att_risk+": "+riskText(item.risk)+" · "+t.att_effect+": "+effectText(item.effect)
    :t.att_ai+": "+String(item.ai_verdict||t.unknown)+" · "+t.att_expected+": "+String(item.expected_verdict||t.unknown)+" · "+t.att_count+": "+(item.disagreement_count===undefined||item.disagreement_count===null?t.unknown:String(item.disagreement_count));
  var url=safeHttpUrl(item.external_url);
  var view=el(url?"a":"button","att-view",t.att_view);
  if(url){view.href=url;view.target="_blank";view.rel="noreferrer";}
  else{view.type="button";view.setAttribute("aria-expanded","false");view.addEventListener("click",function(){detail.hidden=!detail.hidden;view.setAttribute("aria-expanded",detail.hidden?"false":"true");});}
  append(row,badge,body,view,detail);
  return row;
}
function renderAttention(){
  var target=qs("attention");if(!target)return;
  if(state.changes.status==="loading"||state.shadow.status==="loading"){loadingPanel("attention");return;}
  var human=state.changes.status==="ok"?((state.changes.data&&state.changes.data.changes)||[]).filter(requiresHumanAudit):[];
  var shadow=state.shadow.status==="ok"?((state.shadow.data&&state.shadow.data.disagreements)||[]):[];
  clear(target);
  var list=el("div","att-list");
  if(state.changes.status==="error"){var humanError=el("div","panel-error");append(humanError,el("b","",t.att_human+" · "+t.error_title),el("span","",t.error_detail));list.appendChild(humanError);}
  if(state.shadow.status==="error"){var shadowError=el("div","panel-error");append(shadowError,el("b","",t.att_shadow+" · "+t.error_title),el("span","",t.error_detail));list.appendChild(shadowError);}
  human.forEach(function(item){list.appendChild(attentionRow("human",item));});
  shadow.forEach(function(item){list.appendChild(attentionRow("shadow",item));});
  if(state.changes.status==="ok"&&state.shadow.status==="ok"&&!human.length&&!shadow.length){target.appendChild(el("div","empty",t.att_none));return;}
  target.appendChild(list);
}

function diagnosisItems(container,items,emptyText){
  if(!items.length){container.appendChild(el("div","empty",emptyText));return;}
  items.slice(0,20).forEach(function(item){
    var box=el("div","diag-item");
    var line=el("div","diag-line");
    var meta=el("div","");
    var updatedAt=finiteNumber(item.updated_at);
    append(meta,el("div","",diagStatusText(item.status)),el("div","diag-meta",updatedAt===null?t.diag_time_unknown:new Date(updatedAt*1000).toLocaleString(locale())));
    var output=el("pre","diagnosis-output",t.diag_advisory);
    output.hidden=true;
    var button=el("button","diag-button",t.diag_view);
    button.type="button";
    button.addEventListener("click",function(){
      output.hidden=false;output.textContent=t.diag_output_loading;
      fetchJSON(ROUTES.diagnoses+"/"+encodeURIComponent(item.job_id)).then(function(detail){
        var diagnosis=detail&&detail.diagnosis?detail.diagnosis:{};
        output.textContent=String(diagnosis.output||diagnosis.message||t.diag_output_unavailable);
      }).catch(function(){output.textContent=t.diag_output_unavailable;});
    });
    append(line,meta,button);
    append(box,line,output);
    container.appendChild(box);
  });
}
function renderDiagnoses(){
  var target=qs("diagnoses");if(!target)return;
  if(state.diagnoses.status==="loading"){loadingPanel("diagnoses");return;}
  if(state.diagnoses.status==="error"){panelError("diagnoses",t.diag_title+" · "+t.error_title);return;}
  var data=state.diagnoses.data||{};var items=Array.isArray(data.diagnoses)?data.diagnoses:[];
  var operational=items.filter(function(item){return item.kind==="operational";});
  var rehearsal=items.filter(function(item){return item.kind==="historical_rehearsal";});
  clear(target);
  var opSection=el("section","");opSection.appendChild(el("h4","",t.diag_operational));
  diagnosisItems(opSection,operational,t.diag_none_op);
  var rehSection=el("section","");rehSection.appendChild(el("h4","",t.diag_rehearsal));
  diagnosisItems(rehSection,rehearsal,t.diag_none_reh);
  append(target,opSection,rehSection);
}

function renderAll(){
  renderStatus();
  renderReadouts();
  renderQuota();
  renderHealthTable();
  renderOrg();
  renderChanges();
  renderEffectiveness();
  renderAttention();
  renderDiagnoses();
  var last=qs("last-refresh");
  if(last)last.textContent=new Date().toLocaleTimeString(locale());
}

function fetchJSON(path){
  return fetch(API+path).then(function(response){
    return response.text().then(function(raw){
      var data=null;
      try{data=raw?JSON.parse(raw):null;}catch(error){data=null;}
      if(!response.ok||(data&&data.status==="error")){
        if(path===ROUTES.orgHealth&&response.status===503&&data&&data.status==="unavailable")return data;
        var detail=data&&data.error?": "+data.error:"";
        throw new Error(path+": "+response.status+detail);
      }
      if(!data)throw new Error(path+": invalid JSON");
      return data;
    });
  });
}
function normalize(key,data){
  if(key==="quota")return data&&data.quota&&typeof data.quota==="object"?data.quota:data;
  if(key==="eff")return data&&data.report&&typeof data.report==="object"?data.report:data;
  return data;
}
function load(key,path){
  return fetchJSON(path).then(function(data){
    state[key]={status:"ok",data:normalize(key,data)};
  }).catch(function(error){
    state[key]={status:"error",error:shortError(error)};
  }).then(function(){renderAll();});
}
function loadAll(){
  var jobs=[
    load("health",ROUTES.health),
    load("org",ROUTES.orgHealth),
    load("quota",ROUTES.quota),
    load("changes",ROUTES.changes+"?days=7"),
    load("eff",ROUTES.effectiveness+"?days=90"),
    load("shadow",ROUTES.shadow),
    load("diagnoses",ROUTES.diagnoses)
  ];
  return Promise.all(jobs).then(function(){
    var failed=false;
    Object.keys(state).forEach(function(key){if(state[key].status==="error")failed=true;});
    toast(failed?t.toast_error:"");
  });
}

function safeAvatarUrl(value){return safeHttpUrl(value);}
function loadUser(){
  fetch(API+"/user").then(function(response){
    if(!response.ok)throw new Error("user_unavailable");
    return response.json();
  }).then(function(user){
    var name=qs("username");if(name)name.textContent=String(user.login||"");
    var avatar=safeAvatarUrl(user.avatar_url);var image=qs("avatar");
    if(!image)return;
    if(avatar){image.src=avatar;image.style.display="block";}
    else{image.removeAttribute("src");image.style.display="none";}
  }).catch(function(){});
}

function applyI18n(){
  t=I18N[lang]||I18N["zh-CN"];
  root.lang=lang;
  root.setAttribute("data-theme",theme);
  document.querySelectorAll("[data-i18n]").forEach(function(node){var key=node.getAttribute("data-i18n");if(t[key]!==undefined)node.textContent=t[key];});
  document.querySelectorAll("[data-i18n-aria]").forEach(function(node){var key=node.getAttribute("data-i18n-aria");if(t[key]!==undefined)node.setAttribute("aria-label",t[key]);});
  document.querySelectorAll(".js-lang").forEach(function(button){button.textContent=lang==="zh-CN"?"EN":"中";button.setAttribute("aria-label",t.lang_label);});
  document.querySelectorAll(".js-theme").forEach(function(button){button.setAttribute("aria-label",t.theme_label);});
  document.title=t.d_title;
  renderAll();
}

function initWindows(){
  var buttons=document.querySelectorAll("[data-window]");
  buttons.forEach(function(button){
    button.addEventListener("click",function(){
      var win=button.getAttribute("data-window");
      buttons.forEach(function(other){other.setAttribute("aria-pressed",other===button?"true":"false");});
      var targetId=win==="live"?"signal":win==="90d"?"effectiveness-panel":"changes-panel";
      var node=qs(targetId);
      if(node&&node.scrollIntoView)node.scrollIntoView({behavior:"smooth",block:"start"});
    });
  });
}

document.querySelectorAll(".js-lang").forEach(function(button){button.addEventListener("click",function(){lang=lang==="zh-CN"?"en":"zh-CN";try{localStorage.setItem("qg-lang",lang);}catch(error){}applyI18n();});});
document.querySelectorAll(".js-theme").forEach(function(button){button.addEventListener("click",function(){theme=theme==="dark"?"light":"dark";try{localStorage.setItem("qg-theme",theme);}catch(error){}applyI18n();});});
initWindows();
applyI18n();
loadUser();
loadAll();
setInterval(function(){loadAll();},30000);
`;

export function dashboardHTML() {
  return String.raw`<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${text("d_title")}</title>
<script>${BOOT_SCRIPT}</script>
<style>${CSS}</style>
</head>
<body data-view="dashboard">
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div class="app">
  <nav class="rail" aria-label="${text("d_title")}">
    <div class="mark" aria-hidden="true">QG</div>
    <a class="on" href="#signal"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><span data-i18n="nav_signal">${text("nav_signal")}</span></a>
    <a href="#quota-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg><span data-i18n="nav_quota">${text("nav_quota")}</span></a>
    <a href="#changes-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h6"/><circle cx="18" cy="16" r="3"/></svg><span data-i18n="nav_changes">${text("nav_changes")}</span></a>
    <a href="#diagnoses-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5zM9 8h6M9 12h6M9 16h3"/></svg><span data-i18n="nav_diagnoses">${text("nav_diagnoses")}</span></a>
    <span class="sp"></span>
    <a href="#account-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/></svg><span data-i18n="nav_account">${text("nav_account")}</span></a>
  </nav>
  <main>
    <header class="top">
      <h1><span class="mark m-only" aria-hidden="true">QG</span><span><span data-i18n="d_title">${text("d_title")}</span><small class="lbl" data-i18n="d_sub">${text("d_sub")}</small></span></h1>
      <span class="grow"></span>
      <span class="chip hide-m"><span class="lamp st-unk" aria-hidden="true"></span><span data-i18n="d_chip">${text("d_chip")}</span></span>
      <span class="chip hide-m" id="status-pill"><span class="lamp st-unk" id="pulse" aria-hidden="true"></span><span id="status-text" data-i18n="loading">${text("loading")}</span></span>
      <span class="chip hide-m"><span data-i18n="updated">${text("updated")}</span> <span id="last-refresh">—</span></span>
      <div class="seg" role="group" data-i18n-aria="win_label" aria-label="${text("win_label")}">
        <button type="button" data-window="live" aria-pressed="true" data-i18n="win_live">${text("win_live")}</button>
        <button type="button" data-window="7d" aria-pressed="false" data-i18n="win_7d">${text("win_7d")}</button>
        <button type="button" data-window="90d" aria-pressed="false" data-i18n="win_90d">${text("win_90d")}</button>
      </div>
      <button class="iconbtn js-lang" type="button" data-i18n-aria="lang_label" aria-label="${text("lang_label")}">EN</button>
      <button class="iconbtn js-theme" type="button" data-i18n-aria="theme_label" aria-label="${text("theme_label")}">◐</button>
      <div class="user"><img id="avatar" alt=""><span id="username" aria-live="polite"></span><a href="/logout" data-i18n="acct_logout">${text("acct_logout")}</a></div>
    </header>

    <section class="pn readouts" id="signal" aria-label="${text("win_live")}">
      <div class="ro"><span class="lbl" data-i18n="k_req">${text("k_req")}</span><div class="v" id="v-req">—</div><div class="meter" id="m-req" aria-hidden="true"></div><div class="foot" id="f-req"></div></div>
      <div class="ro"><span class="lbl" data-i18n="k_p95">${text("k_p95")}</span><div class="v" id="v-p95">—</div><div class="meter" id="m-p95" aria-hidden="true"></div><div class="foot" id="f-p95"></div></div>
      <div class="ro"><span class="lbl" data-i18n="k_err">${text("k_err")}</span><div class="v" id="v-err">—</div><div class="meter" id="m-err" aria-hidden="true"></div><div class="foot" id="f-err"></div></div>
      <div class="ro"><span class="lbl" data-i18n="k_tok">${text("k_tok")}</span><div class="v" id="v-tok">—</div><div class="meter" id="m-tok" aria-hidden="true"></div><div class="foot" id="f-tok"></div></div>
      <div class="ro"><span class="lbl" data-i18n="k_bal">${text("k_bal")}</span><div class="v unk" id="v-bal">—</div><div class="meter" id="m-bal" aria-hidden="true"></div><div class="foot" id="f-bal"></div></div>
    </section>

    <div class="grid2">
      <section class="pn" aria-labelledby="traffic-h">
        <div class="pn-h"><h3 id="traffic-h" data-i18n="c_title">${text("c_title")}</h3><span class="lbl" data-i18n="c_unit">${text("c_unit")}</span></div>
        <div class="na"><div class="na-bar" aria-hidden="true"></div><div class="na-body"><div class="na-big" aria-hidden="true">—</div><b data-i18n="c_na_title">${text("c_na_title")}</b><p data-i18n="c_na_body">${text("c_na_body")}</p></div></div>
      </section>
      <section class="pn" id="quota-panel" aria-labelledby="quota-h">
        <div class="pn-h"><h3 id="quota-h" data-i18n="q_title">${text("q_title")}</h3><span class="lbl" data-i18n="q_unit">${text("q_unit")}</span></div>
        <div id="quota"></div>
      </section>
    </div>

    <div class="grid2">
      <section class="pn" id="health-panel" aria-labelledby="health-h">
        <div class="pn-h"><h3 id="health-h" data-i18n="p_title">${text("p_title")}</h3><span class="lbl">/v1/ai/health</span></div>
        <div class="table-wrap" id="health"></div>
      </section>
      <section class="pn" aria-labelledby="tail-h">
        <div class="pn-h"><h3 id="tail-h" data-i18n="t_title">${text("t_title")}</h3><span class="lbl">live</span></div>
        <div class="na"><div class="na-bar" aria-hidden="true"></div><div class="na-body"><div class="na-big" aria-hidden="true">—</div><b data-i18n="t_na_title">${text("t_na_title")}</b><p data-i18n="t_na_body">${text("t_na_body")}</p></div></div>
      </section>
    </div>

    <section class="pn panel-block" id="org-panel" aria-labelledby="org-h">
      <div class="pn-h"><h3 id="org-h" data-i18n="org_title">${text("org_title")}</h3><span class="lbl">/v1/ai/org-health</span></div>
      <div id="org"></div>
    </section>

    <section class="pn panel-block" id="attention-panel" aria-labelledby="att-h">
      <div class="pn-h"><h3 id="att-h" data-i18n="att_title">${text("att_title")}</h3><span class="lbl" data-i18n="att_sub">${text("att_sub")}</span></div>
      <div id="attention"></div>
    </section>

    <section class="pn panel-block" id="changes-panel" aria-labelledby="changes-h">
      <div class="pn-h"><h3 id="changes-h" data-i18n="chg_title">${text("chg_title")}</h3><span class="lbl" data-i18n="aut_title">${text("aut_title")}</span></div>
      <div id="autonomy-summary"></div>
      <div class="table-wrap changes-wrap" id="changes"></div>
    </section>

    <section class="pn panel-block" id="effectiveness-panel" aria-labelledby="eff-h">
      <div class="pn-h"><h3 id="eff-h" data-i18n="eff_title">${text("eff_title")}</h3><span class="lbl" data-i18n="eff_window">${text("eff_window")}</span></div>
      <div id="effectiveness"></div>
    </section>

    <section class="pn panel-block" id="diagnoses-panel" aria-labelledby="diag-h">
      <div class="pn-h"><h3 id="diag-h" data-i18n="diag_title">${text("diag_title")}</h3><span class="lbl">/api/diagnoses</span></div>
      <div class="diag" id="diagnoses"></div>
    </section>

    <section class="pn panel-block" id="account-panel" aria-labelledby="account-h">
      <div class="pn-h"><h3 id="account-h" data-i18n="acct_panel_title">${text("acct_panel_title")}</h3><span class="lbl">QuantStrategyLab</span></div>
      <div class="stat-grid">
        <div class="stat"><span class="lbl" data-i18n="acct_scope_title">${text("acct_scope_title")}</span><b data-i18n="acct_scope_body">${text("acct_scope_body")}</b></div>
        <div class="stat"><span class="lbl" data-i18n="acct_signed_in">${text("acct_signed_in")}</span><b><a href="/logout" data-i18n="acct_logout">${text("acct_logout")}</a></b></div>
      </div>
    </section>

    <footer class="footer"><span data-i18n="footer_refresh">${text("footer_refresh")}</span><span data-i18n="footer_readonly">${text("footer_readonly")}</span></footer>
  </main>

  <nav class="mobnav" aria-label="${text("d_title")}">
    <a class="on" href="#signal"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><span data-i18n="nav_signal">${text("nav_signal")}</span></a>
    <a href="#quota-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg><span data-i18n="nav_quota">${text("nav_quota")}</span></a>
    <a href="#changes-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h10M4 18h6"/><circle cx="18" cy="16" r="3"/></svg><span data-i18n="nav_changes">${text("nav_changes")}</span></a>
    <a href="#diagnoses-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5zM9 8h6M9 12h6M9 16h3"/></svg><span data-i18n="nav_diagnoses">${text("nav_diagnoses")}</span></a>
  </nav>
</div>
<script>
var I18N=${jsObjectLiteral(LOCALES)};
${HELPER_SOURCE}
${CLIENT}
</script>
</body>
</html>`;
}
