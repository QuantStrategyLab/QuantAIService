/** Applied in <head> before first paint so the selected language and theme
  do not flash the server default. No data is read here. */

export const BOOT_SCRIPT = `(function(){try{var lang=localStorage.getItem("qg-lang");if(lang!=="zh-CN"&&lang!=="en"){lang=(navigator.language||"en").toLowerCase().indexOf("zh")===0?"zh-CN":"en";}document.documentElement.lang=lang;var theme=localStorage.getItem("qg-theme");if(theme!=="light"&&theme!=="dark"){theme=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";}document.documentElement.setAttribute("data-theme",theme);}catch(e){}})();`;
