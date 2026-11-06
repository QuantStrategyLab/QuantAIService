/** Login page: the approved "Signal Panel" sign-in screen.

  One sign-in method only: a plain navigation to the Worker's existing
  `/login` OAuth start endpoint. The page has no credential inputs and stores
  nothing except the theme and language preferences.

  Nine presentation states are implemented. The Worker callback reports
  outcomes through `?error=`; that result still wins over `?state=`. Both are
  plain, user-writable URL parameters, so neither is authenticated server
  evidence: a handcrafted link can claim any outcome. Copy is chosen by origin:
  a callback `?error=` shows the production state title plus a neutral
  "no session was created" detail, while a design-only `?state=` keeps the
  unverified preview wording. Neither asserts a server-side outcome (for
  example a cleared session, a denied membership or an in-progress check), and
  progress markers for both stay "unverified" rather than "not started". No
  state on this page creates or asserts a session, and `unavailable` disables
  the only action. The backend gap is recorded in QODER_REPORT.md rather than
  invented here.

  References are optional and must arrive as an opaque, validated token in
  `?ref=`; the page never synthesises a correlation ID. A validated reference
  is still redacted through the shared `maskIdentifier` helper before display,
  because a shape-valid label can itself name an account or a secret.
 */

import { LOGIN_LOCALES } from "./login-locales.mjs";
import { LOGIN_CSS } from "./login-styles.mjs";
import { classifyLoginError, escapeHtml, jsObjectLiteral, maskIdentifier } from "./format.mjs";
import { BOOT_SCRIPT } from "./shared.mjs";

/** state -> notice tone, button mode, step markers [s1, s2, s3]. */
export const LOGIN_STATES = {
  idle: { tone: null, btn: "go", steps: ["na", "na", "na"] },
  redirecting: { tone: "info", btn: "busy", steps: ["run", "wait", "wait"] },
  verifying: { tone: "info", btn: "verify", steps: ["done", "run", "wait"] },
  denied: { tone: "bad", btn: "retry", steps: ["done", "fail", "na"] },
  cancel: { tone: "warn", btn: "go", steps: ["stop", "na", "na"] },
  error: { tone: "bad", btn: "retry", steps: ["fail", "na", "na"] },
  expired: { tone: "warn", btn: "go", steps: ["na", "na", "na"] },
  signedout: { tone: "info", btn: "go", steps: ["na", "na", "na"] },
  unavailable: { tone: "unk", btn: "off", steps: ["na", "na", "na"] },
};

const DESIGN_STATES = Object.keys(LOGIN_STATES);

/** The callback error parameter wins over `?state=`; `?state=` only selects a
  presentation state and an unknown value fails closed to `error`. Precedence
  is about which label to show, not about trusting either parameter. */
export function resolveLoginState(search) {
  const params = new URLSearchParams(String(search || ""));
  const kind = classifyLoginError(params.get("error"));
  if (kind !== "none") return kind;
  const requested = params.get("state");
  if (requested === null || requested === "") return "idle";
  return DESIGN_STATES.indexOf(requested) === -1 ? "error" : requested;
}

/** Accept only an opaque correlation token, never a username, email or free
  text. The Worker does not emit references yet; when it does, they must use
  this shape. Invalid or oversized values are dropped. A valid value is still
  masked before it reaches the DOM because the label half can be an account or
  secret name. */
export function resolveLoginRef(search) {
  const raw = new URLSearchParams(String(search || "")).get("ref");
  if (!raw) return "";
  const value = String(raw).trim();
  if (value.length > 64) return "";
  if (!/^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)* · [0-9A-F]{4}(?:-[A-Z0-9]+)*$/.test(value)) return "";
  return maskIdentifier(value);
}

/** Every state this page can read arrives through a plain URL parameter
  (`?error=` and `?state=`). Both are user-writable, so neither is
  authenticated server evidence: a handcrafted link can claim any outcome.
  Until the Worker provides a verifiable marker (for example a signed callback
  result), every URL-derived state is client-attested and renders without
  server progress facts. `loginNotice` still accepts an explicit "server"
  attestation so a future authenticated source can opt in, but this resolver
  never returns it. */
export function resolveLoginAttestation(_search) {
  return "client";
}

/** Classify which honest copy to show. This is not a trust level: a `?error=`
  value is the Worker callback's production sign-in result and a `?state=`
  value is a design-only preview selector, but both remain client-attested.
  The distinction only decides whether the approved production wording or the
  unverified preview wording is displayed. */
export function resolveLoginOrigin(search) {
  const params = new URLSearchParams(String(search || ""));
  if (classifyLoginError(params.get("error")) !== "none") return "callback";
  const requested = params.get("state");
  return requested ? "design" : "none";
}

/** Resolve the notice copy for a state without letting an unattested URL state
  reuse server-fact wording. Returns null for the idle state.

  `origin` defaults to the conservative "design" preview: a caller that has not
  classified its source never gets production copy by accident. */
export function loginNotice(state, attestation, dictionary, origin = "design") {
  if (!state || state === "idle") return null;
  const key = Object.prototype.hasOwnProperty.call(LOGIN_STATES, state) ? state : "error";
  const spec = LOGIN_STATES[key];
  if (!spec.tone) return null;
  if (attestation === "server") {
    return {
      tone: spec.tone,
      title: dictionary["n_" + key + "_t"],
      detail: dictionary["n_" + key],
      attested: true,
    };
  }
  if (origin === "callback") {
    return {
      tone: spec.tone,
      title: dictionary["n_" + key + "_t"],
      detail: dictionary.callback_detail,
      attested: false,
    };
  }
  return {
    tone: spec.tone,
    title: dictionary.preview_prefix + dictionary["n_" + key + "_t"],
    detail: dictionary.preview_detail,
    attested: false,
  };
}

/** Progress markers are server facts too. A client-attested state must not
  show "not started", "in progress" or "failed" steps that the server never
  reported; it shows a neutral "unverified" marker instead. Only a
  server-attested state may render its real markers, including a genuine "na"
  (not started) step. */
export function loginSteps(state, attestation) {
  const key = Object.prototype.hasOwnProperty.call(LOGIN_STATES, state) ? state : "error";
  if (attestation !== "server") return ["unverified", "unverified", "unverified"];
  return LOGIN_STATES[key].steps.slice();
}

/** Split locale copy on {placeholder} so it renders as <code>, never as HTML. */
export function stepSegments(template) {
  const source = String(template ?? "");
  const segments = [];
  const pattern = /\{([^}]+)\}/g;
  let cursor = 0;
  let match = pattern.exec(source);
  while (match !== null) {
    if (match.index > cursor) {
      segments.push({ type: "text", value: source.slice(cursor, match.index) });
    }
    segments.push({ type: "code", value: match[1] });
    cursor = match.index + match[0].length;
    match = pattern.exec(source);
  }
  if (cursor < source.length) {
    segments.push({ type: "text", value: source.slice(cursor) });
  }
  return segments;
}

function richText(template) {
  return stepSegments(template)
    .map((segment) => (segment.type === "code"
      ? "<code>" + escapeHtml(segment.value) + "</code>"
      : escapeHtml(segment.value)))
    .join("");
}

export function loginHTML() {
  const t = LOGIN_LOCALES["zh-CN"];
  const tx = (key) => escapeHtml(t[key]);

  return `<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${tx("l_title")} · ${tx("brand")}</title>
<script>${BOOT_SCRIPT}</script>
<script>(function(){try{var q=new URLSearchParams(location.search),l=q.get("lang"),t=q.get("theme");if(l==="zh-CN"||l==="en")document.documentElement.lang=l;if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);}catch(e){}})();</script>
<style>${LOGIN_CSS}</style>
</head>
<body>
<section class="login">
  <div class="l-art">
    <div class="brand"><span class="mark" aria-hidden="true">QG</span><span data-i18n="brand">${tx("brand")}</span></div>
    <div class="tools">
      <button class="iconbtn js-lang" type="button" data-i18n-aria="switch_to_en" aria-label="${tx("switch_to_en")}">EN</button>
      <button class="iconbtn js-theme" type="button" data-i18n-aria="use_dark" aria-label="${tx("use_dark")}">◐</button>
    </div>
    <svg class="routes" viewBox="0 0 560 520" preserveAspectRatio="xMaxYMid meet" aria-hidden="true" id="routes"></svg>
    <div>
      <h2><span data-i18n="l_head_1">${tx("l_head_1")}</span><br><em data-i18n="l_head_2">${tx("l_head_2")}</em><span data-i18n="l_head_dot">${tx("l_head_dot")}</span></h2>
      <p data-i18n="l_sub">${tx("l_sub")}</p>
    </div>
    <div class="l-stat">
      <div><span data-i18n="l_s1">${tx("l_s1")}</span><b class="mono">GitHub OAuth 2.0</b></div>
      <div><span data-i18n="l_s2">${tx("l_s2")}</span><b class="mono">dashboard:read</b></div>
      <div><span data-i18n="l_s3">${tx("l_s3")}</span><b class="mono">/v1/ai/*</b></div>
    </div>
  </div>
  <main class="l-form">
    <div class="tools">
      <button class="iconbtn js-lang" type="button" data-i18n-aria="switch_to_en" aria-label="${tx("switch_to_en")}">EN</button>
      <button class="iconbtn js-theme" type="button" data-i18n-aria="use_dark" aria-label="${tx("use_dark")}">◐</button>
    </div>
    <div class="l-card">
      <span class="lbl" data-i18n="l_kicker">${tx("l_kicker")}</span>
      <h1 data-i18n="l_title">${tx("l_title")}</h1>
      <p class="sub" data-i18n="l_desc">${tx("l_desc")}</p>

      <div class="note" id="note" role="status" aria-live="polite"><b id="note-t"></b><span id="note-d"></span><span class="ref" id="note-r"></span></div>

      <a class="gh" id="gh" href="/login">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
        <span class="spin" aria-hidden="true"></span>
        <span id="gh-l">${tx("l_go")}</span>
        <span class="ar" aria-hidden="true">→</span>
      </a>
      <div class="only" data-i18n="l_only">${tx("l_only")}</div>

      <section class="pn flow" aria-labelledby="flow-h">
        <div class="pn-h"><h3 id="flow-h" data-i18n="f_title">${tx("f_title")}</h3><span class="lbl">OAuth 2.0</span></div>
        <ol id="steps">
          <li class="step"><span class="n">01</span><b data-i18n="f1">${tx("f1")}</b><span class="st"></span><p data-i18n-rich="f1p">${richText(t.f1p)}</p></li>
          <li class="step"><span class="n">02</span><b data-i18n="f2">${tx("f2")}</b><span class="st"></span><p data-i18n-rich="f2p">${richText(t.f2p)}</p></li>
          <li class="step"><span class="n">03</span><b data-i18n="f3">${tx("f3")}</b><span class="st"></span><p data-i18n-rich="f3p">${richText(t.f3p)}</p></li>
        </ol>
      </section>
      <div class="legal"><svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2" y="5" width="8" height="6"/><path d="M4 5V3.5a2 2 0 0 1 4 0V5"/></svg><span data-i18n="l_legal">${tx("l_legal")}</span></div>
    </div>
  </main>
</section>
<script>
var I18N=${jsObjectLiteral(LOGIN_LOCALES)};
var LOGIN_STATES=${jsObjectLiteral(LOGIN_STATES)};
var DESIGN_STATES=${jsObjectLiteral(DESIGN_STATES)};
${classifyLoginError.toString()}
${stepSegments.toString()}
${maskIdentifier.toString()}
${resolveLoginState.toString()}
${resolveLoginAttestation.toString()}
${resolveLoginOrigin.toString()}
${resolveLoginRef.toString()}
${loginNotice.toString()}
${loginSteps.toString()}
var root=document.documentElement;
var lang=root.lang==="en"?"en":"zh-CN";
var theme=root.getAttribute("data-theme")==="dark"?"dark":"light";
var state=resolveLoginState(location.search);
var attestation=resolveLoginAttestation(location.search);
var origin=resolveLoginOrigin(location.search);
var ref=resolveLoginRef(location.search);
var navigating=false;

function setText(node,value){if(node)node.textContent=value;}

function renderRich(node,template){
  var fragment=document.createDocumentFragment();
  stepSegments(template).forEach(function(segment){
    if(segment.type==="code"){var code=document.createElement("code");code.textContent=segment.value;fragment.appendChild(code);}
    else{fragment.appendChild(document.createTextNode(segment.value));}
  });
  while(node.firstChild)node.removeChild(node.firstChild);
  node.appendChild(fragment);
}

function renderState(t){
  var spec=LOGIN_STATES[state]||LOGIN_STATES.error;
  var notice=loginNotice(state,attestation,t,origin);
  var note=document.getElementById("note");
  note.className="note";
  note.setAttribute("role","status");
  note.setAttribute("aria-live","polite");
  setText(document.getElementById("note-t"),"");
  setText(document.getElementById("note-d"),"");
  setText(document.getElementById("note-r"),"");
  if(notice){
    note.className="note on "+notice.tone;
    if(notice.tone==="bad"){note.setAttribute("role","alert");note.setAttribute("aria-live","assertive");}
    setText(document.getElementById("note-t"),notice.title);
    setText(document.getElementById("note-d"),notice.detail);
    if(ref){setText(document.getElementById("note-r"),t.ref+" "+ref);}
  }
  var gh=document.getElementById("gh");
  gh.removeAttribute("aria-busy");
  gh.removeAttribute("aria-disabled");
  gh.tabIndex=0;
  gh.setAttribute("href","/login");
  var labels={go:t.l_go,retry:t.l_retry,busy:t.l_busy,verify:t.l_verify,off:t.l_off};
  var buttonLabel=labels[spec.btn];
  if(attestation!=="server"&&(spec.btn==="busy"||spec.btn==="verify"||spec.btn==="off")){
    buttonLabel=t["preview_"+spec.btn];
  }
  setText(document.getElementById("gh-l"),buttonLabel);
  if(attestation==="server"&&(spec.btn==="busy"||spec.btn==="verify"))gh.setAttribute("aria-busy","true");
  if(attestation!=="server"&&(spec.btn==="busy"||spec.btn==="verify"))gh.setAttribute("aria-disabled","true");
  if(spec.btn==="off"){gh.setAttribute("aria-disabled","true");gh.tabIndex=-1;gh.removeAttribute("href");}
  var statusLabels={done:t.s_done,run:t.s_run,wait:t.s_wait,fail:t.s_fail,stop:t.s_stop,na:t.s_na,unverified:t.s_unverified};
  var steps=loginSteps(state,attestation);
  document.querySelectorAll("#steps .step").forEach(function(item,index){
    var key=steps[index]||"na";
    var slot=item.querySelector(".st");
    item.classList.toggle("cur",key==="run"||key==="fail"||key==="stop");
    while(slot.firstChild)slot.removeChild(slot.firstChild);
    if(state!=="idle"){
      var dot=document.createElement("i");
      dot.className="dot "+key;
      dot.setAttribute("aria-hidden","true");
      slot.appendChild(dot);
      slot.appendChild(document.createTextNode(statusLabels[key]));
    }
  });
}

function apply(){
  var t=I18N[lang]||I18N["zh-CN"];
  root.lang=lang;
  root.setAttribute("data-theme",theme);
  document.querySelectorAll("[data-i18n]").forEach(function(node){
    var key=node.getAttribute("data-i18n");
    if(t[key]!==undefined)node.textContent=t[key];
  });
  document.querySelectorAll("[data-i18n-rich]").forEach(function(node){
    var key=node.getAttribute("data-i18n-rich");
    if(t[key]!==undefined)renderRich(node,t[key]);
  });
  document.querySelectorAll(".js-lang").forEach(function(button){
    button.textContent=lang==="zh-CN"?"EN":"中";
    button.setAttribute("aria-label",lang==="zh-CN"?t.switch_to_en:t.switch_to_zh);
  });
  document.querySelectorAll(".js-theme").forEach(function(button){
    button.setAttribute("aria-label",theme==="dark"?t.use_light:t.use_dark);
  });
  document.title=t.l_title+" · "+t.brand;
  renderState(t);
}

document.querySelectorAll(".js-lang").forEach(function(button){
  button.addEventListener("click",function(){
    lang=lang==="zh-CN"?"en":"zh-CN";
    try{localStorage.setItem("qg-lang",lang);}catch(e){}
    apply();
  });
});
document.querySelectorAll(".js-theme").forEach(function(button){
  button.addEventListener("click",function(){
    theme=theme==="dark"?"light":"dark";
    try{localStorage.setItem("qg-theme",theme);}catch(e){}
    apply();
  });
});

(function signIn(){
  var gh=document.getElementById("gh");
  gh.addEventListener("click",function(event){
    var spec=LOGIN_STATES[state]||LOGIN_STATES.error;
    if(navigating||spec.btn==="off"||spec.btn==="busy"||spec.btn==="verify"){
      event.preventDefault();
      return;
    }
    navigating=true;
    gh.setAttribute("aria-busy","true");
    setText(document.getElementById("gh-l"),(I18N[lang]||I18N["zh-CN"]).l_busy);
  });
  window.addEventListener("pageshow",function(){navigating=false;apply();});
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
