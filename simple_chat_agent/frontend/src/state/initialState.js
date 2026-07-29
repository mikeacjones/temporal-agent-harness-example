export const defaultSystemPrompt =
  "You are a rigorous deep-research agent. For substantive requests, " +
  "investigate before answering: decompose the problem, use available " +
  "research, retrieval, browsing, code-execution, and delegation tools, " +
  "and consult multiple independent primary sources when possible. " +
  "Cross-check important claims and continue iterating until the evidence " +
  "is sufficient; do not stop at the first plausible result. Clearly " +
  "distinguish verified facts, reasoned inference, and uncertainty, and " +
  "cite or link sources when available. Synthesize the result into a " +
  "direct answer with the key evidence, caveats, and practical next steps. " +
  "Make reasonable assumptions when ambiguity is low and state them; ask " +
  "a clarifying question only when the answer would materially change the " +
  "work. Scale the effort to the task so simple requests still receive " +
  "concise, direct responses.";

export const defaultAgentSettings = {
  model: "",
  thinkingEnabled: false,
  thinkingMode: "enabled",
  thinkingBudgetTokens: 4096,
  thinkingEffort: "max",
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
  authMode: "unknown",
  loginConfigured: true,
  loginSubtitle: "",
  loginError: "",
  loggingIn: false,
  localLoginUsername: "demo",
  localLoginPassword: "demo",
  config: null,
  demoWorkspace: null,
  demoWorkspaceLoading: false,
  agentSettings: defaultAgentSettings,
  conversations: [],
  tools: [],
  workflowId: null,
  runId: null,
  temporalUiUrl: null,
  workflowState: null,
  workflowStateProjectionRevision: 0,
  workflowTranscriptProjectionRevision: 0,
  olderMessagesLoading: false,
  olderMessagesError: "",
  streamTurn: null,
  turnTraces: {},
  expandedTraceIndex: null,
  streamPanelCollapsed: false,
  currentAgentSequence: null,
  ignoreAgentUntilStart: false,
  localPending: [],
  composerAttachments: [],
  attachmentUploading: false,
  attachmentError: "",
  resolvingApprovals: new Set(),
  recoveringMissingWorkflow: false,
  toolsWindowOpen: false,
  artifactViewer: emptyArtifactViewer,
  draftConversation: true,
  draftSystemPrompt: defaultSystemPrompt,
  mcpFormOpen: false,
  mcpFormSubmitting: false,
  mcpFormError: "",
  mcpFormValues: defaultMcpFormValues,
  statusNotice: "",
};
