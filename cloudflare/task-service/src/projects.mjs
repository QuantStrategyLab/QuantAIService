// Explicit worker project lists mirror the Python grant contract. Callers stay single-project.
const denied=()=>{throw Object.assign(new Error('project access denied'),{name:'PermissionError'});};
export function workerContext(env,identity,project){
  const home=env.PROJECT_CONTEXTS?.[identity.projectId];
  if(!home||home.organization!==identity.organization||home.ownerId!==identity.ownerId)denied();
  if(project===undefined||project===home.project)return {...home,projectId:identity.projectId};
  const client=env.AUTH_CLIENTS?.[identity.clientId];
  if(identity.provider===null||client?.provider!==identity.provider||!Array.isArray(client.projects)||!client.projects.includes(project))denied();
  const matches=Object.entries(env.PROJECT_CONTEXTS??{}).filter(([,c])=>c.project===project&&
    c.organization===identity.organization&&c.ownerId===identity.ownerId&&c.providers?.includes(identity.provider));
  if(matches.length!==1||!env.PROJECT_POLICIES?.[project])denied();
  return {...matches[0][1],projectId:matches[0][0]};
}
export function workerProjects(env,identity){
  const home=workerContext(env,identity).project;
  return [home,...new Set(env.AUTH_CLIENTS?.[identity.clientId]?.projects??[])].filter((p,i,a)=>a.indexOf(p)===i).filter(p=>{
    try{return workerContext(env,identity,p).project===p;}catch{return false;}
  });
}
