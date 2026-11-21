// Compatibility shim delegating to platform-specific agent wake adapters.
export {validateAgentTrigger as nativeConfiguration, wakeAgent as triggerNative} from './agent-adapters.mjs';
