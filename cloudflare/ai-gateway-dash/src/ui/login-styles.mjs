/** Login page styles, ported from the approved mock.

  Scoped to the login page as a separate stylesheet so the dashboard's shared
  CSS string stays byte-identical. The tokens already include the login mock's
  AA contrast adjustments. No external fonts or scripts are referenced.
 */

export const LOGIN_CSS = `
:root{
  --f-mono:"Martian Mono","JetBrains Mono","IBM Plex Mono",ui-monospace,"Noto Sans Mono CJK SC",monospace;
  --f-ui:"IBM Plex Sans Condensed","IBM Plex Sans","Noto Sans CJK SC",system-ui,sans-serif;
  --r:2px;
}
[data-theme=light]{
  --bg:#E9E6DF; --panel:#F4F2EC; --panel2:#ECE9E1; --ink:#1B1A17; --ink2:#55524A; --ink3:#7A766C;
  --line:#CFCABE; --line2:#B7B1A3; --sig:#D4470B; --ok:#2F7D4F; --warn:#9A6508; --bad:#B42318; --unk:#5F6375;
  --grid:rgba(27,26,23,.05);
}
[data-theme=dark]{
  --bg:#0E0F10; --panel:#151719; --panel2:#1B1E21; --ink:#ECE8DF; --ink2:#A7A398; --ink3:#85827A;
  --line:#272A2E; --line2:#3A3E43; --sig:#FF7A1A; --ok:#5BC48A; --warn:#E8B04A; --bad:#FF6B5E; --unk:#8D93A6;
  --grid:rgba(236,232,223,.035);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--ink);font:14px/1.45 var(--f-ui);
  background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:24px 24px;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:0;height:0}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
.mono{font-family:var(--f-mono);font-feature-settings:"tnum"}
.lbl{font-family:var(--f-mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3)}
:lang(zh-CN) .lbl{letter-spacing:.06em}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* crop-mark panel motif (shared with dashboard) */
.pn{position:relative;background:var(--panel);border:1px solid var(--line)}
.pn::before,.pn::after{content:"";position:absolute;width:9px;height:9px;border:solid var(--sig);pointer-events:none}
.pn::before{top:-1px;left:-1px;border-width:2px 0 0 2px}
.pn::after{bottom:-1px;right:-1px;border-width:0 2px 2px 0}
.pn-h{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line)}
.pn-h h3{font:600 13px/1 var(--f-ui);letter-spacing:.02em;text-transform:uppercase}
:lang(zh-CN) .pn-h h3{text-transform:none;letter-spacing:.04em}
.mark{width:40px;height:40px;display:grid;place-items:center;background:var(--sig);color:#fff;font:700 13px/1 var(--f-mono);clip-path:polygon(0 0,100% 0,100% 70%,70% 100%,0 100%)}
[data-theme=dark] .mark{color:#160A02}
.iconbtn{min-width:32px;height:32px;padding:0 8px;display:grid;place-items:center;border:1px solid var(--line2);border-radius:var(--r);font:600 11px/1 var(--f-mono);color:var(--ink2)}
.iconbtn:focus-visible{outline:2px solid var(--sig);outline-offset:2px}

/* ---------- LAYOUT ---------- */
.login{display:grid;grid-template-columns:1.1fr 1fr;min-height:100vh}
.l-art{position:relative;border-right:1px solid var(--line);padding:40px 48px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;background:var(--panel)}
.brand{display:flex;align-items:center;gap:12px;font:600 15px/1 var(--f-ui);position:relative}
.l-art h2{font:500 52px/1.02 var(--f-mono);letter-spacing:-.04em;max-width:640px}
:lang(zh-CN) .l-art h2{font:700 50px/1.18 var(--f-ui);letter-spacing:.02em}
.l-art h2 em{font-style:normal;color:var(--sig)}
/* The approved mock uses an explicit break only for zh-CN; English wraps naturally. */
:lang(en) .l-art h2 br{display:none}
.l-art p{color:var(--ink2);max-width:440px;margin-top:18px;font-size:15px}
.l-art svg.routes{position:absolute;right:24px;top:110px;width:520px;height:300px;opacity:.9}
.l-art>div{position:relative}
.l-stat{display:flex;gap:28px;font:400 11px/1.6 var(--f-mono);color:var(--ink3)}
.l-stat b{display:block;color:var(--ink);font-weight:500;font-size:13px}

.l-form{display:flex;flex-direction:column;justify-content:center;padding:40px 72px;position:relative}
.tools{position:absolute;top:24px;right:24px;display:flex;gap:8px}
.l-card{max-width:420px;width:100%}
.l-card h1{font:600 28px/1.2 var(--f-ui);margin:10px 0 8px}
.l-card .sub{color:var(--ink2);margin-bottom:24px;font-size:14.5px}

/* the single sign-in control */
.gh{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;height:52px;background:var(--ink);color:var(--bg);font:600 15px/1 var(--f-ui);border-radius:var(--r);letter-spacing:.01em;position:relative;text-decoration:none}
.gh svg{width:20px;height:20px;fill:currentColor;flex:none}
.gh .ar{position:absolute;right:16px;font:400 13px/1 var(--f-mono);opacity:.7}
.gh:hover{background:color-mix(in srgb,var(--ink) 88%,var(--sig))}
.gh:focus-visible{outline:2px solid var(--sig);outline-offset:3px}
.gh[aria-disabled=true]{background:var(--panel2);color:var(--ink3);border:1px dashed var(--line2);cursor:not-allowed}
.gh[aria-busy=true]{cursor:progress;background:color-mix(in srgb,var(--ink) 82%,var(--bg))}
.gh .spin{display:none;width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:sp 1s linear infinite;flex:none}
.gh[aria-busy=true] .spin{display:block}.gh[aria-busy=true] svg,.gh[aria-busy=true] .ar,.gh[aria-disabled=true] .ar{display:none}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.gh .spin,.step .dot.run{animation:none}}
.only{margin-top:12px;font:400 11px/1.5 var(--f-mono);color:var(--ink3);display:flex;gap:8px;align-items:center}
.only::before{content:"";width:6px;height:6px;background:var(--sig);flex:none}

/* status notice (one at a time, above the button) */
.note{display:none;border:1px solid var(--line2);border-left-width:3px;background:var(--panel);padding:12px 14px;margin-bottom:18px;font-size:13px;color:var(--ink2)}
.note.on{display:block}
.note b{display:block;font-weight:600;margin-bottom:3px;color:var(--ink)}
.note .ref{display:block;margin-top:8px;font:400 10.5px/1.4 var(--f-mono);color:var(--ink3)}
.note.bad{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 8%,var(--panel))}.note.bad b{color:var(--bad)}
.note.warn{border-color:var(--warn);background:color-mix(in srgb,var(--warn) 9%,var(--panel))}.note.warn b{color:var(--warn)}
.note.info{border-color:var(--ink2)}
.note.unk{border-color:var(--unk);border-style:dashed;border-left-style:solid}.note.unk b{color:var(--unk)}

/* how-it-works panel doubles as the progress tracker */
.flow{margin-top:28px}
.flow ol{list-style:none}
.step{display:grid;grid-template-columns:28px 1fr auto;gap:2px 10px;padding:12px 14px;border-bottom:1px solid var(--line)}
.step:last-child{border-bottom:0}
.step .n{font:500 11px/1.5 var(--f-mono);color:var(--sig)}
.step b{font-weight:600;font-size:13.5px}
.step p{grid-column:2/4;color:var(--ink2);font-size:12.5px;line-height:1.5}
.step code{font:500 11px/1 var(--f-mono);background:var(--panel2);border:1px solid var(--line);padding:2px 4px;color:var(--ink)}
.step .st{font:500 10px/1.5 var(--f-mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);display:flex;align-items:center;gap:6px;white-space:nowrap}
.step .dot{width:7px;height:7px;border:1px solid var(--line2);flex:none}
.step .dot.done{background:var(--ink2);border-color:var(--ink2)}
.step .dot.run{background:var(--sig);border-color:var(--sig);animation:bl 1.1s steps(2) infinite}
.step .dot.fail{background:var(--bad);border-color:var(--bad)}
.step .dot.stop{background:var(--warn);border-color:var(--warn)}
@keyframes bl{50%{opacity:.25}}
.step.cur{background:var(--panel2)}
.legal{margin-top:20px;font-size:12px;color:var(--ink3);display:flex;gap:10px;align-items:flex-start}
.legal svg{width:12px;height:12px;stroke:currentColor;fill:none;stroke-width:1.6;flex:none;margin-top:3px}

/* ---------- MOBILE ---------- */
@media (max-width:760px){
  body{font-size:13px}
  .login{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  .l-art{padding:20px 20px 22px;border-right:0;border-bottom:1px solid var(--line)}
  .l-art h2{font-size:28px;margin-top:26px}
  :lang(zh-CN) .l-art h2{font-size:26px}
  .l-art p{font-size:13px;margin-top:8px}
  .l-art svg.routes{width:300px;height:280px;right:-110px;top:-50px;opacity:.45}
  .l-stat{display:none}
  .l-form{padding:22px 20px 28px;justify-content:flex-start}
  .tools{top:16px;right:16px;position:absolute}
  .l-art .tools{position:absolute}
  .l-form>.tools{display:none}
  .l-card h1{font-size:22px;margin-top:6px}
  .l-card .sub{margin-bottom:18px;font-size:13.5px}
  .gh{height:50px}
  .flow{margin-top:22px}
  .step{padding:10px 12px}
  .legal{margin-top:16px}
}
@media (min-width:761px){.l-art .tools{display:none}}
`;
