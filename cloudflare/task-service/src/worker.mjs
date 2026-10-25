import {configuration,createAuthApplication} from './auth.mjs';
export {TaskService} from './task-service.mjs';
export {QuantTaskStore as PersonalTaskStore} from './state.mjs';
export default {
  fetch(request,env,ctx){return createAuthApplication(configuration(env)).fetch(request,env,ctx);}
};
