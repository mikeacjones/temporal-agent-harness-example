import React, {
  Children,
  isValidElement,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const defaultAgentSettings = {
  model: "",
  thinkingEnabled: false,
  thinkingBudgetTokens: 4096,
  thinkingEffort: "medium",
};

const defaultMcpFormValues = {
  label: "",
  server_url: "",
  tool_prefix: "",
  auth_mode: "none",
  bearer_token: "",
};

const emptyArtifactViewer = {
  open: false,
  artifact: null,
  loading: false,
  error: "",
  text: "",
};

const initialState = {
  auth: "loading",
  user: null,
  loginConfigured: true,
  loginSubtitle: "",
  loginError: "",
  loggingIn: false,
  config: null,
  agentSettings: defaultAgentSettings,
  conversations: [],
  tools: [],
  workflowId: null,
  runId: null,
  temporalUiUrl: null,
  workflowState: null,
  workflowStateProjectionRevision: 0,
  workflowTranscriptProjectionRevision: 0,
  streamTurn: null,
  streamPanelCollapsed: false,
  currentClaudeSequence: null,
  ignoreClaudeUntilStart: false,
  localPending: [],
  resolvingApprovals: new Set(),
  recoveringMissingWorkflow: false,
  toolsWindowOpen: false,
  artifactViewer: emptyArtifactViewer,
  draftConversation: true,
  mcpFormOpen: false,
  mcpFormSubmitting: false,
  mcpFormError: "",
  mcpFormValues: defaultMcpFormValues,
  statusNotice: "",
};

export default function App() {
  const [state, setState] = useState(initialState);
  const [message, setMessage] = useState("");
  const stateRef = useRef(state);
  const messageRef = useRef(message);
  const eventSourceRef = useRef(null);
  const messagesRef = useRef(null);
  const pinnedToBottomRef = useRef(true);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    messageRef.current = message;
  }, [message]);

  useEffect(() => {
    const run = { cancelled: false };
    boot(run);
    return () => {
      run.cancelled = true;
      closeEventSource();
    };
  }, []);

  useLayoutEffect(() => {
    const messages = messagesRef.current;
    if (!messages) return;
    if (pinnedToBottomRef.current) {
      messages.scrollTop = messages.scrollHeight;
    }
    messages
      .querySelectorAll(
        ".stream-current-turn .stream-text, .stream-current-turn .stream-thinking",
      )
      .forEach((node) => {
        node.scrollTop = node.scrollHeight;
      });
  }, [state.workflowState, state.localPending, state.streamTurn, state.draftConversation]);

  async function boot(run) {
    try {
      const user = await fetchCurrentUser();
      if (!user || run.cancelled) {
        showLogin();
        return;
      }

      const [config, tools, conversations] = await Promise.all([
        loadConfigData(),
        loadToolsData(),
        loadConversationsData(),
      ]);
      if (run.cancelled) return;

      const agentSettings = agentSettingsFromConfig(config);
      setState((previous) => ({
        ...previous,
        auth: "app",
        user,
        config,
        tools,
        conversations,
        agentSettings,
        statusNotice: "",
      }));

      const savedWorkflowId = localStorage.getItem("simpleChatWorkflowId");
      const savedConversation = conversations.find(
        (conversation) => conversation.workflow_id === savedWorkflowId,
      );
      const conversation = savedConversation || conversations[0];
      if (conversation) {
        selectConversation(conversation.workflow_id, {}, conversations);
      } else {
        startDraftConversation();
      }
      showOAuthCallbackStatus();
    } catch (error) {
      setStatusNotice(`failed: ${error}`);
    }
  }

  async function fetchCurrentUser() {
    const response = await fetch("/api/me");
    if (response.status === 401) return null;
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  function closeEventSource() {
    if (!eventSourceRef.current) return;
    eventSourceRef.current.close();
    eventSourceRef.current = null;
  }

  function showLogin() {
    closeEventSource();
    const params = new URLSearchParams(window.location.search);
    const loginError = params.has("oauth_error") ? params.get("oauth_error") || "" : "";
    if (params.has("oauth_error")) history.replaceState({}, "", "/");
    setState((previous) => ({
      ...previous,
      auth: "login",
      loginError,
      statusNotice: "",
    }));
    configureLoginButton();
  }

  async function configureLoginButton() {
    try {
      const response = await fetch("/api/auth/google/configured");
      if (!response.ok) throw new Error(await response.text());
      const body = await response.json();
      setState((previous) => ({
        ...previous,
        loginConfigured: Boolean(body.configured),
        loginSubtitle: body.configured
          ? ""
          : "Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.",
      }));
    } catch (error) {
      setState((previous) => ({
        ...previous,
        loginConfigured: false,
        loginSubtitle: `Could not check auth config: ${error}`,
      }));
    }
  }

  async function loadConversationsData() {
    const response = await fetch("/api/conversations");
    if (response.status === 401) {
      showLogin();
      return [];
    }
    if (!response.ok) throw new Error(await response.text());
    const body = await response.json();
    return body.conversations || [];
  }

  async function loadToolsData() {
    const response = await fetch("/api/tools");
    if (response.status === 401) {
      showLogin();
      return [];
    }
    if (!response.ok) throw new Error(await response.text());
    const body = await response.json();
    return body.tools || [];
  }

  async function loadConfigData() {
    const response = await fetch("/api/config");
    if (response.status === 401) {
      showLogin();
      return {};
    }
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async function refreshTools() {
    const tools = await loadToolsData();
    setState((previous) => ({ ...previous, tools }));
  }

  async function refreshConversations() {
    const conversations = await loadConversationsData();
    setState((previous) => ({ ...previous, conversations }));
    return conversations;
  }

  function startDraftConversation() {
    closeEventSource();
    localStorage.removeItem("simpleChatWorkflowId");
    setState((previous) => ({
      ...previous,
      workflowId: null,
      runId: null,
      temporalUiUrl: null,
      workflowState: null,
      workflowStateProjectionRevision: 0,
      workflowTranscriptProjectionRevision: 0,
      streamTurn: null,
      streamPanelCollapsed: false,
      currentClaudeSequence: null,
      ignoreClaudeUntilStart: false,
      localPending: [],
      resolvingApprovals: new Set(),
      draftConversation: true,
      artifactViewer: emptyArtifactViewer,
      statusNotice: "",
    }));
  }

  function selectConversation(workflowId, options = {}, conversationsArg = null) {
    const conversations = conversationsArg || stateRef.current.conversations;
    const conversation = conversations.find((item) => item.workflow_id === workflowId);
    if (!conversation) return;
    closeEventSource();
    localStorage.setItem("simpleChatWorkflowId", conversation.workflow_id);
    setState((previous) => ({
      ...previous,
      conversations,
      workflowId: conversation.workflow_id,
      runId: conversation.run_id,
      temporalUiUrl: temporalUiUrl(conversation),
      workflowState: null,
      workflowStateProjectionRevision: 0,
      workflowTranscriptProjectionRevision: 0,
      streamTurn: null,
      streamPanelCollapsed: false,
      currentClaudeSequence: null,
      ignoreClaudeUntilStart: false,
      draftConversation: false,
      resolvingApprovals: new Set(),
      localPending: options.preserveLocalPending ? previous.localPending : [],
      artifactViewer: emptyArtifactViewer,
      statusNotice: "",
    }));
    connectEvents(conversation.workflow_id);
  }

  function connectEvents(workflowId) {
    if (!workflowId) return;
    closeEventSource();
    const eventSource = new EventSource(`/api/sessions/${workflowId}/events`);
    eventSourceRef.current = eventSource;
    eventSource.addEventListener("state", (event) => {
      const nextState = JSON.parse(event.data);
      setState((previous) => updateWorkflowStateInState(previous, nextState));
    });
    eventSource.addEventListener("stream", (event) => {
      const streamEvent = JSON.parse(event.data);
      setState((previous) => handleStreamEventInState(previous, streamEvent));
    });
    eventSource.addEventListener("missing", () => {
      handleMissingWorkflow();
    });
    eventSource.addEventListener("error", () => {
      setStatusNotice("event stream reconnecting...");
    });
  }

  async function createConversation(initialMessage = null, options = {}) {
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        ...newConversationRequest(),
        initial_message: initialMessage,
      }),
    });
    if (response.status === 401) {
      showLogin();
      return null;
    }
    if (!response.ok) throw new Error(await responseErrorText(response));
    const body = await response.json();
    const conversations = await refreshConversations();
    selectConversation(body.workflow_id, options, conversations);
    return body;
  }

  function newConversationRequest() {
    const current = stateRef.current;
    const config = current.config || {};
    const minBudget = config.thinking?.min_budget_tokens || 1024;
    const budgetTokens = Math.max(
      minBudget,
      Number(
        current.agentSettings.thinkingBudgetTokens ||
          config.thinking?.budget_tokens ||
          4096,
      ),
    );
    const agentSettings = {
      model: current.agentSettings.model || config.default_model || "",
      thinkingEnabled: Boolean(current.agentSettings.thinkingEnabled),
      thinkingBudgetTokens: budgetTokens,
      thinkingEffort: current.agentSettings.thinkingEffort || config.thinking?.effort || "medium",
    };
    saveAgentSettings(agentSettings);
    setState((previous) => ({ ...previous, agentSettings }));
    return {
      model: agentSettings.model,
      thinking: {
        enabled: agentSettings.thinkingEnabled,
        budget_tokens: budgetTokens,
        effort: agentSettings.thinkingEffort,
      },
    };
  }

  async function sendDefault() {
    const busy = stateRef.current.workflowState?.status === "responding";
    await sendAction(busy ? "steer" : "chat", busy ? "you steering" : "you", "sending");
  }

  async function sendAction(action, label, phase) {
    let content = messageRef.current.trim();
    if (!content && action === "interrupt") {
      content = "Stop the current response.";
    }
    if (!content) return;

    const current = stateRef.current;
    if (!current.workflowId) {
      if (action === "interrupt" || action === "steer" || action === "after-tool") return;
      setMessage("");
      const pending = createPendingMessage(label, content, phase, current);
      setState((previous) => ({
        ...previous,
        localPending: [...previous.localPending, pending],
      }));
      try {
        await createConversation(content, { preserveLocalPending: true });
        markPendingDelivered(pending.id);
      } catch (error) {
        markPendingFailed(pending.id, error);
      }
      return;
    }

    setMessage("");
    const pending = createPendingMessage(label, content, phase, current);
    setState((previous) => {
      let next = {
        ...previous,
        localPending: [...previous.localPending, pending],
      };
      if (action === "interrupt") {
        next = markStreamInterruptedInState(next);
        next.ignoreClaudeUntilStart = true;
      }
      return next;
    });

    try {
      if (action === "chat") {
        await post(`/api/sessions/${current.workflowId}/chat`, { message: content });
      } else if (action === "steer") {
        await post(`/api/sessions/${current.workflowId}/steer`, {
          message: content,
          mode: "immediate",
        });
      } else if (action === "after-tool") {
        await post(`/api/sessions/${current.workflowId}/steer`, {
          message: content,
          mode: "after_next_tool_result",
        });
      } else if (action === "interrupt") {
        await post(`/api/sessions/${current.workflowId}/interrupt`, { message: content });
      }
      markPendingDelivered(pending.id);
      await refreshConversations();
    } catch (error) {
      markPendingFailed(pending.id, error);
    }
  }

  function markPendingDelivered(pendingId) {
    setState((previous) => ({
      ...previous,
      localPending: previous.localPending.map((pending) =>
        pending.id === pendingId ? { ...pending, phase: "delivered" } : pending,
      ),
    }));
  }

  function markPendingFailed(pendingId, error) {
    setState((previous) => ({
      ...previous,
      localPending: previous.localPending.map((pending) =>
        pending.id === pendingId ? { ...pending, phase: `failed: ${error}` } : pending,
      ),
    }));
  }

  async function post(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(payload),
    });
    if (response.status === 401) {
      showLogin();
    }
    if (response.status === 404) {
      await handleMissingWorkflow();
    }
    if (!response.ok) throw new Error(await responseErrorText(response));
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) return response.json();
    return {};
  }

  async function handleMissingWorkflow() {
    const current = stateRef.current;
    if (current.recoveringMissingWorkflow) return;
    const missingWorkflowId = current.workflowId;
    setState((previous) => ({ ...previous, recoveringMissingWorkflow: true }));
    try {
      closeEventSource();
      if (missingWorkflowId) {
        setStatusNotice("Workflow no longer exists; selecting a live chat...");
        if (localStorage.getItem("simpleChatWorkflowId") === missingWorkflowId) {
          localStorage.removeItem("simpleChatWorkflowId");
        }
      }
      const conversations = await refreshConversations();
      const nextConversation = conversations[0];
      if (nextConversation) {
        selectConversation(nextConversation.workflow_id, {}, conversations);
      } else {
        startDraftConversation();
      }
    } finally {
      setState((previous) => ({ ...previous, recoveringMissingWorkflow: false }));
    }
  }

  async function deleteConversation(workflowId) {
    if (!confirm("Delete this chat?")) return;
    const deletingCurrent = stateRef.current.workflowId === workflowId;
    const response = await fetch(`/api/sessions/${workflowId}`, { method: "DELETE" });
    if (response.status === 401) {
      showLogin();
      return;
    }
    if (!response.ok) throw new Error(await responseErrorText(response));

    if (deletingCurrent) {
      closeEventSource();
      if (localStorage.getItem("simpleChatWorkflowId") === workflowId) {
        localStorage.removeItem("simpleChatWorkflowId");
      }
      setState((previous) => ({
        ...previous,
        workflowId: null,
        workflowState: null,
        workflowStateProjectionRevision: 0,
        workflowTranscriptProjectionRevision: 0,
        streamTurn: null,
        localPending: [],
        artifactViewer: emptyArtifactViewer,
      }));
    }

    const conversations = await refreshConversations();
    if (!deletingCurrent) return;
    const nextConversation = conversations[0];
    if (nextConversation) {
      selectConversation(nextConversation.workflow_id, {}, conversations);
    } else {
      startDraftConversation();
    }
  }

  async function resolveApproval(approvalId, decision) {
    if (stateRef.current.resolvingApprovals.has(approvalId)) return;
    setState((previous) => ({
      ...previous,
      resolvingApprovals: new Set([...previous.resolvingApprovals, approvalId]),
    }));
    try {
      await post(`/api/sessions/${stateRef.current.workflowId}/approvals/${approvalId}`, {
        decision,
      });
    } catch (error) {
      setState((previous) => {
        const resolvingApprovals = new Set(previous.resolvingApprovals);
        resolvingApprovals.delete(approvalId);
        return {
          ...previous,
          resolvingApprovals,
          statusNotice: `approval failed: ${error}`,
        };
      });
    }
  }

  async function openArtifactViewer(artifact) {
    setState((previous) => ({
      ...previous,
      artifactViewer: {
        open: true,
        artifact,
        loading: true,
        error: "",
        text: "",
      },
    }));

    if (isImageArtifact(artifact) || isPdfArtifact(artifact)) {
      setState((previous) => ({
        ...previous,
        artifactViewer: {
          ...previous.artifactViewer,
          loading: false,
        },
      }));
      return;
    }

    try {
      const response = await fetch(artifact.view_url);
      if (!response.ok) throw new Error(await responseErrorText(response));
      const text = await response.text();
      setState((previous) => ({
        ...previous,
        artifactViewer: {
          ...previous.artifactViewer,
          loading: false,
          text,
        },
      }));
    } catch (error) {
      setState((previous) => ({
        ...previous,
        artifactViewer: {
          ...previous.artifactViewer,
          loading: false,
          error: String(error),
        },
      }));
    }
  }

  function closeArtifactViewer() {
    setState((previous) => ({
      ...previous,
      artifactViewer: emptyArtifactViewer,
    }));
  }

  async function setMcpServerEnabled(tool, enabled) {
    const serverId = tool.provider.slice("mcp:".length);
    await post(`/api/mcp-servers/${encodeURIComponent(serverId)}/enabled`, { enabled });
    setStatusNotice(`${tool.label} ${enabled ? "enabled" : "disabled"}`);
    await refreshTools();
  }

  async function deleteMcpServer(tool) {
    if (!confirm(`Delete ${tool.label}?`)) return;
    const serverId = tool.provider.slice("mcp:".length);
    const response = await fetch(`/api/mcp-servers/${encodeURIComponent(serverId)}`, {
      method: "DELETE",
    });
    if (response.status === 401) {
      showLogin();
      return;
    }
    if (!response.ok) throw new Error(await responseErrorText(response));
    setStatusNotice(`${tool.label} deleted`);
    await refreshTools();
  }

  async function addHttpMcpServer(event) {
    event.preventDefault();
    const values = stateRef.current.mcpFormValues;
    const label = values.label.trim();
    const serverUrl = values.server_url.trim();
    const toolPrefix = values.tool_prefix.trim();
    const authMode = values.auth_mode || "none";
    const bearerToken = values.bearer_token.trim();

    if (authMode === "oauth") {
      window.location.href = mcpOAuthStartUrl({
        label,
        serverUrl,
        toolPrefix,
      });
      return;
    }

    setState((previous) => ({
      ...previous,
      mcpFormSubmitting: true,
      mcpFormError: "",
    }));
    try {
      const body = await post("/api/mcp-servers", {
        label,
        server_url: serverUrl,
        tool_prefix: toolPrefix,
        auth_mode: authMode,
        bearer_token: authMode === "bearer" ? bearerToken : null,
      });
      setState((previous) => ({
        ...previous,
        mcpFormOpen: false,
        mcpFormSubmitting: false,
        mcpFormError: "",
        mcpFormValues: defaultMcpFormValues,
      }));
      setStatusNotice(`Added MCP server: ${body.server?.label || label}`);
      await refreshTools();
    } catch (error) {
      setState((previous) => ({
        ...previous,
        mcpFormSubmitting: false,
        mcpFormError: String(error),
      }));
    }
  }

  function updateAgentSettings(patch) {
    setState((previous) => {
      const agentSettings = { ...previous.agentSettings, ...patch };
      saveAgentSettings(agentSettings);
      return { ...previous, agentSettings };
    });
  }

  function updateThinkingBudget(value) {
    const minBudget = stateRef.current.config?.thinking?.min_budget_tokens || 1024;
    updateAgentSettings({
      thinkingBudgetTokens: Math.max(minBudget, Number(value || minBudget)),
    });
  }

  function updateMcpFormValues(patch) {
    setState((previous) => ({
      ...previous,
      mcpFormValues: {
        ...previous.mcpFormValues,
        ...patch,
      },
    }));
  }

  function setStatusNotice(statusNotice) {
    setState((previous) => ({ ...previous, statusNotice }));
  }

  function showOAuthCallbackStatus() {
    const params = new URLSearchParams(window.location.search);
    if (params.has("oauth_error")) {
      setStatusNotice(`OAuth failed: ${params.get("oauth_error")}`);
    } else if (params.has("github")) {
      setStatusNotice("GitHub connected");
    } else if (params.has("mcp")) {
      setStatusNotice("MCP server connected");
      refreshTools().catch((error) => {
        setStatusNotice(`tool refresh failed: ${error}`);
      });
    }
    if (params.has("oauth_error") || params.has("github") || params.has("mcp")) {
      history.replaceState({}, "", "/");
    }
  }

  function handleLoginClick(event) {
    event.preventDefault();
    if (!state.loginConfigured) return;
    const href = event.currentTarget.getAttribute("href");
    setState((previous) => ({ ...previous, loggingIn: true }));
    setTimeout(() => {
      window.location.href = href;
    }, 750);
  }

  const adaptiveThinking = selectedModelUsesAdaptiveThinking(state);
  const status = displayStatus(state);
  const artifacts = state.workflowState?.artifacts || [];

  return (
    <>
      <LoginScreen
        hidden={state.auth !== "login"}
        loggingIn={state.loggingIn}
        configured={state.loginConfigured}
        subtitle={state.loginSubtitle}
        error={state.loginError}
        onLoginClick={handleLoginClick}
      />
      <div className="app" hidden={state.auth !== "app"}>
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
                <button className="primary" type="button" onClick={startDraftConversation}>
                  New Chat
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setState((previous) => ({ ...previous, toolsWindowOpen: true }))
                  }
                >
                  Tools
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    await post("/api/logout", {});
                    localStorage.removeItem("simpleChatWorkflowId");
                    closeEventSource();
                    setState({
                      ...initialState,
                      auth: "login",
                    });
                    configureLoginButton();
                  }}
                >
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
                      updateAgentSettings({ model: event.currentTarget.value })
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
                      updateAgentSettings({
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
                    onChange={(event) => updateThinkingBudget(event.currentTarget.value)}
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
                      updateAgentSettings({ thinkingEffort: event.currentTarget.value })
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
                onNewDraft={startDraftConversation}
                onSelect={selectConversation}
                onDelete={(workflowId) => {
                  deleteConversation(workflowId).catch((error) => {
                    setStatusNotice(`delete failed: ${error}`);
                  });
                }}
              />
            </section>
          </div>
          <div className="status">{status}</div>
        </header>
        <main>
          <section
            className="messages"
            ref={messagesRef}
            onScroll={(event) => {
              const node = event.currentTarget;
              pinnedToBottomRef.current =
                node.scrollHeight - node.scrollTop - node.clientHeight < 80;
            }}
          >
            <Messages
              workflowState={state.workflowState}
              draftConversation={state.draftConversation}
              localPending={state.localPending}
              streamTurn={state.streamTurn}
              streamPanelCollapsed={state.streamPanelCollapsed}
              resolvingApprovals={state.resolvingApprovals}
              onToggleStreamPanel={() =>
                setState((previous) => ({
                  ...previous,
                  streamPanelCollapsed: !previous.streamPanelCollapsed,
                }))
              }
              onResolveApproval={resolveApproval}
            />
          </section>
          <aside className="sidebar">
            <p className="events-title">Sideband Stream</p>
            <div></div>
          </aside>
        </main>
        <ArtifactsPanel artifacts={artifacts} onOpen={openArtifactViewer} />
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            sendDefault();
          }}
        >
          <textarea
            value={message}
            onChange={(event) => setMessage(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendDefault();
              }
            }}
            placeholder="Type to chat. While responding, Send becomes steering."
          ></textarea>
          <button className="primary" type="submit">
            Send
          </button>
          {state.temporalUiUrl ? (
            <a
              className="temporal-link"
              href={state.temporalUiUrl}
              target="_blank"
              rel="noreferrer"
            >
              Workflow
            </a>
          ) : null}
          <button
            type="button"
            onClick={() => sendAction("interrupt", "you interrupt", "sending")}
          >
            Interrupt
          </button>
        </form>
        <ToolsWindow
          open={state.toolsWindowOpen}
          tools={state.tools}
          mcpFormOpen={state.mcpFormOpen}
          mcpFormSubmitting={state.mcpFormSubmitting}
          mcpFormError={state.mcpFormError}
          mcpFormValues={state.mcpFormValues}
          onClose={() =>
            setState((previous) => ({
              ...previous,
              toolsWindowOpen: false,
              mcpFormOpen: false,
              mcpFormError: "",
            }))
          }
          onOpenMcpForm={() =>
            setState((previous) => ({
              ...previous,
              mcpFormOpen: true,
              mcpFormError: "",
            }))
          }
          onCancelMcpForm={() =>
            setState((previous) => ({
              ...previous,
              mcpFormOpen: false,
              mcpFormError: "",
              mcpFormValues: defaultMcpFormValues,
            }))
          }
          onUpdateMcpForm={updateMcpFormValues}
          onSubmitMcpForm={addHttpMcpServer}
          onRefreshTools={refreshTools}
          onSetMcpEnabled={setMcpServerEnabled}
          onDeleteMcp={deleteMcpServer}
          setStatusNotice={setStatusNotice}
          post={post}
        />
        <ArtifactViewer
          viewer={state.artifactViewer}
          onClose={closeArtifactViewer}
        />
      </div>
    </>
  );
}

function LoginScreen({ hidden, loggingIn, configured, subtitle, error, onLoginClick }) {
  return (
    <section className="login-screen" hidden={hidden}>
      <div className={`login-card${loggingIn ? " logging-in" : ""}`}>
        <h1 aria-label="Simple Chat Agent"></h1>
        <p className="login-subtitle">{subtitle}</p>
        <div className="login-form">
          <a
            className="login-google"
            href="/oauth/google/start"
            aria-disabled={configured ? undefined : "true"}
            onClick={onLoginClick}
          >
            Log In
          </a>
          <p className="login-error">{error}</p>
        </div>
      </div>
    </section>
  );
}

function ConversationList({
  conversations,
  currentWorkflowId,
  draftConversation,
  onNewDraft,
  onSelect,
  onDelete,
}) {
  return (
    <div>
      {draftConversation ? (
        <div className="conversation-row">
          <button type="button" className="conversation-item active" onClick={onNewDraft}>
            New chat
          </button>
        </div>
      ) : null}
      {conversations.map((conversation) => (
        <div className="conversation-row" key={conversation.workflow_id}>
          <button
            type="button"
            className={`conversation-item${
              conversation.workflow_id === currentWorkflowId ? " active" : ""
            }`}
            onClick={() => onSelect(conversation.workflow_id)}
          >
            {conversation.title || "New chat"}
          </button>
          <button
            type="button"
            className="conversation-delete"
            title="Delete chat"
            aria-label="Delete chat"
            onClick={(event) => {
              event.stopPropagation();
              onDelete(conversation.workflow_id);
            }}
          >
            <svg
              viewBox="0 0 24 24"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect width="20" height="5" x="2" y="3" rx="1" />
              <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
              <path d="M10 12h4" />
            </svg>
          </button>
        </div>
      ))}
      {conversations.length === 0 && !draftConversation ? (
        <div className="tool-meta">No chats yet.</div>
      ) : null}
    </div>
  );
}

function Messages({
  workflowState,
  draftConversation,
  localPending,
  streamTurn,
  streamPanelCollapsed,
  resolvingApprovals,
  onToggleStreamPanel,
  onResolveApproval,
}) {
  const transcript = workflowState?.transcript || [];
  const messageItems = visibleMessageItems(transcript, localPending);
  const hasContent = workflowState || localPending.length > 0;
  return (
    <>
      {!hasContent ? (
        <div className="empty">
          {draftConversation
            ? "Type your first message to start a Temporal workflow."
            : "Starting a Temporal workflow..."}
        </div>
      ) : null}
      {messageItems.map((item) => (
        item.kind === "pending" ? (
          <Bubble
            key={item.pending.id}
            kind="pending"
            label={item.pending.label}
            content={`${item.pending.content} (${item.pending.phase})`}
          />
        ) : (
          <MessageBubble
            key={`transcript-${item.index}`}
            message={item.message}
            index={item.index}
            workflowState={workflowState}
          />
        )
      ))}
      <StreamPanel
        turn={streamTurn}
        collapsed={streamPanelCollapsed}
        onToggle={onToggleStreamPanel}
      />
      <ApprovalsPanel
        workflowState={workflowState}
        resolvingApprovals={resolvingApprovals}
        onResolve={onResolveApproval}
      />
    </>
  );
}

function MessageBubble({ message, index, workflowState }) {
  if (message.role === "user") {
    if (workflowState.active_message_index === index) {
      return <Bubble kind="pending" label="you -> agent" content={`${message.content} (delivered)`} />;
    }
    if ((workflowState.queued_message_indices || []).includes(index)) {
      return <Bubble kind="pending" label="you" content={`${message.content} (queued)`} />;
    }
    return <Bubble kind="user" label="you" content={message.content} />;
  }
  if (message.role === "assistant") {
    return <Bubble kind="assistant" label="assistant" content={message.content} />;
  }
  return <Bubble kind="system" label="system" content={message.content} />;
}

function Bubble({ kind, label, content }) {
  return (
    <div className={`bubble ${kind}`}>
      <span className="label">{label}</span>
      <MarkdownContent content={content} />
    </div>
  );
}

function MarkdownContent({ content, className = "" }) {
  return (
    <div className={`bubble-content${className ? ` ${className}` : ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {String(content || "")}
      </ReactMarkdown>
    </div>
  );
}

const markdownComponents = {
  a({ href, children }) {
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
  h1({ children }) {
    return <div className="md-heading">{children}</div>;
  },
  h2({ children }) {
    return <div className="md-heading">{children}</div>;
  },
  h3({ children }) {
    return <div className="md-heading">{children}</div>;
  },
  h4({ children }) {
    return <div className="md-heading">{children}</div>;
  },
  table({ children }) {
    return (
      <div className="markdown-table-wrap">
        <table>{children}</table>
      </div>
    );
  },
  pre({ children }) {
    const language = codeBlockLanguage(children);
    return <pre data-language={language || undefined}>{children}</pre>;
  },
  code({ className, children, node: _node, ...props }) {
    return (
      <code className={className || undefined} {...props}>
        {children}
      </code>
    );
  },
};

function codeBlockLanguage(children) {
  const child = Children.toArray(children).find((candidate) => isValidElement(candidate));
  const className = child?.props?.className || "";
  const match = className.match(/language-([A-Za-z0-9_+.#-]+)/);
  return normalizeCodeLanguage(match?.[1] || "");
}

function CodeBlock({ source, languageHint = null }) {
  const language = normalizeCodeLanguage(languageHint) || inferCodeLanguage(source);
  return (
    <pre data-language={language || undefined}>
      <code className={language ? `language-${language}` : undefined}>{source}</code>
    </pre>
  );
}

function ApprovalsPanel({ workflowState, resolvingApprovals, onResolve }) {
  const approvals = (workflowState?.pending_approvals || []).filter(
    (approval) => !resolvingApprovals.has(approval.approval_id),
  );
  if (approvals.length === 0) return null;
  return (
    <section className="approval-panel">
      <div className="approval-panel-header">
        <span>Approval Required</span>
        <span className="approval-panel-count">
          {approvals.length === 1 ? "1 pending" : `${approvals.length} pending`}
        </span>
      </div>
      {approvals.map((approval) => (
        <ApprovalCard key={approval.approval_id} approval={approval} onResolve={onResolve} />
      ))}
    </section>
  );
}

function ApprovalCard({ approval, onResolve }) {
  return (
    <div className="approval-card">
      <div className="approval-title">{approval.summary || approval.tool_name}</div>
      <div className="approval-meta">
        <ApprovalMetaRow label="Tool" value={approval.tool_name} />
        <ApprovalMetaRow label="Scope" value={approval.memory_key || "one time"} />
      </div>
      <div className="approval-details bubble-content">
        <ApprovalArgs args={approval.tool_args || {}} />
      </div>
      <div className="approval-actions">
        <button
          type="button"
          className="allow"
          onClick={() => onResolve(approval.approval_id, "allow")}
        >
          Allow
        </button>
        <button
          type="button"
          className="always"
          onClick={() => onResolve(approval.approval_id, "always_allow")}
        >
          Always Allow
        </button>
        <button
          type="button"
          className="deny"
          onClick={() => onResolve(approval.approval_id, "deny")}
        >
          Deny
        </button>
      </div>
    </div>
  );
}

function ApprovalMetaRow({ label, value }) {
  return (
    <div>
      <strong>{label}: </strong>
      {value || "unknown"}
    </div>
  );
}

function ApprovalArgs({ args }) {
  if (typeof args.code === "string") {
    const rest = { ...args };
    delete rest.code;
    return (
      <>
        <CodeBlock source={args.code} languageHint="python" />
        {Object.keys(rest).length > 0 ? (
          <CodeBlock source={JSON.stringify(rest, null, 2)} languageHint="json" />
        ) : null}
      </>
    );
  }

  if (typeof args.content === "string" && typeof args.name === "string") {
    const metadata = { ...args };
    delete metadata.content;
    const truncated = args.content.length > 12000;
    const preview = truncated
      ? `${args.content.slice(0, 12000)}\n...[truncated for approval preview]`
      : args.content;
    return (
      <>
        <CodeBlock source={JSON.stringify(metadata, null, 2)} languageHint="json" />
        <CodeBlock source={preview} languageHint={languageFromFileName(args.name)} />
      </>
    );
  }

  return <CodeBlock source={JSON.stringify(args, null, 2)} languageHint="json" />;
}

function StreamPanel({ turn, collapsed, onToggle }) {
  if (!turn) return null;
  if (
    !turn.text &&
    !turn.thinking &&
    turn.currentEvents.length === 0 &&
    turn.finishedTurns.length === 0
  ) {
    return null;
  }
  return (
    <section className={`stream-panel ${turn.status}${collapsed ? " collapsed" : ""}`}>
      <div className="stream-panel-header">
        <div className="stream-panel-title">
          Streaming visibility
          <span className="stream-panel-status">{streamPanelStatus(turn)}</span>
        </div>
        <button type="button" className="stream-panel-toggle" onClick={onToggle}>
          {collapsed ? "Expand" : "Collapse"}
        </button>
      </div>
      <div className="stream-panel-body">
        {collapsed ? (
          <div className="stream-preview">{streamPanelPreview(turn)}</div>
        ) : (
          <StreamPanelBody turn={turn} />
        )}
      </div>
    </section>
  );
}

function StreamPanelBody({ turn }) {
  return (
    <>
      {turn.finishedTurns.length ? (
        <div className="stream-finished-list">
          {turn.finishedTurns.map((finishedTurn, index) => (
            <FinishedStreamTurn
              key={`${finishedTurn.sequence ?? "turn"}-${index}`}
              finishedTurn={finishedTurn}
            />
          ))}
        </div>
      ) : null}
      {turn.text ? (
        <div className="stream-current-turn">
          <div className="stream-finished-title">
            Claude turn {turn.activeSequence ?? ""} streaming
          </div>
          {turn.thinking ? <div className="stream-thinking">{turn.thinking}</div> : null}
          <div className="stream-text">{turn.text}</div>
          {turn.currentEvents.length ? <StreamToolList events={turn.currentEvents} /> : null}
        </div>
      ) : null}
      {!turn.text && turn.thinking ? (
        <div className="stream-current-turn">
          <div className="stream-finished-title">
            Claude turn {turn.activeSequence ?? ""} thinking
          </div>
          <div className="stream-thinking">{turn.thinking}</div>
        </div>
      ) : null}
      {!turn.text && turn.currentEvents.length ? (
        <StreamToolList events={turn.currentEvents} />
      ) : null}
      {!turn.text &&
      !turn.thinking &&
      !turn.currentEvents.length &&
      !turn.finishedTurns.length ? (
        <div className="stream-preview">Waiting for streamed tokens or tool activity...</div>
      ) : null}
    </>
  );
}

function FinishedStreamTurn({ finishedTurn }) {
  return (
    <div className="stream-finished-turn">
      <div className="stream-finished-title">
        Claude turn {finishedTurn.sequence ?? ""} complete | {finishedTurn.stopReason}
      </div>
      {finishedTurn.thinking ? (
        <div className="stream-thinking">{finishedTurn.thinking}</div>
      ) : null}
      <div>{finishedTurn.text || `Completed without text (${finishedTurn.stopReason}).`}</div>
      {finishedTurn.events?.length ? <StreamToolList events={finishedTurn.events} /> : null}
    </div>
  );
}

function StreamToolList({ events }) {
  return (
    <div className="stream-tool-list">
      {events.slice(-5).map((event, index) => (
        <StreamToolEvent key={`${event.kind}-${index}`} event={event} />
      ))}
    </div>
  );
}

function StreamToolEvent({ event }) {
  return (
    <div
      className={`stream-tool-event${
        event.kind?.startsWith("claude_tool_input_") ? " input-streaming" : ""
      }`}
    >
      <div className="stream-tool-name">{streamToolLabel(event)}</div>
      <div className="stream-tool-payload">{streamToolPayloadText(event)}</div>
    </div>
  );
}

function ArtifactsPanel({ artifacts, onOpen }) {
  return (
    <aside className="artifacts-sidebar">
      <section className="artifact-panel">
        <div className="artifact-panel-header">
          <span>Artifacts</span>
          <span className="artifact-panel-count">
            {artifacts.length === 1 ? "1 file" : `${artifacts.length} files`}
          </span>
        </div>
        {artifacts.length === 0 ? (
          <div className="artifact-empty">Artifacts created by the agent will appear here.</div>
        ) : (
          <div className="artifact-list">
            {[...artifacts].reverse().map((artifact) => (
              <ArtifactCard key={artifact.artifact_id} artifact={artifact} onOpen={onOpen} />
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}

function ArtifactCard({ artifact, onOpen }) {
  return (
    <article className="artifact-card">
      <div className="artifact-name">{artifact.name || artifact.artifact_id}</div>
      <div className="artifact-meta">
        {artifact.mime_type || "application/octet-stream"} |{" "}
        {formatBytes(artifact.size_bytes || 0)}
      </div>
      <div className="artifact-actions">
        <button type="button" onClick={() => onOpen(artifact)}>
          View
        </button>
        <ArtifactLink url={artifact.download_url} label="Download" download />
      </div>
    </article>
  );
}

function ArtifactLink({ url, label, download = false }) {
  return download ? (
    <a href={url} download="">
      {label}
    </a>
  ) : (
    <a href={url} target="_blank" rel="noreferrer">
      {label}
    </a>
  );
}

function ArtifactViewer({ viewer, onClose }) {
  return (
    <section
      className="artifact-viewer-overlay"
      hidden={!viewer.open}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      {viewer.open && viewer.artifact ? (
        <div className="artifact-viewer">
          <div className="artifact-viewer-header">
            <div className="artifact-viewer-title">
              <div className="artifact-viewer-name">
                {viewer.artifact.name || viewer.artifact.artifact_id}
              </div>
              <div className="artifact-viewer-meta">
                {viewer.artifact.mime_type || "application/octet-stream"} |{" "}
                {formatBytes(viewer.artifact.size_bytes || 0)}
              </div>
            </div>
            <div className="artifact-viewer-actions">
              <ArtifactLink
                url={viewer.artifact.download_url}
                label="Download"
                download
              />
              <button type="button" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
          <div className="artifact-viewer-body">
            <ArtifactViewerBody viewer={viewer} />
          </div>
        </div>
      ) : null}
    </section>
  );
}

function ArtifactViewerBody({ viewer }) {
  const artifact = viewer.artifact;
  if (viewer.loading) return <div className="empty">Loading artifact...</div>;
  if (viewer.error) return <div className="artifact-viewer-error">{viewer.error}</div>;
  if (isImageArtifact(artifact)) {
    return (
      <img
        className="artifact-viewer-image"
        src={artifact.view_url}
        alt={artifact.name || "Artifact"}
      />
    );
  }
  if (isPdfArtifact(artifact)) {
    return <iframe className="artifact-viewer-frame" src={artifact.view_url}></iframe>;
  }
  if (isMarkdownArtifact(artifact)) {
    return <MarkdownContent className="artifact-markdown" content={viewer.text} />;
  }
  return (
    <div className="bubble-content">
      <CodeBlock source={viewer.text} languageHint={languageFromFileName(artifact.name)} />
    </div>
  );
}

function ToolsWindow({
  open,
  tools,
  mcpFormOpen,
  mcpFormSubmitting,
  mcpFormError,
  mcpFormValues,
  onClose,
  onOpenMcpForm,
  onCancelMcpForm,
  onUpdateMcpForm,
  onSubmitMcpForm,
  onRefreshTools,
  onSetMcpEnabled,
  onDeleteMcp,
  setStatusNotice,
  post,
}) {
  const builtInTools = tools.filter((tool) => !tool.provider?.startsWith("mcp:"));
  const mcpTools = tools.filter((tool) => tool.provider?.startsWith("mcp:"));

  return (
    <section
      className="tools-overlay"
      hidden={!open}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="tools-window"
        role="dialog"
        aria-modal="true"
        aria-labelledby="toolsWindowTitle"
      >
        <div className="tools-window-header">
          <div className="tools-window-title" id="toolsWindowTitle">
            Tools
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="tools-window-body">
          <section className="tools-section">
            <ToolsSectionHeader title="Built-in tools" />
            <div className="tools-grid">
              {builtInTools.map((tool) => (
                <BuiltInToolCard
                  key={tool.provider || tool.label}
                  tool={tool}
                  onRefreshTools={onRefreshTools}
                  setStatusNotice={setStatusNotice}
                  post={post}
                />
              ))}
            </div>
          </section>
          <section className="tools-section">
            <ToolsSectionHeader
              title="MCP servers"
              actions={
                <button type="button" onClick={onOpenMcpForm}>
                  Add HTTP MCP
                </button>
              }
            />
            {mcpFormOpen ? (
              <McpForm
                values={mcpFormValues}
                submitting={mcpFormSubmitting}
                error={mcpFormError}
                onUpdate={onUpdateMcpForm}
                onSubmit={onSubmitMcpForm}
                onCancel={onCancelMcpForm}
              />
            ) : null}
            <div className="tools-grid">
              {mcpTools.map((tool) => (
                <McpToolCard
                  key={tool.provider || tool.label}
                  tool={tool}
                  onSetEnabled={onSetMcpEnabled}
                  onDelete={onDeleteMcp}
                />
              ))}
              {mcpTools.length === 0 ? (
                <div className="tool-meta">No MCP servers connected.</div>
              ) : null}
            </div>
          </section>
        </div>
      </div>
    </section>
  );
}

function ToolsSectionHeader({ title, actions = null }) {
  return (
    <div className="tools-section-header">
      <div className="tools-section-title">{title}</div>
      {actions ? <div className="tools-section-actions">{actions}</div> : null}
    </div>
  );
}

function BuiltInToolCard({ tool, onRefreshTools, setStatusNotice, post }) {
  return (
    <ToolCard
      tool={tool}
      status={tool.connected ? "Connected" : "Disconnected"}
      connected={Boolean(tool.connected)}
      disabled={false}
    >
      {tool.provider === "github" ? (
        <div className="tool-actions">
          <button
            type="button"
            disabled={!tool.configured}
            onClick={async () => {
              if (tool.connected) {
                await post("/api/tools/github/disconnect", {});
                setStatusNotice("GitHub disconnected");
                await onRefreshTools();
              } else {
                window.location.href = "/oauth/github/start";
              }
            }}
          >
            {tool.connected ? "Disconnect" : "Connect"}
          </button>
        </div>
      ) : null}
    </ToolCard>
  );
}

function McpToolCard({ tool, onSetEnabled, onDelete }) {
  const connected = Boolean(tool.connected);
  const enabled = Boolean(tool.enabled);
  return (
    <ToolCard
      tool={tool}
      status={connected ? (enabled ? "Enabled" : "Disabled") : "Disconnected"}
      connected={connected && enabled}
      disabled={!enabled}
    >
      <div className="tool-actions">
        {tool.auth_mode === "oauth" ? (
          <button
            type="button"
            onClick={() => {
              window.location.href = mcpOAuthStartUrl({
                label: tool.label,
                serverUrl: tool.server_url || tool.login || "",
                toolPrefix: tool.tool_prefix || "",
                serverId: tool.server_id || tool.provider.slice("mcp:".length),
              });
            }}
          >
            Reconnect
          </button>
        ) : null}
        <button type="button" onClick={() => onSetEnabled(tool, !enabled)}>
          {enabled ? "Disable" : "Enable"}
        </button>
        <button type="button" className="danger" onClick={() => onDelete(tool)}>
          Delete
        </button>
      </div>
    </ToolCard>
  );
}

function ToolCard({ tool, status, connected, disabled, children }) {
  return (
    <div className={`tool-card${connected ? " connected" : ""}${disabled ? " disabled" : ""}`}>
      <div className="tool-title">
        <span className="tool-label">{tool.label}</span>
        <span className="tool-status">{status}</span>
      </div>
      <div className="tool-meta">{toolMetaText(tool)}</div>
      {tool.available_tools?.length ? (
        <div className="tool-chip-list">
          {tool.available_tools.slice(0, 8).map((toolName) => (
            <span className="tool-chip" key={toolName}>
              {toolName}
            </span>
          ))}
          {tool.available_tools.length > 8 ? (
            <span className="tool-chip">+{tool.available_tools.length - 8}</span>
          ) : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}

function McpForm({ values, submitting, error, onUpdate, onSubmit, onCancel }) {
  const prefixTouchedRef = useRef(false);
  return (
    <form className="mcp-form" onSubmit={onSubmit}>
      <McpField
        label="Label"
        name="label"
        placeholder="Temporal docs"
        value={values.label}
        onChange={(value) => {
          const patch = { label: value };
          if (!prefixTouchedRef.current) patch.tool_prefix = toolPrefixFromLabel(value);
          onUpdate(patch);
        }}
      />
      <McpField
        label="HTTP URL"
        name="server_url"
        placeholder="https://example.com/mcp"
        value={values.server_url}
        onChange={(value) => onUpdate({ server_url: value })}
      />
      <McpField
        label="Tool prefix"
        name="tool_prefix"
        placeholder="temporal"
        value={values.tool_prefix}
        onChange={(value) => {
          prefixTouchedRef.current = true;
          onUpdate({ tool_prefix: value });
        }}
      />
      <div className="mcp-field">
        <label htmlFor="mcp-auth-mode">Auth</label>
        <select
          id="mcp-auth-mode"
          name="auth_mode"
          value={values.auth_mode}
          onChange={(event) => onUpdate({ auth_mode: event.currentTarget.value })}
        >
          <option value="none">No auth</option>
          <option value="oauth">OAuth authorization</option>
          <option value="bearer">Bearer token</option>
        </select>
      </div>
      <McpField
        label="Bearer token"
        name="bearer_token"
        placeholder=""
        type="password"
        value={values.bearer_token}
        hidden={values.auth_mode !== "bearer"}
        required={values.auth_mode === "bearer"}
        onChange={(value) => onUpdate({ bearer_token: value })}
      />
      {error ? <div className="mcp-error">{error}</div> : null}
      <div className="mcp-form-actions">
        <button type="submit" className="primary" disabled={submitting}>
          {submitting ? "Adding..." : "Add"}
        </button>
        <button type="button" disabled={submitting} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function McpField({
  label,
  name,
  placeholder,
  value,
  onChange,
  type = "text",
  hidden = false,
  required = true,
}) {
  return (
    <div className="mcp-field" data-field={name} hidden={hidden}>
      <label htmlFor={`mcp-${name}`}>{label}</label>
      <input
        id={`mcp-${name}`}
        name={name}
        type={type}
        placeholder={placeholder}
        value={value}
        required={required}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
    </div>
  );
}

function updateWorkflowStateInState(previous, nextWorkflowState) {
  const normalized = normalizeWorkflowState(nextWorkflowState, previous.workflowState);
  const previousAssistantCount = previous.workflowState
    ? previous.workflowState.transcript.filter((message) => message.role === "assistant").length
    : 0;
  const nextAssistantCount = normalized.transcript.filter(
    (message) => message.role === "assistant",
  ).length;
  const resolvingApprovals = new Set(previous.resolvingApprovals);
  const pendingApprovalIds = new Set(
    (normalized.pending_approvals || []).map((approval) => approval.approval_id),
  );
  for (const approvalId of resolvingApprovals) {
    if (!pendingApprovalIds.has(approvalId)) resolvingApprovals.delete(approvalId);
  }
  let next = {
    ...previous,
    workflowState: normalized,
    workflowStateProjectionRevision: Math.max(
      previous.workflowStateProjectionRevision || 0,
      Number(normalized.state_revision || 0),
    ),
    workflowTranscriptProjectionRevision: Math.max(
      previous.workflowTranscriptProjectionRevision || 0,
      Number(normalized.transcript_revision || 0),
    ),
    localPending: previous.localPending.filter(
      (pending) => !isAcknowledged(pending, normalized),
    ),
    resolvingApprovals,
    statusNotice: "",
  };
  if (nextAssistantCount > previousAssistantCount) next = markStreamCommittedInState(next);
  if (!hasLiveWorkflowActivity(next, normalized)) next = markStreamCommittedInState(next);
  return next;
}

function normalizeWorkflowState(nextWorkflowState, previousWorkflowState = null) {
  return {
    ...nextWorkflowState,
    transcript: nextWorkflowState.transcript || [],
    pending_approvals: nextWorkflowState.pending_approvals || [],
    queued_message_indices: nextWorkflowState.queued_message_indices || [],
    artifacts: nextWorkflowState.artifacts || previousWorkflowState?.artifacts || [],
  };
}

function createPendingMessage(label, content, phase, state) {
  return {
    id: crypto.randomUUID(),
    label,
    content,
    phase,
    transcriptIndex: state.workflowState?.transcript?.length || 0,
  };
}

function visibleMessageItems(transcript, localPending) {
  const pendingByIndex = new Map();
  for (const pending of localPending) {
    if (isPendingAcknowledgedByTranscript(pending, transcript)) continue;
    const index = pendingTranscriptIndex(pending, transcript.length);
    const pendingAtIndex = pendingByIndex.get(index) || [];
    pendingAtIndex.push(pending);
    pendingByIndex.set(index, pendingAtIndex);
  }

  const items = [];
  for (let index = 0; index <= transcript.length; index += 1) {
    for (const pending of pendingByIndex.get(index) || []) {
      items.push({ kind: "pending", pending });
    }
    if (index < transcript.length) {
      items.push({ kind: "transcript", message: transcript[index], index });
    }
  }
  return items;
}

function pendingTranscriptIndex(pending, transcriptLength) {
  const index = Number(pending.transcriptIndex);
  if (!Number.isFinite(index)) return transcriptLength;
  return Math.max(0, Math.min(transcriptLength, index));
}

function handleStreamEventInState(previous, event) {
  const projectionResult = applyWorkflowProjectionEventInState(previous, event);
  if (projectionResult.handled) return projectionResult.state;

  const artifactResult = applyArtifactStreamEventInState(previous, event);
  if (artifactResult.handled) return artifactResult.state;

  if (!hasLiveWorkflowActivity(previous)) return previous;

  const next = {
    ...previous,
    streamTurn: cloneStreamTurn(previous.streamTurn),
  };
  const sequence = event.payload?.sequence ?? null;

  if (event.kind === "claude_start") {
    next.currentClaudeSequence = sequence;
    next.ignoreClaudeUntilStart = false;
    if (isOpenStreamTurn(next.streamTurn)) {
      registerStreamSequence(next.streamTurn, sequence);
      next.streamTurn.status = "streaming";
      next.streamTurn.activeSequence = sequence;
    }
  } else if (event.kind === "claude_text_delta" && event.payload?.text) {
    if (next.ignoreClaudeUntilStart || sequence !== next.currentClaudeSequence) return previous;
    const turn = ensureStreamTurn(next, sequence);
    turn.status = "streaming";
    turn.text += event.payload.text;
  } else if (event.kind === "claude_thinking_start") {
    if (next.ignoreClaudeUntilStart || sequence !== next.currentClaudeSequence) return previous;
    const turn = ensureStreamTurn(next, sequence);
    turn.status = "streaming";
  } else if (event.kind === "claude_thinking_delta" && event.payload?.thinking) {
    if (next.ignoreClaudeUntilStart || sequence !== next.currentClaudeSequence) return previous;
    const turn = ensureStreamTurn(next, sequence);
    turn.status = "streaming";
    turn.thinking += event.payload.thinking;
  } else if (event.kind === "claude_cancelled") {
    if (sequence === next.currentClaudeSequence) {
      return {
        ...markStreamInterruptedInState(next),
        ignoreClaudeUntilStart: true,
      };
    }
  } else if (event.kind === "claude_complete") {
    const terminal = isTerminalClaudeStop(event.payload || {});
    if (!terminal && sequence !== next.currentClaudeSequence) return previous;
    const turn = streamTurnForSequence(next.streamTurn, sequence) || ensureStreamTurn(next, sequence);
    if (turn) {
      finishStreamClaudeTurn(turn, event.payload || {});
      turn.status = terminal ? "complete" : turn.currentEvents.length ? "tooling" : "waiting";
      turn.lastClaudeCompletedAt = new Date().toISOString();
    }
    if (terminal) {
      return next;
    }
  } else if (isClaudeToolEvent(event)) {
    const turn = ensureStreamTurn(next, next.currentClaudeSequence);
    appendStreamToolEvent(turn, event);
    if (turn.status !== "complete" && turn.status !== "interrupted") {
      turn.status = "tooling";
    }
  } else if (!event.kind?.startsWith("claude_")) {
    const turn = ensureStreamTurn(next, next.currentClaudeSequence);
    appendStreamToolEvent(turn, event);
    if (turn.status !== "complete" && turn.status !== "interrupted") {
      turn.status = "tooling";
    }
  }
  return next;
}

function applyWorkflowProjectionEventInState(previous, event) {
  if (!previous.workflowState) return { handled: false, state: previous };
  const payload = event.payload || {};

  if (event.kind === "workflow_state") {
    const revision = Number(payload.revision || 0);
    if (revision && revision <= previous.workflowStateProjectionRevision) {
      return { handled: true, state: previous };
    }
    const { revision: _revision, transcript_revision: _transcriptRevision, ...patch } = payload;
    return {
      handled: true,
      state: updateWorkflowStateInState(previous, {
        ...previous.workflowState,
        ...patch,
        state_revision: revision || previous.workflowState.state_revision || 0,
        transcript_revision: previous.workflowState.transcript_revision || 0,
        transcript: previous.workflowState.transcript || [],
        artifacts: previous.workflowState.artifacts || [],
      }),
    };
  }

  if (event.kind === "workflow_transcript") {
    const revision = Number(payload.revision || 0);
    if (revision && revision <= previous.workflowTranscriptProjectionRevision) {
      return { handled: true, state: previous };
    }
    return {
      handled: true,
      state: updateWorkflowStateInState(previous, {
        ...previous.workflowState,
        transcript: payload.transcript || [],
        transcript_revision: revision || previous.workflowState.transcript_revision || 0,
        artifacts: previous.workflowState.artifacts || [],
      }),
    };
  }

  return { handled: false, state: previous };
}

function applyArtifactStreamEventInState(previous, event) {
  if (event.kind !== "artifact_create_complete" || !previous.workflowState) {
    return { handled: false, state: previous };
  }
  const artifact = event.payload || {};
  if (!artifact.artifact_id) return { handled: true, state: previous };
  const artifacts = previous.workflowState.artifacts || [];
  if (artifacts.some((existing) => existing.artifact_id === artifact.artifact_id)) {
    return { handled: true, state: previous };
  }
  return {
    handled: true,
    state: {
      ...previous,
      workflowState: {
        ...previous.workflowState,
        artifacts: [...artifacts, artifact],
      },
    },
  };
}

function ensureStreamTurn(state, sequence) {
  if (!isOpenStreamTurn(state.streamTurn)) {
    state.streamTurn = createStreamTurn(sequence);
  } else {
    registerStreamSequence(state.streamTurn, sequence);
  }
  return state.streamTurn;
}

function streamTurnForSequence(turn, sequence) {
  if (!isOpenStreamTurn(turn)) return null;
  if (sequence === null) return turn;
  return turn.sequences.includes(sequence) ? turn : null;
}

function isOpenStreamTurn(turn) {
  return Boolean(turn && turn.status !== "complete" && turn.status !== "interrupted");
}

function registerStreamSequence(turn, sequence) {
  if (sequence !== null && !turn.sequences.includes(sequence)) {
    turn.sequences.push(sequence);
  }
}

function createStreamTurn(sequence) {
  return {
    sequence,
    sequences: sequence === null ? [] : [sequence],
    activeSequence: sequence,
    status: "streaming",
    text: "",
    thinking: "",
    finishedTurns: [],
    currentEvents: [],
    startedAt: new Date().toISOString(),
    completedAt: null,
    lastClaudeCompletedAt: null,
    interrupted: false,
  };
}

function cloneStreamTurn(turn) {
  if (!turn) return null;
  return {
    ...turn,
    sequences: [...turn.sequences],
    finishedTurns: turn.finishedTurns.map((finishedTurn) => ({
      ...finishedTurn,
      events: [...(finishedTurn.events || [])],
    })),
    currentEvents: [...turn.currentEvents],
  };
}

function finishStreamClaudeTurn(turn, payload) {
  const text = String(payload.text || turn.text || "").trim();
  const stopReason = payload.stop_reason || "unknown";
  const sequence = payload.sequence ?? turn.activeSequence;
  turn.finishedTurns.push({
    sequence,
    text,
    thinking: String(turn.thinking || "").trim(),
    stopReason,
    usage: payload.usage || null,
    events: turn.currentEvents,
    completedAt: new Date().toISOString(),
  });
  turn.finishedTurns = turn.finishedTurns.slice(-12);
  turn.text = "";
  turn.thinking = "";
  turn.currentEvents = [];
}

function appendStreamToolEvent(turn, event) {
  const finishedTurn = latestFinishedToolUseTurn(turn);
  if (finishedTurn) {
    finishedTurn.events = mergeStreamToolEvent(finishedTurn.events || [], event);
    return;
  }
  turn.currentEvents = mergeStreamToolEvent(turn.currentEvents, event);
}

function isClaudeToolEvent(event) {
  return event.kind?.startsWith("claude_tool_input_");
}

function mergeStreamToolEvent(events, event) {
  if (!event.kind?.startsWith("claude_tool_input_")) {
    return [...events, event].slice(-5);
  }

  const key = streamToolInputKey(event);
  const nextEvents = [...events];
  const existingIndex = nextEvents.findIndex(
    (candidate) =>
      candidate.kind?.startsWith("claude_tool_input_") &&
      streamToolInputKey(candidate) === key,
  );
  const existing = existingIndex >= 0 ? nextEvents[existingIndex] : null;
  const merged = mergeToolInputEvent(existing, event, key);
  if (existingIndex >= 0) {
    nextEvents[existingIndex] = merged;
  } else {
    nextEvents.push(merged);
  }
  return nextEvents.slice(-5);
}

function mergeToolInputEvent(existing, event, key) {
  const existingPayload = existing?.payload || {};
  const payload = event.payload || {};
  const nextPayload = { ...existingPayload, ...payload };
  const existingPartial = String(existingPayload.input_partial || "");

  if (event.kind === "claude_tool_input_delta") {
    nextPayload.input_partial = existingPartial + String(payload.partial_json || "");
    nextPayload.status = "streaming input";
  } else if (event.kind === "claude_tool_input_complete") {
    nextPayload.input_partial = existingPartial;
    nextPayload.status = "input complete";
  } else {
    nextPayload.input_partial = existingPartial;
    nextPayload.status = "building input";
  }

  return {
    ...(existing || event),
    kind: event.kind,
    payload: nextPayload,
    streamToolInputKey: key,
  };
}

function streamToolInputKey(event) {
  return (
    event.streamToolInputKey ||
    event.payload?.tool_use_id ||
    `block:${event.payload?.content_block_index ?? "unknown"}`
  );
}

function latestFinishedToolUseTurn(turn) {
  const latest = turn.finishedTurns[turn.finishedTurns.length - 1];
  if (!latest || latest.stopReason !== "tool_use") return null;
  return latest;
}

function markStreamCommittedInState(state) {
  return {
    ...state,
    streamTurn: null,
    currentClaudeSequence: null,
    ignoreClaudeUntilStart: false,
  };
}

function markStreamInterruptedInState(state) {
  return {
    ...state,
    streamTurn: null,
    currentClaudeSequence: null,
  };
}

function hasLiveWorkflowActivity(state, workflowState = state.workflowState) {
  if (!workflowState) return true;
  if (workflowState.status === "responding") return true;
  if (Number(workflowState.pending_messages || 0) > 0) return true;
  return state.localPending.length > 0;
}

function isTerminalClaudeStop(payload) {
  return payload.stop_reason && payload.stop_reason !== "tool_use";
}

function isAcknowledged(pending, workflowState) {
  return isPendingAcknowledgedByTranscript(pending, workflowState.transcript);
}

function isPendingAcknowledgedByTranscript(pending, transcript) {
  return transcript.some((message) => {
    if (message.role === "user" && message.content === pending.content) return true;
    if (message.role === "system" && message.content.includes(pending.content)) return true;
    return false;
  });
}

function displayStatus(state) {
  if (state.statusNotice) return state.statusNotice;
  const workflowState = state.workflowState;
  const thinkingLabel = workflowState?.thinking?.enabled ? " | thinking" : "";
  const modelLabel = workflowState?.model ? ` | ${workflowState.model}${thinkingLabel}` : "";
  if (state.draftConversation) return "draft | workflow not started";
  if (workflowState) {
    const queued = workflowState.pending_messages
      ? `, queued: ${workflowState.pending_messages}`
      : "";
    return `${workflowState.status}${queued}${modelLabel}`;
  }
  return state.auth === "app" ? "starting..." : "connecting...";
}

function selectedModelUsesAdaptiveThinking(state) {
  const model = state.agentSettings.model || "";
  return (state.config?.thinking?.adaptive_model_prefixes || []).some((prefix) =>
    model.startsWith(prefix),
  );
}

function agentSettingsFromConfig(config) {
  const modelOptions = config.model_options || [];
  const savedModel = localStorage.getItem("simpleChatModel");
  const savedEffort = localStorage.getItem("simpleChatThinkingEffort");
  const effortOptions = config.thinking?.effort_options || ["medium"];
  return {
    model: savedModel && modelOptions.includes(savedModel) ? savedModel : config.default_model || "",
    thinkingEnabled: localStorage.getItem("simpleChatThinkingEnabled") === "true",
    thinkingBudgetTokens: Number(
      localStorage.getItem("simpleChatThinkingBudgetTokens") ||
        config.thinking?.budget_tokens ||
        4096,
    ),
    thinkingEffort:
      savedEffort && effortOptions.includes(savedEffort)
        ? savedEffort
        : config.thinking?.effort || "medium",
  };
}

function saveAgentSettings(agentSettings) {
  localStorage.setItem("simpleChatModel", agentSettings.model);
  localStorage.setItem("simpleChatThinkingEnabled", String(agentSettings.thinkingEnabled));
  localStorage.setItem(
    "simpleChatThinkingBudgetTokens",
    String(agentSettings.thinkingBudgetTokens),
  );
  localStorage.setItem("simpleChatThinkingEffort", agentSettings.thinkingEffort);
}

function temporalUiUrl(conversation) {
  if (conversation.temporal_ui_url) return conversation.temporal_ui_url;
  const workflow = encodeURIComponent(conversation.workflow_id);
  const run = encodeURIComponent(conversation.run_id || "");
  if (run) {
    return `http://localhost:8233/namespaces/default/workflows/${workflow}/${run}/history`;
  }
  return `http://localhost:8233/namespaces/default/workflows/${workflow}`;
}

function toolMetaText(tool) {
  if (!tool.configured) {
    return "Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET.";
  }
  if (tool.provider?.startsWith("mcp:")) {
    return `${tool.login || "HTTP MCP"} | ${tool.available_tools?.length || 0} tools | ${tool.scopes}`;
  }
  if (tool.connected && tool.login) {
    return `@${tool.login} | ${tool.scopes || "no scopes returned"}`;
  }
  return `Scopes: ${tool.scopes || "none"}`;
}

function mcpOAuthStartUrl({ label, serverUrl, toolPrefix, serverId = "" }) {
  const params = new URLSearchParams({
    label,
    server_url: serverUrl,
    tool_prefix: toolPrefix,
  });
  if (serverId) params.set("server_id", serverId);
  return `/api/mcp-servers/oauth/start?${params.toString()}`;
}

function toolPrefixFromLabel(label) {
  return (
    label
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "_")
      .replace(/^_+|_+$/g, "") || "mcp"
  );
}

function streamToolPayloadText(event) {
  const payload = event.payload || {};
  if (event.kind?.startsWith("claude_tool_input_")) {
    const status = payload.status || "building input";
    if (event.kind === "claude_tool_input_complete") {
      return `${status}:\n${truncateStreamText(
        formatStreamValue(payload.input ?? payload.input_partial ?? payload.input_preview),
      )}`;
    }
    const partial = payload.input_partial || payload.partial_json || "";
    return `${status}:\n${truncateStreamText(String(partial))}`;
  }

  return `${event.kind}: ${truncateStreamText(formatStreamValue(payload))}`;
}

function streamPanelStatus(turn) {
  const count =
    turn.currentEvents.length +
    turn.finishedTurns.reduce(
      (total, finishedTurn) => total + (finishedTurn.events?.length || 0),
      0,
    );
  const toolText = count === 1 ? "1 tool event" : `${count} tool events`;
  const turnCount = turn.finishedTurns.length;
  const turnText = turnCount === 1 ? "1 Claude turn" : `${turnCount} Claude turns`;
  if (turn.status === "interrupted") return `interrupted | ${toolText}`;
  if (turn.status === "complete") return `complete | ${turnText} | ${toolText}`;
  if (turn.status === "tooling") return `tool activity | ${turnText} | ${toolText}`;
  if (turn.status === "waiting") return `finalizing | ${turnText} | ${toolText}`;
  return `streaming | ${turnText} | ${toolText}`;
}

function streamPanelPreview(turn) {
  const text = turn.text.trim();
  const thinking = String(turn.thinking || "").trim();
  const latestEvent = turn.currentEvents[turn.currentEvents.length - 1];
  if (text) return text.replace(/\s+/g, " ").slice(-240);
  if (thinking) return thinking.replace(/\s+/g, " ").slice(-240);
  const latestFinished = turn.finishedTurns[turn.finishedTurns.length - 1];
  if (latestFinished?.text) return latestFinished.text.replace(/\s+/g, " ").slice(-240);
  if (latestFinished?.thinking) {
    return latestFinished.thinking.replace(/\s+/g, " ").slice(-240);
  }
  if (latestEvent) return `${streamToolLabel(latestEvent)} | ${latestEvent.kind}`;
  return streamPanelStatus(turn);
}

function streamToolLabel(event) {
  const payloadToolName = event.payload?.tool_name;
  const name = payloadToolName || event.tool_name || "stream";
  return event.step ? `${name}:${event.step}` : name;
}

function formatStreamValue(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function truncateStreamText(value) {
  const text = String(value || "");
  if (text.length <= 4000) return text;
  return text.slice(-4000);
}

function isImageArtifact(artifact) {
  const mimeType = artifact?.mime_type || "";
  return mimeType.startsWith("image/") && mimeType !== "image/svg+xml";
}

function isPdfArtifact(artifact) {
  return artifact?.mime_type === "application/pdf";
}

function isMarkdownArtifact(artifact) {
  const mimeType = String(artifact?.mime_type || "").toLowerCase();
  const name = String(artifact?.name || artifact?.artifact_id || "").toLowerCase();
  return (
    mimeType === "text/markdown" ||
    mimeType === "text/x-markdown" ||
    name.endsWith(".md") ||
    name.endsWith(".markdown")
  );
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function languageFromFileName(name) {
  const extension = String(name || "").split(".").pop()?.toLowerCase();
  const languages = {
    bash: "bash",
    css: "css",
    html: "html",
    js: "javascript",
    json: "json",
    md: "markdown",
    markdown: "markdown",
    py: "python",
    sh: "bash",
    sql: "sql",
    ts: "typescript",
    xml: "xml",
    yaml: "yaml",
    yml: "yaml",
  };
  return languages[extension] || null;
}

function normalizeCodeLanguage(language) {
  if (!language) return null;
  const normalized = language.toLowerCase();
  const aliases = {
    bash: "bash",
    cjs: "javascript",
    css: "css",
    html: "html",
    javascript: "javascript",
    js: "javascript",
    json: "json",
    jsonc: "json",
    jsx: "javascript",
    markdown: "markdown",
    md: "markdown",
    mjs: "javascript",
    py: "python",
    python: "python",
    sh: "bash",
    shell: "bash",
    sql: "sql",
    ts: "typescript",
    tsx: "typescript",
    typescript: "typescript",
    xml: "xml",
    yaml: "yaml",
    yml: "yaml",
    zsh: "bash",
  };
  return aliases[normalized] || null;
}

function inferCodeLanguage(source) {
  const trimmed = source.trim();
  if (!trimmed) return null;
  if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && looksLikeJson(trimmed)) {
    return "json";
  }
  if (/^\s*(from\s+\w+\s+import|import\s+\w+|def\s+\w+|class\s+\w+|async\s+def\s+\w+)\b/m.test(source)) {
    return "python";
  }
  if (/\b(print|range|len)\s*\(/.test(source) && /(^|\n)\s*#/.test(source)) {
    return "python";
  }
  if (/\b(const|let|function|console\.log|=>|import\s+.+\s+from)\b/.test(source)) {
    return "javascript";
  }
  if (/^#!.*\b(?:bash|sh|zsh)\b/m.test(source) || /\b(?:echo|curl|export|chmod|sudo)\b/.test(source)) {
    return "bash";
  }
  if (/\bselect\b[\s\S]+\bfrom\b/i.test(source)) return "sql";
  if (/^\s*</.test(source) && /<\/?[A-Za-z][\s\S]*>/.test(source)) return "html";
  if (/^[\s\S]*\{[\s\S]*:[\s\S]*\}/.test(source) && /[.#]?[A-Za-z][\w-]*\s*\{/.test(source)) {
    return "css";
  }
  if (/^[A-Za-z_][\w.-]*\s*:/m.test(source)) return "yaml";
  return null;
}

function looksLikeJson(source) {
  try {
    JSON.parse(source);
    return true;
  } catch (_error) {
    return false;
  }
}

function jsonHeaders() {
  return { "content-type": "application/json" };
}

async function responseErrorText(response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text);
    if (typeof body.detail === "string") return body.detail;
    if (body.detail) return JSON.stringify(body.detail);
  } catch (_error) {
  }
  return text || `${response.status} ${response.statusText}`;
}
