/** Signal Panel styles, ported from the approved design tokens.

  No external fonts or scripts: the stacks fall back to system and CJK fonts.
 */

export const CSS = `
:root{
  --f-mono:"Martian Mono","JetBrains Mono","IBM Plex Mono",ui-monospace,"Noto Sans Mono CJK SC",monospace;
  --f-ui:"IBM Plex Sans Condensed","IBM Plex Sans","Noto Sans CJK SC",system-ui,sans-serif;
  --r:2px;
}
[data-theme=light]{
  --bg:#E9E6DF; --panel:#F4F2EC; --panel2:#ECE9E1; --ink:#1B1A17; --ink2:#55524A; --ink3:#8A867C;
  --line:#CFCABE; --line2:#B7B1A3; --sig:#D4470B; --sig-soft:#F6D9C8; --ok:#2F7D4F; --warn:#B7790A; --bad:#B42318; --unk:#6B6F80;
  --grid:rgba(27,26,23,.05);
  --s1:#D4470B; --s2:#1F5F8B; --s3:#6A7A2A; --s4:#8C5E9E;
}
[data-theme=dark]{
  --bg:#0E0F10; --panel:#151719; --panel2:#1B1E21; --ink:#ECE8DF; --ink2:#A7A398; --ink3:#6E6B64;
  --line:#272A2E; --line2:#3A3E43; --sig:#FF7A1A; --sig-soft:#3A2210; --ok:#5BC48A; --warn:#E8B04A; --bad:#FF6B5E; --unk:#8D93A6;
  --grid:rgba(236,232,223,.035);
  --s1:#FF7A1A; --s2:#5AA9E6; --s3:#B5C95A; --s4:#C79BE0;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100%}
body{background:var(--bg);color:var(--ink);font:14px/1.45 var(--f-ui);
  background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:24px 24px;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:0;height:0}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
button,a{font:inherit}
a{color:inherit}
.mono{font-family:var(--f-mono);font-feature-settings:"tnum"}
.lbl{font-family:var(--f-mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3)}
:lang(zh-CN) .lbl{letter-spacing:.06em}
:focus-visible{outline:2px solid var(--sig);outline-offset:3px}
.pn{position:relative;background:var(--panel);border:1px solid var(--line);min-width:0}
.pn::before,.pn::after{content:"";position:absolute;width:9px;height:9px;border:solid var(--sig);pointer-events:none}
.pn::before{top:-1px;left:-1px;border-width:2px 0 0 2px}
.pn::after{bottom:-1px;right:-1px;border-width:0 2px 2px 0}
.pn-h{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border-bottom:1px solid var(--line)}
.pn-h h3{font:600 13px/1 var(--f-ui);letter-spacing:.02em;text-transform:uppercase}
:lang(zh-CN) .pn-h h3{text-transform:none;letter-spacing:.04em}
.lamp{display:inline-block;width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 0 3px color-mix(in srgb,currentColor 22%,transparent)}
.st-ok{color:var(--ok)} .st-warn{color:var(--warn)} .st-bad{color:var(--bad)} .st-unk{color:var(--unk)}
.tag{display:inline-block;font:500 10px/1 var(--f-mono);letter-spacing:.08em;text-transform:uppercase;padding:4px 6px;border:1px solid currentColor;border-radius:var(--r)}
.seg{display:inline-flex;border:1px solid var(--line2);border-radius:var(--r)}
.seg button{padding:6px 10px;font:500 11px/1 var(--f-mono);color:var(--ink2);white-space:nowrap}
.seg button+button{border-left:1px solid var(--line2)}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--panel)}
.app{display:grid;grid-template-columns:76px 1fr;min-height:100vh}
.rail{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;align-items:center;padding:16px 0;gap:6px}
.mark{width:40px;height:40px;display:grid;place-items:center;background:var(--sig);color:#fff;font:700 13px/1 var(--f-mono);margin-bottom:18px;clip-path:polygon(0 0,100% 0,100% 70%,70% 100%,0 100%)}
.rail a{width:60px;padding:9px 0 7px;display:flex;flex-direction:column;align-items:center;gap:5px;color:var(--ink3);text-decoration:none;font:500 10px/1 var(--f-ui);border-left:2px solid transparent}
.rail a svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.6}
.rail a.on,.rail a:focus-visible{color:var(--ink);border-left-color:var(--sig)}
.rail .sp{flex:1}
main{position:relative;padding:0 24px 28px;min-width:0}
.top{display:flex;align-items:center;gap:16px;height:64px;border-bottom:1px solid var(--line);margin-bottom:20px}
.top h1{font:600 18px/1 var(--f-ui);letter-spacing:.01em}
.top h1 small{display:block;margin-top:5px}
.top .grow{flex:1}
.chip{display:inline-flex;align-items:center;gap:8px;white-space:nowrap;padding:6px 10px;border:1px solid var(--line2);font:500 11px/1 var(--f-mono);border-radius:var(--r);color:var(--ink2)}
.iconbtn{width:32px;height:32px;display:grid;place-items:center;border:1px solid var(--line2);border-radius:var(--r);font:600 11px/1 var(--f-mono)}
.user{display:flex;align-items:center;gap:8px;font:500 11px/1 var(--f-mono);color:var(--ink2)}
.user img{display:none;width:24px;height:24px;border-radius:50%;border:1px solid var(--line2)}
.user a{text-decoration:none;border:1px solid var(--line2);padding:6px 8px;border-radius:var(--r);white-space:nowrap}
.readouts{display:grid;grid-template-columns:repeat(5,1fr);gap:0;margin-bottom:16px}
.ro{padding:14px 16px 12px;border-right:1px solid var(--line);min-width:0}
.ro:last-child{border-right:0}
.ro .v{font:500 28px/1 var(--f-mono);letter-spacing:-.02em;margin:10px 0 8px;display:flex;align-items:baseline;gap:6px;overflow-wrap:anywhere}
.ro .v small{font-size:11px;color:var(--ink3);letter-spacing:0}
.ro .v.unk{color:var(--unk)}
.meter{display:flex;gap:2px;height:8px}
.meter i{flex:1;background:var(--line)}
.meter i.f{background:var(--ink2)} .meter i.h{background:var(--sig)}
.meter.na{background:repeating-linear-gradient(135deg,var(--line) 0 4px,transparent 4px 8px);gap:0}
.meter.na i{background:transparent}
.ro .foot{margin-top:8px;font:400 10.5px/1.3 var(--f-mono);color:var(--ink3)}
.grid2{display:grid;grid-template-columns:1.65fr 1fr;gap:16px;margin-bottom:16px}
.grid2.even{grid-template-columns:1fr 1fr}
.panel-block{margin-bottom:16px}
.chart{padding:12px 14px 10px}
.chart svg{display:block;width:100%;height:210px}
.na{padding:18px 16px;display:flex;gap:14px;align-items:stretch;min-height:132px}
.na-bar{width:6px;flex:none;border:1px solid var(--line);background:repeating-linear-gradient(135deg,var(--line) 0 4px,transparent 4px 8px)}
.na-body{display:flex;flex-direction:column;justify-content:center;gap:6px;min-width:0}
.na-big{font:500 26px/1 var(--f-mono);color:var(--unk)}
.na-body b{font-weight:600}
.na-body p{color:var(--ink2);font-size:12px}
table{width:100%;border-collapse:collapse}
th{font:500 10px/1 var(--f-mono);letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);text-align:left;padding:9px 14px;border-bottom:1px solid var(--line)}
td{padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px;white-space:nowrap}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-family:var(--f-mono);font-size:12px}
.prov{display:flex;align-items:center;gap:10px}
.prov b{font-weight:600}
.prov div span{color:var(--ink3);font:400 11px/1 var(--f-mono)}
.table-wrap{overflow:auto}
.quota{padding:6px 14px 12px}
.q{padding:10px 0;border-bottom:1px solid var(--line)}
.q:last-child{border-bottom:0}
.q .row{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:7px}
.q .row b{font-weight:600;overflow-wrap:anywhere}
.q .row span{font:400 11px/1 var(--f-mono);color:var(--ink2);text-align:right}
.bar{height:6px;background:var(--panel2);border:1px solid var(--line);position:relative}
.bar>i{position:absolute;inset:0 auto 0 0;background:var(--ink2)}
.bar>i.h{background:var(--sig)}
.bar.na{background:repeating-linear-gradient(135deg,var(--line) 0 4px,transparent 4px 8px)}
.q small{display:block;margin-top:6px;color:var(--ink3);font-size:11px}
.panel-error{padding:16px 14px;border-left:3px solid var(--bad);color:var(--bad);font-size:13px}
.panel-error b{display:block;margin-bottom:4px;color:var(--bad)}
.panel-error .cause{display:block;margin-top:6px;color:var(--ink2);font-size:12px;line-height:1.5}
.panel-error .cause code{margin-left:6px;padding:2px 4px;border:1px solid var(--line);background:var(--panel2);color:var(--ink3);font:400 11px/1 var(--f-mono);white-space:nowrap}
.empty,.loading{padding:16px 14px;font-size:13px;color:var(--ink2)}
.empty b,.loading b{display:block;margin-bottom:4px;color:var(--ink)}
.loading{color:var(--ink3)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:0}
.kv div{padding:10px 14px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.kv div:nth-child(4n){border-right:0}
.kv b{display:block;font:500 18px/1.2 var(--f-mono);margin-top:6px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;padding:14px}
.stat-grid .stat{border:1px solid var(--line);padding:10px 12px}
.stat-grid .stat b{display:block;font:500 18px/1.2 var(--f-mono);margin-top:6px}
.reasons{padding:4px 14px 12px}
.reasons .r{display:flex;gap:10px;justify-content:space-between;padding:7px 0;border-top:1px solid var(--line);font-size:12px;color:var(--ink2);overflow-wrap:anywhere}
.reasons .r:first-child{border-top:0}
.att-list{display:grid}
.att-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:12px;align-items:start;padding:12px 14px;border-bottom:1px solid var(--line)}
.att-row:last-child{border-bottom:0}
.att-title{font-size:14px;overflow-wrap:anywhere}
.att-meta{margin-top:4px;color:var(--ink3);font:400 11px/1.4 var(--f-mono);overflow-wrap:anywhere}
.att-detail{grid-column:2/-1;font-size:12px;color:var(--ink2);overflow-wrap:anywhere}
.att-detail[hidden]{display:none}
.att-view{display:inline-flex;align-items:center;justify-content:center;min-height:30px;padding:5px 10px;border:1px solid var(--line2);border-radius:var(--r);font:500 10px/1 var(--f-mono);color:var(--ink);text-decoration:none;background:var(--panel2)}
.att-body{min-width:0}
.changes-wrap{padding:0 0 6px}
.diag{padding:0 0 12px}
.diag>section{padding:0 14px}
.diag h4{font:600 12px/1 var(--f-ui);letter-spacing:.04em;text-transform:uppercase;color:var(--ink2);margin:14px 0 6px}
:lang(zh-CN) .diag h4{text-transform:none}
.diag-item{padding:12px 0;border-bottom:1px solid var(--line)}
.diag-line{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;align-items:center}
.diag-meta{color:var(--ink3);font:400 11px/1.5 var(--f-mono)}
.diag-button{padding:6px 10px;border:1px solid var(--line2);border-radius:var(--r);background:var(--panel2);color:var(--ink);font:500 11px/1 var(--f-mono)}
.diagnosis-output{margin-top:10px;padding:12px;background:var(--bg);border:1px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 var(--f-mono);color:var(--ink2)}
.diagnosis-output[hidden]{display:none}
.details{margin:0 14px 14px;border:1px solid var(--line)}
.details>summary{cursor:pointer;padding:10px 12px;font:500 11px/1 var(--f-mono);color:var(--ink2);letter-spacing:.06em}
.details .body{padding:0 12px 10px}
.mini{font:400 11px/1.5 var(--f-mono);color:var(--ink3)}
.footer{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;padding:14px 24px;border-top:1px solid var(--line);color:var(--ink3);font:400 11px/1.4 var(--f-mono);margin-top:8px}
.mobnav{display:none}
.m-only{display:none!important}
/* Global notice: absolutely positioned at its static position, i.e. directly
   below the header's rendered bottom edge, inside the content column. */
.toast{position:absolute;right:24px;margin-top:-12px;max-width:min(420px,calc(100% - 48px));display:flex;align-items:flex-start;gap:10px;padding:4px 4px 4px 14px;background:var(--panel);color:var(--bad);border:1px solid var(--bad);border-left-width:3px;z-index:10;font:500 11px/1.4 var(--f-mono)}
.toast[hidden]{display:none}
.toast p{flex:1;min-width:0;padding:8px 0;overflow-wrap:anywhere}
.toast-x{flex:none;color:var(--ink2);font-size:16px}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
/* login */
.login{display:grid;grid-template-columns:1.1fr 1fr;min-height:100vh}
.l-art{position:relative;border-right:1px solid var(--line);padding:40px 48px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;background:var(--panel)}
.l-art .brand{display:flex;align-items:center;gap:12px;font:600 15px/1 var(--f-ui)}
.l-art .brand .mark{margin:0}
.l-art h2{font:500 52px/1.02 var(--f-mono);letter-spacing:-.04em;max-width:640px}
:lang(zh-CN) .l-art h2{font:700 50px/1.18 var(--f-ui);letter-spacing:.02em}
.l-art h2 em{font-style:normal;color:var(--sig)}
.l-art p{color:var(--ink2);max-width:440px;margin-top:18px;font-size:15px}
.l-art svg.routes{position:absolute;right:24px;top:110px;width:520px;height:300px;opacity:.9}
.l-art>div{position:relative}
.l-stat{display:flex;gap:28px;font:400 11px/1.6 var(--f-mono);color:var(--ink3)}
.l-stat b{display:block;color:var(--ink);font-weight:500;font-size:13px}
.l-form{display:flex;flex-direction:column;justify-content:center;padding:40px 72px;position:relative}
.l-form .tools{position:absolute;top:24px;right:24px;display:flex;gap:8px}
.l-card{max-width:420px;width:100%}
.l-card h3{font:600 28px/1.2 var(--f-ui);margin:10px 0 8px}
.l-card .sub{color:var(--ink2);margin-bottom:26px;font-size:14.5px}
.gh{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;height:52px;background:var(--ink);color:var(--bg);font:600 15px/1 var(--f-ui);border-radius:var(--r);letter-spacing:.01em;position:relative;text-decoration:none}
.gh svg{width:20px;height:20px;fill:currentColor}
.gh .ar{position:absolute;right:16px;font:400 13px/1 var(--f-mono);opacity:.7}
.gh[aria-busy=true]{opacity:.75;cursor:progress}
.gh .spin{display:none;width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:sp 1s linear infinite}
.gh[aria-busy=true] .spin{display:block}
.gh[aria-busy=true] svg{display:none}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.gh .spin{animation:none}}
.only{margin-top:12px;font:400 11px/1.5 var(--f-mono);color:var(--ink3);display:flex;gap:8px;align-items:flex-start}
.only::before{content:"";width:6px;height:6px;background:var(--sig);flex:none;margin-top:5px}
.alert{display:none;border:1px solid var(--bad);border-left-width:3px;background:color-mix(in srgb,var(--bad) 8%,var(--panel));padding:12px 14px;margin-bottom:20px;font-size:13px}
.alert b{display:block;font-weight:600;margin-bottom:3px;color:var(--bad)}
.alert.warn{border-color:var(--warn);background:color-mix(in srgb,var(--warn) 9%,var(--panel))}
.alert.warn b{color:var(--warn)}
.alert.on{display:block}
.flow{margin-top:30px}
.flow ol{list-style:none}
.flow li{display:grid;grid-template-columns:28px 1fr;gap:2px 10px;padding:12px 14px;border-bottom:1px solid var(--line)}
.flow li:last-child{border-bottom:0}
.flow li .n{font:500 11px/1.5 var(--f-mono);color:var(--sig)}
.flow li b{font-weight:600;font-size:13.5px}
.flow li p{grid-column:2;color:var(--ink2);font-size:12.5px;line-height:1.5}
.legal{margin-top:22px;font-size:12px;color:var(--ink3);display:flex;gap:10px;align-items:flex-start}
@media (max-width:1100px){.grid2{grid-template-columns:1fr}.readouts{grid-template-columns:repeat(3,1fr)}.ro:nth-child(3n){border-right:0}.ro{border-bottom:1px solid var(--line)}}
/* Between the rail and the narrow layout the header content no longer fits on
   one row. Give the title its own row instead of squeezing the subtitle and the
   window buttons into two lines. */
@media (max-width:1080px){.top{gap:12px;height:auto;min-height:64px;flex-wrap:wrap;padding:12px 0}.top h1{flex:1 1 100%}.top .grow{display:none}}
/* The desktop rail removes 76px and the page adds 48px of padding, so the
   single-row header only fits from 1000px up; below that the title block is
   squeezed into the status chips. Drop the rail and wrap the header instead. */
@media (max-width:1000px){
  body{font-size:13px}
  .app{grid-template-columns:1fr}
  .rail{display:none}
  main{padding:0 14px 84px}
  .top{height:auto;padding:14px 0 12px;flex-wrap:wrap;gap:10px}
  .top h1{font-size:17px;flex:1 1 100%;display:flex;align-items:center;gap:10px}
  .m-only{display:grid!important}
  .top h1 .mark{display:grid;width:30px;height:30px;font-size:10px;margin:0}
  .top .grow,.top .hide-m{display:none}
  .seg button{padding:6px 8px}
  .toast{left:14px;right:14px;max-width:none}
  .readouts{grid-template-columns:1fr 1fr}
  .ro{border-bottom:1px solid var(--line);padding:12px}
  .ro:nth-child(2n){border-right:0}
  .ro:last-child{grid-column:1/-1;border-bottom:0}
  .ro .v{font-size:22px}
  .grid2{grid-template-columns:1fr}
  .chart svg{height:150px}
  .t-prov th:nth-child(3),.t-prov td:nth-child(3),.t-prov th:nth-child(5),.t-prov td:nth-child(5){display:none}
  td,th{padding:9px 10px}
  .tail div{grid-template-columns:58px 1fr 54px}
  .mobnav{display:flex;position:fixed;left:0;right:0;bottom:0;height:64px;background:var(--panel);border-top:1px solid var(--line);z-index:5}
  .mobnav a{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;color:var(--ink3);text-decoration:none;font-size:10px;border-top:2px solid transparent}
  .mobnav a.on,.mobnav a:focus-visible{color:var(--ink);border-top-color:var(--sig)}
  .mobnav svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.6}
  .login{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  .l-art{padding:22px 20px 24px;border-right:0;border-bottom:1px solid var(--line)}
  .l-art h2{font-size:30px;margin-top:30px}
  :lang(zh-CN) .l-art h2{font-size:28px}
  .l-art p{font-size:13px;margin-top:10px}
  .l-art svg.routes{width:300px;height:280px;right:-90px;top:-40px;opacity:.5}
  .l-stat{display:none}
  .l-form{padding:24px 20px;justify-content:flex-start}
  .l-form .tools{top:20px;right:16px}
  .l-card h3{font-size:21px;margin-top:4px}
  .l-card .sub{margin-bottom:18px;font-size:13.5px}
  .flow{margin-top:20px}
  .flow li{padding:10px 12px}
  .legal{margin-top:16px}
  .footer{padding:14px}
  .att-row{grid-template-columns:1fr auto}
  .att-detail{grid-column:1/-1}
}
`;
