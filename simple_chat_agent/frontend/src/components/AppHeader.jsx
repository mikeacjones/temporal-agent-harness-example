import { ConversationList } from "./ConversationList.jsx";

export function AppHeader({
  state,
  adaptiveThinking,
  status,
  onNewChat,
  onOpenTools,
  onLogout,
  onUpdateAgentSettings,
  onUpdateThinkingBudget,
  onSelectConversation,
  onDeleteConversation,
}) {
  return (
    <header>
      <div className="header-left">
        <h1 aria-label="Simple Chat Agent"></h1>
        {state.user?.temporal_ui_workflows_url ? (
          <a
            className="temporal-link"
            href={state.user.temporal_ui_workflows_url}
            target="_blank"
            rel="noreferrer"
          >
            Temporal UI
          </a>
        ) : null}
      </div>
      <div className="side-panel">
        <section className="side-section">
          <div className="side-actions">
            <button className="primary" type="button" onClick={onNewChat}>
              New Chat
            </button>
            <button type="button" onClick={onOpenTools}>
              Tools
            </button>
            <button type="button" onClick={onLogout}>
              Logout
            </button>
          </div>
        </section>
        <section className="side-section">
          <p className="side-section-title">Agent</p>
          <div className="agent-settings">
            <div className="agent-field">
              <label htmlFor="modelSelect">Model</label>
              <select
                id="modelSelect"
                value={state.agentSettings.model || state.config?.default_model || ""}
                onChange={(event) =>
                  onUpdateAgentSettings({ model: event.currentTarget.value })
                }
              >
                {(state.config?.model_options || []).map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </div>
            <label className="agent-toggle" htmlFor="thinkingEnabled">
              <input
                id="thinkingEnabled"
                type="checkbox"
                checked={state.agentSettings.thinkingEnabled}
                onChange={(event) =>
                  onUpdateAgentSettings({
                    thinkingEnabled: event.currentTarget.checked,
                  })
                }
              />
              Extended thinking
            </label>
            <div
              className="agent-field"
              id="thinkingBudgetField"
              hidden={!state.agentSettings.thinkingEnabled || adaptiveThinking}
            >
              <label htmlFor="thinkingBudget">Budget tokens</label>
              <input
                id="thinkingBudget"
                type="number"
                min={state.config?.thinking?.min_budget_tokens || 1024}
                step="1024"
                value={state.agentSettings.thinkingBudgetTokens}
                onChange={(event) => onUpdateThinkingBudget(event.currentTarget.value)}
              />
            </div>
            <div
              className="agent-field"
              id="thinkingEffortField"
              hidden={!state.agentSettings.thinkingEnabled || !adaptiveThinking}
            >
              <label htmlFor="thinkingEffort">Effort</label>
              <select
                id="thinkingEffort"
                value={state.agentSettings.thinkingEffort}
                onChange={(event) =>
                  onUpdateAgentSettings({ thinkingEffort: event.currentTarget.value })
                }
              >
                {(state.config?.thinking?.effort_options || ["medium"]).map((effort) => (
                  <option key={effort} value={effort}>
                    {effort}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </section>
        <section className="side-section">
          <p className="side-section-title">Chats</p>
          <ConversationList
            conversations={state.conversations}
            currentWorkflowId={state.workflowId}
            draftConversation={state.draftConversation}
            onNewDraft={onNewChat}
            onSelect={onSelectConversation}
            onDelete={onDeleteConversation}
          />
        </section>
      </div>
      <div className="status">{status}</div>
    </header>
  );
}
