import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { ArtifactViewer, ArtifactsPanel } from "./components/Artifacts.jsx";
import { AppHeader } from "./components/AppHeader.jsx";
import { Composer } from "./components/Composer.jsx";
import { LoginScreen } from "./components/LoginScreen.jsx";
import { Messages } from "./components/Messages.jsx";
import { ToolsWindow } from "./components/ToolsWindow.jsx";
import { artifactNeedsTextFetch, artifactPreviewKind } from "./utils/artifacts.js";
import { jsonHeaders, responseErrorText } from "./utils/http.js";
import {
  agentSettingsFromConfig,
  createPendingMessage,
  displayStatus,
  handleStreamEventInState,
  markStreamInterruptedInState,
  saveAgentSettings,
  selectedModelUsesAdaptiveThinking,
  temporalUiUrl,
  updateWorkflowStateInState,
} from "./state/chatState.js";
import {
  defaultMcpFormValues,
  emptyArtifactViewer,
  initialState,
} from "./state/initialState.js";

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
    const previewKind = artifactPreviewKind(artifact);
    setState((previous) => ({
      ...previous,
      artifactViewer: {
        open: true,
        artifact,
        previewKind,
        loading: true,
        error: "",
        text: "",
      },
    }));

    if (!artifactNeedsTextFetch(previewKind)) {
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
        <AppHeader
          state={state}
          adaptiveThinking={adaptiveThinking}
          status={status}
          onNewChat={startDraftConversation}
          onOpenTools={() =>
            setState((previous) => ({ ...previous, toolsWindowOpen: true }))
          }
          onLogout={async () => {
            await post("/api/logout", {});
            localStorage.removeItem("simpleChatWorkflowId");
            closeEventSource();
            setState({
              ...initialState,
              auth: "login",
            });
            configureLoginButton();
          }}
          onUpdateAgentSettings={updateAgentSettings}
          onUpdateThinkingBudget={updateThinkingBudget}
          onSelectConversation={selectConversation}
          onDeleteConversation={(workflowId) => {
            deleteConversation(workflowId).catch((error) => {
              setStatusNotice(`delete failed: ${error}`);
            });
          }}
        />
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
        <Composer
          message={message}
          temporalUiUrl={state.temporalUiUrl}
          onMessageChange={setMessage}
          onSend={sendDefault}
          onInterrupt={() => sendAction("interrupt", "you interrupt", "sending")}
        />
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
