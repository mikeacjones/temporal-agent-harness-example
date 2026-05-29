import os

# Task queue is environment-configurable so an isolated test stack can run in
# the same Temporal namespace as prod without sharing workers. Defaults to the
# production queue.
TASK_QUEUE = os.environ.get("SIMPLE_CHAT_TASK_QUEUE", "simple-chat-agent")
