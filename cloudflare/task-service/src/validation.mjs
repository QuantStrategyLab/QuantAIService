import {removeUriSchemePlugin} from '@hyperjump/browser';
import {registerSchema, unregisterSchema, validate} from '@hyperjump/json-schema/draft-2020-12';
// All supported meta documents are bundled. Never retrieve schemas from the network.
removeUriSchemePlugin('http');removeUriSchemePlugin('https');
const DIALECT='https://json-schema.org/draft/2020-12/schema';
const invalid=()=>{throw Object.assign(new Error('invalid schema or arguments'),{name:'ValidationError'});};
// Same no-external-reference rule as Python _check_schema, including dynamic refs.
function localOnly(value){
  if(!value||typeof value!=='object')return;
  for(const [k,v] of Object.entries(value)){
    if(['$ref','$dynamicRef'].includes(k)&&(typeof v!=='string'||!v.startsWith('#')))invalid();
    // Unknown dialects require a remote loader and are outside this explicit D2020 service.
    if(k==='$schema'&&v!==DIALECT)invalid();
    localOnly(v);
  }
}
export async function checkOutputSchema(schema){
  if(!schema||typeof schema!=='object'||Array.isArray(schema))invalid();
  localOnly(schema);
  try{if(!(await validate(DIALECT,schema)).valid)invalid();await compile(schema);}catch{invalid();}
}
async function compile(schema){
  const uri='https://local-schema.invalid/'+crypto.randomUUID();
  try{registerSchema(schema,uri,DIALECT);return await validate(uri);}finally{unregisterSchema(uri);}
}
export async function check(schema,value){
  try{const validator=await compile(schema);if(!validator(value).valid)invalid();}catch{invalid();}
}
export async function outputValid(schema,value){
  try{const validator=await compile(schema);return validator(value).valid;}catch{invalid();}
}
