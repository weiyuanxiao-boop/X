# LLM Proxy 服务管理脚本

## proxy.ps1

用于启动、停止和管理 LLM Proxy 服务的后台运行。

### 使用方法

```powershell
# 查看帮助
.\proxy.ps1 -help

# 查看服务状态
.\proxy.ps1 status

# 启动服务（默认端口 4936）
.\proxy.ps1 start

# 启动服务（指定端口）
.\proxy.ps1 start -Port 8080

# 停止服务
.\proxy.ps1 stop

# 重启服务
.\proxy.ps1 restart

# 查看日志（最后 50 行）
.\proxy.ps1 logs

# 查看可用模型列表
.\proxy.ps1 models

# 安装为 Windows 服务（需要 NSSM）
.\proxy.ps1 install

# 卸载 Windows 服务
.\proxy.ps1 uninstall
```

### 命令说明

| 命令 | 说明 |
|------|------|
| `start` | 后台启动代理服务 |
| `stop` | 停止代理服务 |
| `restart` | 重启代理服务 |
| `status` | 显示服务状态（运行/停止） |
| `logs` | 显示最后 50 行日志 |
| `models` | 显示可用模型列表（包含别名） |
| `set-alias` | 设置别名（添加或更新，需要 -Alias 和 -Target） |
| `remove-alias` | 删除别名（需要 -Alias） |
| `install` | 安装为 Windows 服务（需要 NSSM） |
| `uninstall` | 卸载 Windows 服务 |

### 选项

| 选项 | 说明 | 默认值 |
|------|------|--------|
| `-Port <n>` | 服务器端口 | 4936 |
| `-help` | 显示帮助信息 | - |

### 文件说明

- **PID 文件**: `scripts\llm_proxy.pid` - 存储后台进程 ID
- **日志文件**: `scripts\llm_proxy.log` - 服务运行日志

### 示例

#### 1. 启动服务

```powershell
.\proxy.ps1 start
```

输出：
```
[INFO] Starting LLM Proxy Gateway on port 4936...
[INFO] LLM Proxy Gateway started (PID: 12345)
Logs: D:\ProjectsMy\X\scripts\llm_proxy.log
```

#### 2. 查看状态

```powershell
.\proxy.ps1 status
```

输出：
```
LLM Proxy Gateway Status
===================
  Status:  Running
  PID:     12345
  Port:    4936
  Memory:  45,678,912 bytes
  CPU:     12.34 s

Commands:
  .\proxy.ps1 stop     - Stop the proxy
  .\proxy.ps1 restart  - Restart the proxy
  .\proxy.ps1 logs     - View logs
  .\proxy.ps1 models   - Show available models
```

#### 3. 查看可用模型

```powershell
.\proxy.ps1 models
```

输出：
```
Fetching model list from http://localhost:4936/v1/models...

Available Models:
=================
  - deepseek-v4-flash-openai (OpenAI)
  - deepseek-v4-flash-anthropic (Anthropic)
  - qwen3.6-plus (OpenAI, Anthropic)
  - cc-coder (OpenAI, Anthropic) [alias of qwen3.6-plus]

Total: 4 model(s)
```

#### 4. 管理别名

```powershell
# 设置别名（添加或更新）
.\proxy.ps1 set-alias -Alias my-ai -Target gpt-4o

# 删除别名
.\proxy.ps1 remove-alias -Alias my-ai

# 查看别名（通过 models 命令）
.\proxy.ps1 models
```

注意：
- 通过 API 设置的别名是临时的，重启后会丢失
- 要永久保存，请编辑 `model_config.yaml` 并重启服务
- 使用 `models` 命令可以查看所有模型和别名

#### 5. 查看日志

```powershell
.\proxy.ps1 logs
```

#### 5. 停止服务

```powershell
.\proxy.ps1 stop
```

#### 6. 重启服务

```powershell
.\proxy.ps1 restart
```

### 作为 Windows 服务安装

如果需要代理开机自动启动，可以安装为 Windows 服务：

1. 下载 NSSM (Non-Sucking Service Manager): https://nssm.cc/

2. 运行安装命令：
   ```powershell
   .\proxy.ps1 install
   ```

3. 按照提示使用 NSSM 安装：
   ```cmd
   nssm install LLMProxy
   nssm set LLMProxy Application "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
   nssm set LLMProxy Arguments "-ExecutionPolicy Bypass -File D:\ProjectsMy\X\scripts\llm_proxy_service.ps1"
   nssm start LLMProxy
   ```

4. 卸载服务：
   ```powershell
   .\proxy.ps1 uninstall
   nssm remove LLMProxy confirm
   ```

### 注意事项

1. **端口占用**: 如果启动失败，检查端口是否被占用
   ```powershell
   netstat -ano | findstr :4936
   ```

2. **防火墙**: 确保防火墙允许 4936 端口

3. **日志**: 启动问题请查看 `llm_proxy.log` 文件

4. **权限**: 某些操作可能需要管理员权限

5. **模型列表**: `models` 命令需要代理服务正在运行才能获取模型列表
