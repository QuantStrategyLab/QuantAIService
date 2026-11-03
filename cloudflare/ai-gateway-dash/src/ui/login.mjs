/** Login page: GitHub OAuth 2.0 only.

  The only action is an anchor to the existing `/login` OAuth start route. There
  is no project selector, token field, password, email or SSO input. The denied
  / cancelled / failed states are derived from the existing `?error=` redirect
  produced by the OAuth callback.
 */

import { LOCALES } from "./locales.mjs";
import { CSS } from "./styles.mjs";
import { classifyLoginError, escapeHtml, jsObjectLiteral } from "./format.mjs";
import { BOOT_SCRIPT } from "./shared.mjs";

const zh = LOCALES["zh-CN"];

function text(key) {
  return escapeHtml(zh[key] ?? key);
}

export function loginHTML() {
  return `<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${text("brand")}</title>
<script>${BOOT_SCRIPT}</script>
<style>${CSS}</style>
</head>
<body data-view="login">
<section class="login" aria-label="${text("login_kicker")}">
  <div class="l-art">
    <div class="brand"><span class="mark" aria-hidden="true">QG</span><span data-i18n="brand">${text("brand")}</span></div>
    <svg class="routes" viewBox="0 0 560 520" preserveAspectRatio="xMaxYMid meet" aria-hidden="true" id="routes"></svg>
    <div>
      <h2><span data-i18n="login_head_1">${text("login_head_1")}</span><br><em data-i18n="login_head_2">${text("login_head_2")}</em></h2>
      <p data-i18n="login_sub">${text("login_sub")}</p>
    </div>
    <div class="l-stat">
      <div><span data-i18n="login_s1">${text("login_s1")}</span><b class="mono" data-i18n="login_stat1">${text("login_stat1")}</b></div>
      <div><span data-i18n="login_s2">${text("login_s2")}</span><b class="mono" data-i18n="login_stat2">${text("login_stat2")}</b></div>
      <div><span data-i18n="login_s3">${text("login_s3")}</span><b class="mono" data-i18n="login_stat3">${text("login_stat3")}</b></div>
    </div>
  </div>
  <div class="l-form">
    <div class="tools">
      <button class="iconbtn js-lang" type="button" data-i18n-aria="lang_label" aria-label="${text("lang_label")}">EN</button>
      <button class="iconbtn js-theme" type="button" data-i18n-aria="theme_label" aria-label="${text("theme_label")}">◐</button>
    </div>
    <div class="l-card">
      <span class="lbl" data-i18n="login_kicker">${text("login_kicker")}</span>
      <h3 data-i18n="login_title">${text("login_title")}</h3>
      <p class="sub" data-i18n="login_desc">${text("login_desc")}</p>
      <div class="alert" id="alert" role="alert" aria-live="assertive"><b id="alert-t"></b><span id="alert-d"></span></div>
      <a class="gh" id="gh" href="/login">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
        <span class="spin" aria-hidden="true"></span>
        <span id="gh-l" data-i18n="login_go">${text("login_go")}</span>
        <span class="ar" aria-hidden="true">→</span>
      </a>
      <div class="only" data-i18n="login_only">${text("login_only")}</div>
      <section class="pn flow" aria-labelledby="flow-h">
        <div class="pn-h"><h3 id="flow-h" data-i18n="login_flow_title">${text("login_flow_title")}</h3><span class="lbl">OAuth 2.0</span></div>
        <ol>
          <li><span class="n">01</span><b data-i18n="login_f1">${text("login_f1")}</b><p data-i18n="login_f1p">${text("login_f1p")}</p></li>
          <li><span class="n">02</span><b data-i18n="login_f2">${text("login_f2")}</b><p data-i18n="login_f2p">${text("login_f2p")}</p></li>
          <li><span class="n">03</span><b data-i18n="login_f3">${text("login_f3")}</b><p data-i18n="login_f3p">${text("login_f3p")}</p></li>
        </ol>
      </section>
      <div class="legal"><span class="lamp st-ok" aria-hidden="true"></span><span data-i18n="login_legal">${text("login_legal")}</span></div>
    </div>
  </div>
</section>
<script>
var I18N=${jsObjectLiteral(LOCALES)};
${classifyLoginError.toString()}
var root=document.documentElement;
var lang=root.lang==="en"?"en":"zh-CN";
var theme=root.getAttribute("data-theme")==="dark"?"dark":"light";
var t=I18N[lang]||I18N["zh-CN"];
function apply(){
  t=I18N[lang]||I18N["zh-CN"];
  root.lang=lang;
  root.setAttribute("data-theme",theme);
  document.querySelectorAll("[data-i18n]").forEach(function(node){var key=node.getAttribute("data-i18n");if(t[key]!==undefined)node.textContent=t[key];});
  document.querySelectorAll("[data-i18n-aria]").forEach(function(node){var key=node.getAttribute("data-i18n-aria");if(t[key]!==undefined)node.setAttribute("aria-label",t[key]);});
  document.querySelectorAll(".js-lang").forEach(function(button){button.textContent=lang==="zh-CN"?"EN":"中";button.setAttribute("aria-label",t.lang_label);});
  document.querySelectorAll(".js-theme").forEach(function(button){button.setAttribute("aria-label",t.theme_label);});
  document.title=t.brand;
  renderError();
}
function renderError(){
  var raw="";
  try{raw=new URLSearchParams(location.search).get("error")||"";}catch(e){raw="";}
  var kind=classifyLoginError(raw);
  var alert=document.getElementById("alert");
  if(!alert)return;
  if(kind==="none"){alert.className="alert";document.getElementById("alert-t").textContent="";document.getElementById("alert-d").textContent="";return;}
  var titleKey="err_error_t",detailKey="err_error";
  if(kind==="denied"){titleKey="err_denied_t";detailKey="err_denied";}
  if(kind==="cancel"){titleKey="err_cancel_t";detailKey="err_cancel";}
  if(kind==="expired"){titleKey="err_expired_t";detailKey="err_expired";}
  alert.className="alert on"+(kind==="cancel"||kind==="expired"?" warn":"");
  document.getElementById("alert-t").textContent=t[titleKey];
  document.getElementById("alert-d").textContent=t[detailKey];
}
document.querySelectorAll(".js-lang").forEach(function(button){button.addEventListener("click",function(){lang=lang==="zh-CN"?"en":"zh-CN";try{localStorage.setItem("qg-lang",lang);}catch(e){}apply();});});
document.querySelectorAll(".js-theme").forEach(function(button){button.addEventListener("click",function(){theme=theme==="dark"?"light":"dark";try{localStorage.setItem("qg-theme",theme);}catch(e){}apply();});});
(function busy(){
  var gh=document.getElementById("gh");
  if(!gh)return;
  gh.addEventListener("click",function(){gh.setAttribute("aria-busy","true");var label=document.getElementById("gh-l");if(label)label.textContent=t.login_busy;});
  window.addEventListener("pageshow",function(){gh.removeAttribute("aria-busy");var label=document.getElementById("gh-l");if(label)label.textContent=t.login_go;});
})();
(function routes(){
  var svg=document.getElementById("routes");
  if(!svg)return;
  var NS="http://www.w3.org/2000/svg";
  function node(name,attrs){var item=document.createElementNS(NS,name);for(var key in attrs){item.setAttribute(key,attrs[key]);}return item;}
  var left=[[40,90],[40,200],[40,310],[40,420]];
  var right=[[500,140],[500,260],[500,380]];
  left.forEach(function(a,i){right.forEach(function(b,j){
    var hot=(i+j)%3===0;
    var path=node("path",{d:"M"+a[0]+" "+a[1]+" C 270 "+a[1]+", 270 "+b[1]+", "+b[0]+" "+b[1],fill:"none",stroke:hot?"var(--sig)":"var(--line2)","stroke-width":hot?1.6:1});
    if(!hot)path.setAttribute("stroke-dasharray","3 5");
    svg.appendChild(path);
  });});
  left.forEach(function(a){svg.appendChild(node("rect",{x:a[0]-6,y:a[1]-6,width:12,height:12,fill:"var(--panel)",stroke:"var(--ink)"}));});
  right.forEach(function(b){svg.appendChild(node("circle",{cx:b[0],cy:b[1],r:9,fill:"var(--sig)"}));});
})();
apply();
</script>
</body>
</html>`;
}
