// Preserve Python _json's distinction between integer 1 and float 1.0.
// This is a JSON parser/canonical serializer, not a JSON Schema validator.
const invalid = () => { throw Object.assign(new Error('unsupported or invalid JSON representation'), {name:'ValueError'}); };
const cmp = (a,b) => { const x=Array.from(a), y=Array.from(b); for(let i=0;i<Math.min(x.length,y.length);i++){const d=x[i].codePointAt(0)-y[i].codePointAt(0);if(d)return d;}return x.length-y.length; };
export function floatCanonical(raw) {
  const n=Number(raw); if(!Number.isFinite(n))invalid();
  if(n===0)return raw.startsWith('-')?'-0.0':'0.0';
  const [mant,expString]=Math.abs(n).toExponential().split('e');
  const digits=mant.replace('.',''), exp=Number(expString);
  // Explicit supported intersection: no precision beyond 15 significant digits,
  // no integers represented imprecisely. Wider numeric inputs fail before storage.
  if(digits.length>15 || (Number.isInteger(n)&&!Number.isSafeInteger(n)))invalid();
  let s;
  if(exp < -4 || exp>=16)s=digits[0]+(digits.length>1?'.'+digits.slice(1):'')+'e'+(exp<0?'-':'+')+String(Math.abs(exp)).padStart(2,'0');
  else if(exp<0)s='0.'+'0'.repeat(-exp-1)+digits;
  else if(exp+1>=digits.length)s=digits+'0'.repeat(exp+1-digits.length)+'.0';
  else s=digits.slice(0,exp+1)+'.'+digits.slice(exp+1);
  return (n<0?'-':'')+s;
}
export function parseJson(text) {
  if(typeof text!=='string'||new TextEncoder().encode(text).length>2000000)invalid();
  let i=0,depth=0; const ws=()=>{while(/[\x20\x09\x0a\x0d]/.test(text[i]??'x'))i++;};
  const parse=()=>{ws();if(++depth>100)invalid(); let node;const c=text[i];
    if(c==='{'||c==='['){const object=c==='{',end=object?'}':']';i++;const entries=[];const seen=new Set();ws();if(text[i]!==end)while(true){let k;if(object){const str=parse();if(str.kind!=='scalar'||typeof str.value!=='string')invalid();k=str.value;if(seen.has(k))invalid();seen.add(k);ws();if(text[i++]!==':')invalid();}entries.push(object?[k,parse()]:parse());ws();if(text[i]===end)break;if(text[i++]!==',')invalid();}if(text[i++]!==end)invalid();node={kind:object?'object':'array',entries};}
    else if(c==='"'){const start=i++;while(i<text.length){if(text[i]==='\\'){i+=2;continue;}if(text[i++]==='"')break;}let v;try{v=JSON.parse(text.slice(start,i));}catch{invalid();}if(typeof v!=='string')invalid();for(const ch of v){const cp=ch.codePointAt(0);if(cp>=0xd800&&cp<=0xdfff)invalid();}node={kind:'scalar',value:v,raw:JSON.stringify(v)};}
    else {const match=text.slice(i).match(/^(?:-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)/);if(!match)invalid();const raw=match[0];i+=raw.length;let value;try{value=JSON.parse(raw);}catch{invalid();}let canonical=raw;if(typeof value==='number'){if(/[.eE]/.test(raw))canonical=floatCanonical(raw);else{if(!Number.isSafeInteger(value))invalid();canonical=String(value);}}node={kind:'scalar',value,raw:canonical};}
    depth--;return node;};
  const node=parse();ws();if(i!==text.length)invalid();return node;
}
export const serialize = node => node.kind==='scalar'?node.raw:node.kind==='array'?'['+node.entries.map(serialize).join(',')+']':'{'+[...node.entries].sort((a,b)=>cmp(a[0],b[0])).map(([k,v])=>JSON.stringify(k)+':'+serialize(v)).join(',')+'}';
export const valueOf = node => JSON.parse(serialize(node));
export const field = (node,key) => node.kind==='object'?node.entries.find(([k])=>k===key)?.[1]:undefined;
export const nodeFrom = value => parseJson(JSON.stringify(value));
export const objectNode = entries => ({kind:'object',entries});
export const replaceFields = (node, replacements) => objectNode([...node.entries.filter(([k])=>!Object.hasOwn(replacements,k)), ...Object.entries(replacements)]);
