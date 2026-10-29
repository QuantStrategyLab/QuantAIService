// Render only explicitly typed, bounded inline images; never fetch a material URL.
const invalid=()=>{throw Object.assign(new Error('invalid image material'),{name:'ValueError'});};
export function imageMaterials(materials=[]){
 const images=[];
 for(const item of materials){
  if(item?.type!=='image')continue;
  if(!['image/png','image/jpeg','image/webp'].includes(item.mime_type)||typeof item.base64!=='string'||!item.base64.length||item.base64.length>300000||item.base64.length%4||!/^[A-Za-z0-9+/]*={0,2}$/.test(item.base64))invalid();
  let bytes;try{bytes=Uint8Array.from(atob(item.base64),c=>c.charCodeAt(0));}catch{invalid();}
  const magic=Array.from(bytes.slice(0,12));
  const png=magic.slice(0,8).join(',')==='137,80,78,71,13,10,26,10';
  const jpeg=magic[0]===255&&magic[1]===216&&magic[2]===255;
  const webp=String.fromCharCode(...bytes.slice(0,4))==='RIFF'&&String.fromCharCode(...bytes.slice(8,12))==='WEBP';
  if(!(item.mime_type==='image/png'&&png||item.mime_type==='image/jpeg'&&jpeg||item.mime_type==='image/webp'&&webp))invalid();
  images.push({type:'image',mimeType:item.mime_type,data:item.base64});
  if(images.length>3)invalid();
 }
 return images;
}
