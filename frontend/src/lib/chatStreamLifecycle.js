export function createChatStreamLifecycle() {
  return { completed: false, handledTerminalEvent: false, persisted: null };
}

export function reduceChatStreamLifecycle(state, event) {
  if (event?.type === 'done') {
    return {
      completed: true,
      handledTerminalEvent: true,
      persisted: event.persisted !== false,
    };
  }
  if (event?.type === 'error') {
    return { ...state, handledTerminalEvent: true };
  }
  return state;
}
