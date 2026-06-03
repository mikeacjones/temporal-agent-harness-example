import { useEffect, useRef, useState } from "react";

import { formatBytes } from "../utils/format.js";

const PASTE_ATTACHMENT_THRESHOLD_CHARS = 3000;

export function Composer({
  temporalUiUrl,
  attachments = [],
  attachmentUploading = false,
  attachmentError = "",
  onMessageChange,
  onSend,
  onInterrupt,
  onFilesSelected,
  onPasteAttachment,
  onRemoveAttachment,
  resetToken,
}) {
  const [message, setMessage] = useState("");
  const fileInputRef = useRef(null);

  useEffect(() => {
    setMessage("");
  }, [resetToken]);

  function updateMessage(value) {
    setMessage(value);
    onMessageChange(value);
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        onSend();
      }}
      onDragOver={(event) => {
        if (event.dataTransfer?.types?.includes("Files")) {
          event.preventDefault();
        }
      }}
      onDrop={(event) => {
        const files = Array.from(event.dataTransfer?.files || []);
        if (files.length) {
          event.preventDefault();
          onFilesSelected?.(files);
        }
      }}
    >
      <div className="composer-input-shell">
        <textarea
          value={message}
          onChange={(event) => updateMessage(event.currentTarget.value)}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData?.files || []);
            if (files.length) {
              event.preventDefault();
              onFilesSelected?.(files);
              return;
            }
            const text = event.clipboardData?.getData("text") || "";
            if (text.length >= PASTE_ATTACHMENT_THRESHOLD_CHARS) {
              event.preventDefault();
              onPasteAttachment?.(text);
            }
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSend();
            }
          }}
          placeholder="Type to chat. While responding, Send becomes steering."
        ></textarea>
        {attachments.length || attachmentUploading || attachmentError ? (
          <div className="composer-attachments">
            {attachments.map((attachment) => (
              <div
                key={attachment.attachment_id || attachment.artifact_id}
                className="composer-attachment-chip"
              >
                <span>{attachment.name || "attachment"}</span>
                <small>
                  {attachment.content_kind || "file"} |{" "}
                  {formatBytes(attachment.size_bytes || 0)}
                </small>
                <button
                  type="button"
                  aria-label={`Remove ${attachment.name || "attachment"}`}
                  onClick={() =>
                    onRemoveAttachment?.(
                      attachment.attachment_id || attachment.artifact_id,
                    )
                  }
                >
                  x
                </button>
              </div>
            ))}
            {attachmentUploading ? (
              <div className="composer-attachment-status">Uploading...</div>
            ) : null}
            {attachmentError ? (
              <div className="composer-attachment-error">{attachmentError}</div>
            ) : null}
          </div>
        ) : null}
      </div>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files || []);
          event.currentTarget.value = "";
          if (files.length) onFilesSelected?.(files);
        }}
      />
      <button
        type="button"
        className="icon-button"
        title="Attach files"
        onClick={() => fileInputRef.current?.click()}
        disabled={attachmentUploading}
      >
        📎
      </button>
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
