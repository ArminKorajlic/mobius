/* `/new` and `/clear`: start a chat beside the one they were typed in. */

/**
 * Give a just-created chat another chat's provider and model, the way the
 * model picker saves them, before anything is sent in it.
 *
 * Resolves true when there was nothing to copy or the copy was saved
 * (`onSaved` receives the server's reply, so a cached first paint can show
 * the copied model). Resolves false after telling the owner when it failed:
 * callers then leave a first message unsent instead of sending it on a model
 * the owner did not choose.
 */
export async function copyChatSettings({ updateChat, onSaved, notify, recordError }, chatId, settings) {
  if (!settings) return true
  try {
    const response = await updateChat(chatId, {
      ...(settings.provider ? { provider: settings.provider } : {}),
      agent_settings_json: settings.agentSettings,
    })
    if (response.ok) {
      const saved = await response.json().catch(() => null)
      if (saved) onSaved?.(chatId, saved)
      return true
    }
  } catch (error) {
    recordError?.('new-chat-copy-settings', error)
  }
  notify("The new chat couldn't take this chat's model. Check the model, then send.", {
    variant: 'error',
  })
  return false
}

/**
 * Open a fresh chat in the source chat's project, on its provider and model,
 * and send `firstMessage` there when one was typed.
 *
 * Creation and opening are New Chat's own paths (`createProjectChat` for a
 * project chat, `newChat` otherwise); this only chooses between them, so a
 * project chat never starts outside its project and the source chat is never
 * touched. Resolves true once the new chat exists.
 */
export async function startChatFromCommand(
  { findProject, createProjectChat, newChat, notify, recordError },
  { projectId = null, settings = null, firstMessage = '' } = {},
) {
  if (projectId != null) {
    const project = findProject(projectId)
    if (!project) {
      notify("Couldn't find this chat's project, so no chat was started.", { variant: 'error' })
      return false
    }
    try {
      return !!(await createProjectChat(project, {
        prompt: firstMessage || null,
        autoSend: true,
        settings,
      }))
    } catch (error) {
      recordError?.('new-chat-command', error)
      notify("Couldn't start a new chat — please try again.", { variant: 'error' })
      return false
    }
  }
  const chatId = await newChat({
    draft: firstMessage,
    autoSend: true,
    recordHistory: true,
    settings,
  })
  return chatId != null
}
