/** Dashboard page: the upstream status panel.

  Presentation only. Routes, session handling and the proxy stay in index.mjs;
  `upstream.mjs` owns the probe and its verdicts.

  Scope: this page used to render six panels over `/v1/ai/*` (traffic, quota,
  health, org health, changes, effectiveness) plus a diagnosis panel over
  `/v1/ai/automation/runs`. Those handlers were retired with the V1 surface
  (611c129), so every panel reported a failure cause and none could ever show a
  number. They are removed rather than re-pointed: the live service has no
  aggregate read model the dashboard may call, and a page that invents values is
  worse than a page that states what it cannot show.

  What remains is the one thing this Worker can honestly measure: what the origin
  answers to a fixed set of read-only probes, with the status code, the latency
  and the reason task counts are absent. The "not shown" panel names each removed
  panel and the retired route behind it, so the absence is explained rather than
  hidden.
 */

import { LOCALES } from "./locales.mjs";
import { CSS } from "./styles.mjs";
import { escapeHtml, jsObjectLiteral } from "./format.mjs";
import { BOOT_SCRIPT } from "./shared.mjs";

const zh = LOCALES["zh-CN"];

function text(key) {
  return escapeHtml(zh[key] ?? key);
}

const CLIENT = String.raw`
var API="/api";
/* Data adapter: the only place that names a read resource. Paths are relative to
   API, so the client prefix is applied exactly once. */
var ROUTES={status:"/upstream-status"};
var state={status:{status:"loading"}};
var root=document.documentElement;
var lang=root.lang==="en"?"en":"zh-CN";
var theme=root.getAttribute("data-theme")==="dark"?"dark":"light";
var t=I18N[lang]||I18N["zh-CN"];

function qs(id){return document.getElementById(id);}
function clear(node){if(!node)return;while(node.firstChild)node.removeChild(node.firstChild);}
function el(tag,cls,textValue){var node=document.createElement(tag);if(cls)node.className=cls;if(textValue!==undefined&&textValue!==null)node.textContent=String(textValue);return node;}
function append(parent){for(var i=1;i<arguments.length;i++){if(arguments[i])parent.appendChild(arguments[i]);}return parent;}
function locale(){return lang==="zh-CN"?"zh-CN":"en-US";}
function toNumber(value){var n=Number(value);return Number.isFinite(n)?n:null;}
function fmtMs(value){var n=toNumber(value);return n===null?"—":Math.round(n).toLocaleString(locale())+" ms";}
function shortError(error){var message=error&&error.message?error.message:String(error||"");return message.split("\n").join(" ").slice(0,180);}
function fmtTime(iso){
  if(!iso)return "—";
  var date=new Date(iso);
  if(isNaN(date.getTime()))return "—";
  try{return date.toLocaleTimeString(locale(),{hour12:false});}catch(error){return date.toISOString().slice(11,19);}
}
/* Global load-error notice. It stores episode state, never a rendered string,
   so it re-renders in the current locale. Dismissal lives in memory only. */
var toastModel=null;
function failedKeys(){return Object.keys(state).filter(function(key){return state[key].status==="error";});}
function toastEpisode(prev,event,failed){
  var p=prev||{active:false,dismissed:false,seen:[]};
  if(event==="dismiss")return {active:p.active,dismissed:p.active,seen:p.seen,visible:false,announce:false};
  var keys=Array.isArray(failed)?failed.slice():[];
  if(!keys.length)return {active:false,dismissed:false,seen:[],visible:false,announce:false};
  if(!p.active)return {active:true,dismissed:false,seen:keys,visible:true,announce:true};
  if(p.dismissed){
    var gained=keys.some(function(key){return p.seen.indexOf(key)<0;});
    if(gained)return {active:true,dismissed:false,seen:keys,visible:true,announce:true};
    return {active:true,dismissed:true,seen:p.seen,visible:false,announce:false};
  }
  return {active:true,dismissed:false,seen:keys,visible:true,announce:false};
}
/* The dashboard's own session ended: the Worker answers 401 "unauthorized"
   before probing. Origin authentication failures arrive as a probe verdict and
   stay on the generic notice. */
var LOGIN_PATH="/login";
var sessionEnded=false;
function isOwnSessionExpired(response,data){return response.status===401&&!!data&&data.error==="unauthorized";}
function toastMessage(){return toastModel&&toastModel.kind==="session"?t.toast_session:t.toast_error;}
function renderToast(){
  var node=qs("toast"),textNode=qs("toast-text");if(!node)return;
  var visible=!!(toastModel&&toastModel.visible);
  node.hidden=!visible;
  if(!textNode)return;
  if(!visible){if(textNode.textContent)textNode.textContent="";return;}
  if(toastModel.kind==="session"){
    var link=textNode.firstChild;
    if(!link||String(link.tagName).toUpperCase()!=="A"){
      textNode.textContent="";
      link=el("a","",t.toast_session);link.href=LOGIN_PATH;link.style.textDecoration="underline";textNode.appendChild(link);
    } else if(link.textContent!==t.toast_session){
      link.textContent=t.toast_session;
    }
  } else textNode.textContent=t.toast_error;
}
function announceToast(on){var live=qs("toast-live");if(live)live.textContent=on?toastMessage():"";}
function updateToast(){
  var prev=toastModel;
  var kind=sessionEnded?"session":"error";
  toastModel=toastEpisode(prev,"cycle",sessionEnded?["session"]:failedKeys());
  toastModel.kind=kind;
  if(toastModel.visible&&prev&&prev.visible&&prev.kind!==kind)toastModel.announce=true;
  renderToast();
  if(toastModel.announce)announceToast(true);
  else if(!toastModel.visible)announceToast(false);
}
function noteSessionEnded(){if(sessionEnded)return;sessionEnded=true;updateToast();}
function dismissToast(){
  var node=qs("toast");if(!node||node.hidden)return;
  var inside=node.contains(document.activeElement);
  var kind=toastModel&&toastModel.kind;
  toastModel=toastEpisode(toastModel,"dismiss");
  toastModel.kind=kind;
  renderToast();announceToast(false);
  if(inside){var heading=qs("main-title");if(heading)heading.focus();}
}
function initToast(){
  var node=qs("toast"),button=qs("toast-dismiss");
  if(button)button.addEventListener("click",dismissToast);
  if(node)node.addEventListener("keydown",function(event){if(event.key==="Escape"){event.preventDefault();dismissToast();}});
}

function panelError(id,title,detail,code){
  var target=qs(id);if(!target)return;clear(target);
  var box=el("div","panel-error");
  append(box,el("b","",title||t.error_title),el("span","",detail||t.error_detail));
  if(code){var line=el("span","cause");append(line,document.createTextNode(t.s_error_code+" · "),el("code","",String(code)));box.appendChild(line);}
  target.appendChild(box);
}
function emptyPanel(id,title,body){
  var target=qs(id);if(!target)return;clear(target);
  var box=el("div","empty");append(box,el("b","",title||t.unknown),el("span","",body||""));target.appendChild(box);
}

var OVERALL_CLASS={online:"st-ok",partial:"st-warn",unreachable:"st-bad",unknown:"st-unk"};
var VERDICT_CLASS={reachable:"st-ok",auth_required:"st-warn",not_found:"st-unk",skipped:"st-unk",redirect:"st-warn",rate_limited:"st-warn",server_error:"st-bad",unreachable:"st-bad",unexpected_status:"st-warn"};
function verdictLabel(verdict){return t["v_"+verdict]||t.v_unknown_status;}
function verdictMeaning(verdict){return t["m_"+verdict]||t.v_unknown_status;}
function verdictClass(verdict){return VERDICT_CLASS[verdict]||"st-unk";}
function authLabel(auth){return auth==="dashboard"?t.s_auth_dashboard:t.s_auth_anonymous;}

function renderStatus(){
  var pulse=qs("pulse"),textNode=qs("status-text");
  if(!pulse||!textNode)return;
  if(state.status.status==="ok"){
    var overall=String((state.status.data&&state.status.data.overall)||"unknown");
    pulse.className="lamp "+(OVERALL_CLASS[overall]||"st-unk");
    textNode.textContent=t["s_state_"+overall]||t.s_state_unknown;
    return;
  }
  // The dashboard's own expired session never reached the upstream: report the
  // session, not a probe failure, so the two causes stay separate.
  if(state.status.sessionExpired){pulse.className="lamp st-warn";textNode.textContent=t.s_state_session;return;}
  pulse.className="lamp "+(state.status.status==="error"?"st-bad":"st-unk");
  textNode.textContent=state.status.status==="error"?t.s_state_error:t.loading;
}

function probeRow(probe){
  var row=el("tr");
  append(row,el("td","mono",probe.path),el("td","",authLabel(probe.auth)));
  var resultCell=el("td","");
  var chip=el("span","chip");
  append(chip,el("span","lamp "+verdictClass(probe.verdict)),el("span","",verdictLabel(probe.verdict)));
  append(resultCell,chip,el("div","probe-note",verdictMeaning(probe.verdict)));
  append(row,resultCell,
    el("td","mono",probe.http_status===null||probe.http_status===undefined?"—":String(probe.http_status)),
    el("td","mono",fmtMs(probe.latency_ms)));
  return row;
}

function renderProbes(){
  var target=qs("probe-table");if(!target)return;
  if(state.status.status==="loading"){clear(target);target.appendChild(el("div","loading",t.loading));return;}
  if(state.status.status==="error"&&state.status.sessionExpired){
    panelError("probe-table",t.s_session_title,t.s_session_hint,"");
    return;
  }
  if(state.status.status==="error"){
    panelError("probe-table",t.s_failed,t.s_failed_hint,state.status.code);
    return;
  }
  var probes=state.status.data&&state.status.data.probes;
  if(!Array.isArray(probes)||!probes.length){emptyPanel("probe-table",t.unknown,t.s_empty);return;}
  clear(target);
  var table=el("table"),head=el("thead"),headRow=el("tr");
  [t.s_col_path,t.s_col_auth,t.s_col_result,t.s_col_code,t.s_col_latency].forEach(function(label){headRow.appendChild(el("th","",label));});
  head.appendChild(headRow);
  var body=el("tbody");
  probes.forEach(function(probe){body.appendChild(probeRow(probe));});
  append(table,head,body);
  target.appendChild(table);
}

function renderReadModel(){
  var target=qs("read-model");if(!target)return;
  clear(target);
  if(state.status.status!=="ok")return;
  var model=(state.status.data&&state.status.data.read_model)||{};
  var known=["read_credentials_rejected","dashboard_token_rejected","dashboard_token_missing","stats_route_absent","origin_unreachable","not_probed","unexpected_status","origin_unavailable"];
  var key=model.task_counts?"rm_ok":(known.indexOf(String(model.reason_code))>=0?"rm_"+model.reason_code:"rm_unknown");
  target.appendChild(el("p","note",t[key]||t.rm_unknown));
}

function renderAll(){
  renderStatus();
  renderProbes();
  renderReadModel();
  var last=qs("last-refresh");
  if(last)last.textContent=state.status.status==="ok"?fmtTime(state.status.data&&state.status.data.checked_at):"—";
  var source=qs("source-label");
  if(source)source.textContent=state.status.status==="ok"&&state.status.data&&state.status.data.cached?t.s_cached:t.s_fresh;
  var checked=qs("checked-at");
  if(checked)checked.textContent=state.status.status==="ok"?fmtTime(state.status.data&&state.status.data.checked_at):"—";
}

/* One read. A failure keeps its machine code so the panel can name the cause;
   an expired dashboard session switches the notice to the re-login entry. */
function loadStatus(){
  return fetch(API+ROUTES.status,{credentials:"same-origin",headers:{Accept:"application/json"}})
    .then(function(response){
      return response.text().then(function(raw){
        var data=null;
        try{data=raw?JSON.parse(raw):null;}catch(error){data=null;}
        if(isOwnSessionExpired(response,data)){
          noteSessionEnded();
          state.status={status:"error",error:"unauthorized",code:"",httpStatus:401,sessionExpired:true};
          return;
        }
        if(!response.ok||!data||data.status==="error"){
          state.status={status:"error",error:shortError("status: "+response.status),code:String((data&&data.error_code)||("http_"+response.status)),httpStatus:response.status,sessionExpired:false};
          return;
        }
        state.status={status:"ok",data:data};
      });
    })
    .catch(function(error){
      state.status={status:"error",error:shortError(error),code:"network",httpStatus:0,sessionExpired:false};
    });
}
function loadAll(){
  return loadStatus().then(function(){
    // Recomputed every cycle so a recovered read clears the notice instead of
    // leaving a re-login entry behind for a session that still works.
    sessionEnded=!!(state.status&&state.status.sessionExpired);
    updateToast();
    renderAll();
    scheduleNav();
  });
}

/* Navigation highlight follows the panel actually in view. A hash navigation
   (click, direct link, back/forward) pins its target while that panel is
   visible, because browsers may restore a different scroll position; any user
   scroll input releases the pin. Appearance stays the existing .on class on
   both the rail and the narrow-screen nav. */
var NAV_TARGETS=["signal","omitted"];
var navPin="";
function navId(hash){var id=String(hash||"").replace(/^#/,"");return NAV_TARGETS.indexOf(id)>=0?id:"";}
function pickNav(rects,pin,viewport,atBottom){
  var line=Math.min(96,viewport*0.25),passed=NAV_TARGETS[0],lastVisible=passed,containing="",entering="",i,id,rect;
  for(i=0;i<NAV_TARGETS.length;i++){
    id=NAV_TARGETS[i];rect=rects[id];if(!rect)continue;
    if(rect.top<=line){passed=id;if(rect.bottom>line)containing=id;}
    else if(!entering&&rect.top<viewport)entering=id;
    if(rect.top<viewport)lastVisible=id;
  }
  var pinned=pin&&rects[pin];
  if(pinned&&pinned.top<viewport&&pinned.bottom>0)return pin;
  // Bottom panels cannot reach the top line once the page cannot scroll further.
  if(atBottom)return lastVisible;
  // A target scrolled fully past the line yields to the next target on screen.
  return containing||entering||passed;
}
function setActiveNav(id){
  document.querySelectorAll(".rail a[href^='#'],.mobnav a[href^='#']").forEach(function(link){
    var on=link.getAttribute("href")==="#"+id;
    link.classList.toggle("on",on);
    if(on)link.setAttribute("aria-current","location");else link.removeAttribute("aria-current");
  });
}
function syncNav(){
  if(typeof window==="undefined")return;
  var rects={};
  NAV_TARGETS.forEach(function(id){var node=qs(id);if(node&&node.getBoundingClientRect){var box=node.getBoundingClientRect();rects[id]={top:box.top,bottom:box.bottom};}});
  var doc=document.scrollingElement||document.documentElement;
  var viewport=window.innerHeight||(doc&&doc.clientHeight)||0;
  var scrolled=window.pageYOffset||0;
  var atBottom=!!doc&&scrolled>0&&Math.ceil(scrolled+viewport)>=doc.scrollHeight-2;
  setActiveNav(pickNav(rects,navPin,viewport,atBottom));
}
var navQueued=false;
function scheduleNav(){
  if(navQueued||typeof window==="undefined")return;
  navQueued=true;
  var run=function(){navQueued=false;syncNav();};
  if(window.requestAnimationFrame)window.requestAnimationFrame(run);else setTimeout(run,16);
}
function releaseNavPin(){navPin="";scheduleNav();}
function initNav(){
  if(typeof window==="undefined")return;
  navPin=navId(location.hash);
  window.addEventListener("scroll",scheduleNav,{passive:true});
  window.addEventListener("resize",scheduleNav);
  window.addEventListener("hashchange",function(){navPin=navId(location.hash);syncNav();scheduleNav();});
  window.addEventListener("load",scheduleNav);
  ["wheel","touchstart","mousedown","keydown"].forEach(function(type){window.addEventListener(type,releaseNavPin,{passive:true});});
  // mousedown releases the pin before activation; re-pin explicitly so a click
  // on the current fragment (no hashchange) still selects its target.
  document.querySelectorAll(".rail a[href^='#'],.mobnav a[href^='#']").forEach(function(link){
    link.addEventListener("click",function(){var target=navId(link.getAttribute("href"));if(target){navPin=target;syncNav();scheduleNav();}});
  });
  syncNav();
}

function safeAvatarUrl(value){
  if(typeof value!=="string")return "";
  var trimmed=value.trim();
  return /^https:\/\//i.test(trimmed)?trimmed:"";
}
function loadUser(){
  fetch(API+"/user",{credentials:"same-origin"}).then(function(response){
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
  renderToast();
  var live=qs("toast-live");if(live&&live.textContent&&toastModel&&toastModel.visible)announceToast(true);
  renderAll();
}

document.querySelectorAll(".js-lang").forEach(function(button){button.addEventListener("click",function(){lang=lang==="zh-CN"?"en":"zh-CN";try{localStorage.setItem("qg-lang",lang);}catch(error){}applyI18n();});});
document.querySelectorAll(".js-theme").forEach(function(button){button.addEventListener("click",function(){theme=theme==="dark"?"light":"dark";try{localStorage.setItem("qg-theme",theme);}catch(error){}applyI18n();});});
initToast();
initNav();
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
<div class="app">
  <nav class="rail" aria-label="${text("d_title")}">
    <div class="mark" aria-hidden="true">QG</div>
    <a class="on" href="#signal"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><span data-i18n="nav_upstream">${text("nav_upstream")}</span></a>
    <a href="#omitted"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5zM9 8h6M9 12h6M9 16h3"/></svg><span data-i18n="nav_omitted">${text("nav_omitted")}</span></a>
    <span class="sp"></span>
  </nav>
  <main>
    <header class="top">
      <h1 id="main-title" tabindex="-1"><span class="mark m-only" aria-hidden="true">QG</span><span><span data-i18n="d_title">${text("d_title")}</span><small class="lbl" data-i18n="d_sub">${text("d_sub")}</small></span></h1>
      <span class="grow"></span>
      <span class="chip hide-m" id="status-pill"><span class="lamp st-unk" id="pulse" aria-hidden="true"></span><span id="status-text" data-i18n="loading">${text("loading")}</span></span>
      <span class="chip hide-m"><span data-i18n="updated">${text("updated")}</span> <span id="last-refresh">—</span></span>
      <button class="iconbtn js-lang" type="button" data-i18n-aria="lang_label" aria-label="${text("lang_label")}">EN</button>
      <button class="iconbtn js-theme" type="button" data-i18n-aria="theme_label" aria-label="${text("theme_label")}">◐</button>
      <div class="user"><img id="avatar" alt=""><span id="username" aria-live="polite"></span><a href="/logout" data-i18n="acct_logout">${text("acct_logout")}</a></div>
    </header>
    <div class="toast" id="toast" hidden><p id="toast-text"></p><button class="iconbtn toast-x" type="button" id="toast-dismiss" data-i18n-aria="toast_dismiss" aria-label="${text("toast_dismiss")}"><span aria-hidden="true">×</span></button></div>
    <div class="sr-only" id="toast-live" role="status" aria-live="polite"></div>

    <section class="pn panel-block" id="signal" aria-labelledby="status-h">
      <div class="pn-h"><h3 id="status-h" data-i18n="s_title">${text("s_title")}</h3><span class="lbl" id="source-label" data-i18n="s_fresh">${text("s_fresh")}</span></div>
      <div class="table-wrap" id="probe-table"></div>
      <div class="diag">
        <section>
          <h4 data-i18n="rm_title">${text("rm_title")}</h4>
          <div id="read-model"></div>
        </section>
      </div>
    </section>

    <section class="pn panel-block" id="omitted" aria-labelledby="omitted-h">
      <div class="pn-h"><h3 id="omitted-h" data-i18n="o_title">${text("o_title")}</h3><span class="lbl" data-i18n="o_sub">${text("o_sub")}</span></div>
      <div class="diag">
        <section>
          <div class="diag-item"><div class="diag-line"><strong data-i18n="o_traffic_title">${text("o_traffic_title")}</strong><span class="diag-meta mono">/v1/ai/health</span></div><p class="diag-meta" data-i18n="o_traffic_body">${text("o_traffic_body")}</p></div>
          <div class="diag-item"><div class="diag-line"><strong data-i18n="o_quota_title">${text("o_quota_title")}</strong><span class="diag-meta mono">/v1/ai/quota</span></div><p class="diag-meta" data-i18n="o_quota_body">${text("o_quota_body")}</p></div>
          <div class="diag-item"><div class="diag-line"><strong data-i18n="o_org_title">${text("o_org_title")}</strong><span class="diag-meta mono">/v1/ai/org-health</span></div><p class="diag-meta" data-i18n="o_org_body">${text("o_org_body")}</p></div>
          <div class="diag-item"><div class="diag-line"><strong data-i18n="o_changes_title">${text("o_changes_title")}</strong><span class="diag-meta mono">/v1/ai/changes</span></div><p class="diag-meta" data-i18n="o_changes_body">${text("o_changes_body")}</p></div>
          <div class="diag-item"><div class="diag-line"><strong data-i18n="o_shadow_title">${text("o_shadow_title")}</strong><span class="diag-meta mono">/v1/ai/feedback/shadow</span></div><p class="diag-meta" data-i18n="o_shadow_body">${text("o_shadow_body")}</p></div>
          <div class="diag-item"><div class="diag-line"><strong data-i18n="o_diag_title">${text("o_diag_title")}</strong><span class="diag-meta mono">/v1/ai/automation/runs</span></div><p class="diag-meta" data-i18n="o_diag_body">${text("o_diag_body")}</p></div>
        </section>
      </div>
    </section>

    <footer class="footer"><span data-i18n="footer_refresh">${text("footer_refresh")}</span><span><span data-i18n="footer_checked">${text("footer_checked")}</span> <span class="mono" id="checked-at">—</span></span></footer>
  </main>

  <nav class="mobnav" aria-label="${text("d_title")}">
    <a class="on" href="#signal"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg><span data-i18n="nav_upstream">${text("nav_upstream")}</span></a>
    <a href="#omitted"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14v16H5zM9 8h6M9 12h6M9 16h3"/></svg><span data-i18n="nav_omitted">${text("nav_omitted")}</span></a>
  </nav>
</div>
<script>
var I18N=${jsObjectLiteral(LOCALES)};
${CLIENT}
</script>
</body>
</html>`;
}
