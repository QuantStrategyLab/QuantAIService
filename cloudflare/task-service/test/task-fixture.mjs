import {TaskService as ProductionTaskService} from '../src/task-service.mjs';
export {PigbibiTaskStore as PersonalTaskStore} from '../src/state.mjs';
export class TaskService extends ProductionTaskService {}
export default {fetch(){return new Response(null,{status:404});}};
