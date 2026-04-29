| Field   | Variant                          | Sub-Field             | Support Status        |
|---|----------------------------------|-----------------------|-----------------------|
| content | string                           |                       | Fully Supported       |
| content | array, type="text"               | text                  | Fully Supported       |
| content | array, type="text"               | cache_control         | Ignored               |
| content | array, type="text"               | citations             | Ignored               |
| content | array, type="image"              |                       | Not Supported         |
| content | array, type = "document"         |                       | Not Supported         |
| content | array, type = "search_result"    |                       | Not Supported         |
| content | array, type = "thinking"         |                       | Supported             |
| content | array, type="redacted_thinking"  |                       | Not Supported         |
| content | array, type = "tool_use"         | id                    | Fully Supported       |
| content | array, type = "tool_use"         | input                 | Fully Supported       |
| content | array, type = "tool_use"         | name                  | Fully Supported       |
| content | array, type = "tool_use"         | cache_control         | Ignored               |
| content | array, type = "tool_result"      | tool_use_id           | Fully Supported       |
| content | array, type = "tool_result"      | content               | Fully Supported       |
| content | array, type = "tool_result"      | cache_control         | Ignored               |
| content | array, type = "tool_result"      | is_error              | Ignored               |
| content | array, type = "server_tool_use"  |                       | Not Supported         |
| content | array, type = "web_search_tool_result" |                   | Not Supported         |
| content | array, type = "code_execution_tool_result" |               | Not Supported         |
| content | array, type = "mcp_tool_use"     |                       | Not Supported         |
| content | array, type = "mcp_tool_result"  |                       | Not Supported         |
| content | array, type = "container_upload" |                       | Not Supported         |