export const defaultAgentSettings = {
  model: "",
  thinkingEnabled: false,
  thinkingBudgetTokens: 4096,
  thinkingEffort: "medium",
};

export const defaultMcpFormValues = {
  label: "",
  server_url: "",
  tool_prefix: "",
  auth_mode: "none",
  bearer_token: "",
};

export const emptyArtifactViewer = {
  open: false,
  artifact: null,
  previewKind: "",
  loading: false,
  error: "",
  text: "",
};

export const initialState = {
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
