import {configuration,createAuthApplication} from './auth.mjs';
export {TaskService} from './task-service.mjs';
export {QuantTaskStore as PersonalTaskStore} from './state.mjs';
// The application closes over server-controlled configuration only; all env reads
// (clients, admissions, GitHub login) still happen per request inside its handlers.
// Rebuild only when the configuration inputs themselves change.
let application;
export default {
  fetch(request,env,ctx){
    const key=`${env.ORGANIZATION}\u0000${env.PUBLIC_ORIGIN}\u0000${env.PROJECT_ID}`;
    if(!application||application.key!==key)application={key,app:createAuthApplication(configuration(env))};
    return application.app.fetch(request,env,ctx);
  }
};
