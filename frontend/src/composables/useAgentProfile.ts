import { listAgentProfiles } from '../api/agentProfile'
import { createAgentProfileDirectory } from '../utils/agentProfileDirectory'
import { eventBus } from '../utils/eventBus'

const directory = createAgentProfileDirectory(listAgentProfiles, localStorage)
eventBus.on('auth:cleared', directory.clear)

export function useAgentProfile() {
  return directory
}
