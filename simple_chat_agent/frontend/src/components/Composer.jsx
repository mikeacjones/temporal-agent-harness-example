export function Composer({
  message,
  temporalUiUrl,
  onMessageChange,
  onSend,
  onInterrupt,
}) {
  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        onSend();
      }}
    >
      <textarea
        value={message}
        onChange={(event) => onMessageChange(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            onSend();
          }
        }}
        placeholder="Type to chat. While responding, Send becomes steering."
      ></textarea>
      <button className="primary" type="submit">
        Send
      </button>
      {temporalUiUrl ? (
        <a
          className="temporal-link"
          href={temporalUiUrl}
          target="_blank"
          rel="noreferrer"
        >
          Workflow
        </a>
      ) : null}
      <button type="button" onClick={onInterrupt}>
        Interrupt
      </button>
    </form>
  );
}
