from enum import StrEnum


class ToolType(StrEnum):
    READ = "read"
    MUTATING = "mutating"
    MCP = "mcp"
    ADMIN = "admin"
