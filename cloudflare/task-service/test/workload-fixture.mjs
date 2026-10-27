// Auth/REST tests advance alarms explicitly so follow-up operations see a live task.
// probeTick still runs the production alarm; native.mjs covers natural scheduling.
export {default, TaskService} from './fixture.mjs';
import {PersonalFixtureStore as PersonalBase, QuantFixtureStore as QuantBase} from './fixture.mjs';
export class PersonalFixtureStore extends PersonalBase {
  async alarm() {}
  async probeTick() {return super.alarm();}
}
export class QuantFixtureStore extends QuantBase {
  async alarm() {}
  async probeTick() {return super.alarm();}
}
