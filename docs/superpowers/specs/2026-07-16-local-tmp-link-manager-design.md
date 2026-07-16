# 本地钛盘文件与直链管理器设计

## 目标

在 `D:\Users\sunqi39\Desktop\tmp_link_manager` 创建一个只在本机运行的网页应用，通过钛盘服务端 API 完成文件上传、仓库文件查看、直链创建、直链查看和删除操作。应用应适合非开发者日常使用，启动后通过浏览器访问 `http://127.0.0.1:8765`。

## 范围

第一版包含：

1. 本地持久保存 API Key。
2. 测试 API 连接并查询已购买配额。
3. 拖拽、选择和批量上传文件。
4. 上传时选择永久、24 小时、3 天或 7 天存储模式。
5. 分页查看仓库文件。
6. 从文件 UKEY 创建直链。
7. 设置直链有效分钟数和下载次数限制。
8. 分页查看已有直链。
9. 复制直链。
10. 删除直链，并可选择同时删除源文件。
11. 使用 `pan.cloudcode.xyz` 作为显示和复制时的自定义域名。
12. 提供 Windows 双击启动脚本。

第一版不包含：

1. 多用户、登录和远程访问。
2. 多个钛盘账号或多个 API Key。
3. 文件内容预览、在线编辑或文件夹写操作。
4. 公网部署。
5. 自动购买容量或调用付费接口。

## 技术方案

使用 FastAPI 提供本地后端和静态网页。后端使用 HTTP 客户端调用钛盘 API；前端使用原生 HTML、CSS 和 JavaScript，减少构建依赖。图标使用本地静态 Lucide 资源，不依赖页面运行时访问 CDN。

应用仅绑定 `127.0.0.1:8765`，不监听 `0.0.0.0`。浏览器只与本地后端通信，远端 API Key 不进入前端 JavaScript、HTML、查询参数或浏览器存储。

## 项目结构

```text
tmp_link_manager/
├── app/
│   ├── main.py              FastAPI 路由和静态页面入口
│   ├── client.py            钛盘 API 客户端
│   ├── config.py            本地设置读取、保存和脱敏
│   ├── models.py            内部请求与响应模型
│   └── static/
│       ├── index.html
│       ├── app.css
│       ├── app.js
│       └── vendor/lucide.min.js
├── tests/
│   ├── test_config.py
│   ├── test_client.py
│   └── test_routes.py
├── .local/                  运行时创建，Git 忽略
│   └── settings.json
├── .gitignore
├── pyproject.toml
├── start.bat
└── README.md
```

## 页面设计

采用安静、工具型界面。固定左侧导航包含“概览、文件、直链、设置”，右侧为主要工作区。桌面优先，同时保证窄窗口不出现文本重叠。

### 概览

显示：

- API 连接状态。
- 已购买配额。
- 当前页仓库文件数量。
- 当前页直链数量。
- 自定义域名状态。

页面提供刷新命令，不自动高频轮询远端服务。

### 文件

顶部是稳定高度的上传区域，支持点击和拖拽多个文件。上传前统一选择存储模式，也允许从队列移除单个文件。上传队列显示文件名、大小、状态和失败原因。

仓库文件表显示服务端返回的 UKEY、名称和大小。每行提供复制 UKEY 和创建直链命令。分页由服务端 API 的 `page` 参数驱动。

### 直链

直链表显示 DKEY、名称、大小、链接和到期时间。链接支持复制和新窗口打开。删除按钮必须弹出确认对话框，并提供“同时删除源文件”复选框。

创建直链使用模态对话框，输入 UKEY、有效分钟数和下载次数。两个限制字段允许留空，表示使用服务默认值。

### 设置

设置页包含：

- API Key 输入框，默认不回显已保存内容。
- “已配置/未配置”状态。
- 自定义域名，默认 `pan.cloudcode.xyz`。
- 保存、测试连接和清除密钥命令。

保存后前端只收到 `key_configured: true`，不会收到完整或部分 API Key。

## API 映射

### 文件上传

远端请求：

```text
POST https://tmp-cli.vx-cdn.com/app/upload_cli
Content-Type: multipart/form-data
file=<文件>
key=<API Key>
model=99|0|1|2
```

存储模式映射：

- `99`：永久。
- `0`：24 小时后销毁。
- `1`：3 天。
- `2`：7 天。

### 直链管理

远端请求统一为：

```text
POST https://tmp-api.vx-cdn.com/services/direct
Content-Type: application/x-www-form-urlencoded
```

动作映射：

- 查询配额：`action=quota`、`key`。
- 获取仓库文件：`action=list_of_workspace`、`key`、可选 `page`。
- 获取直链：`action=list_of_direct`、`key`、可选 `page`。
- 创建直链：`action=link_add`、`key`、`ukey`、可选 `valid_time`、`download_limit`。
- 删除直链：`action=link_del`、`key`、`dkey`、可选 `delete`。

后端将第三方响应统一转换为本地 JSON：

```json
{
  "ok": true,
  "data": {},
  "message": ""
}
```

第三方字段结构若与文档不一致，后端保留原始 `data` 供适配层读取，但绝不将请求参数中的 API Key 返回给浏览器。

## 本地路由

```text
GET    /api/settings
PUT    /api/settings
DELETE /api/settings/key
POST   /api/settings/test
GET    /api/quota
POST   /api/uploads
GET    /api/files?page=1
GET    /api/links?page=1
POST   /api/links
DELETE /api/links/{dkey}?delete_file=false
```

文件上传采用 multipart 请求。第一版按队列逐个上传，前端展示“等待、上传中、完成、失败”状态。浏览器上传到本地后端的进度可准确展示；本地后端到钛盘的进度在第一版显示为处理中，不伪造百分比。

## 密钥与安全

API Key 保存在 `.local/settings.json`，目录加入 `.gitignore`。在 Linux/WSL 下保存后将文件权限设为 `0600`。Windows 挂载盘不能保证完全等同的 POSIX 权限，因此 README 明确说明这是本地明文凭据，不是加密保险库。

安全规则：

1. 代码、测试、日志和文档不得包含真实 API Key。
2. 后端日志不得打印表单内容或远端请求体。
3. 前端永远不能读取已保存 Key。
4. 设置更新时空 Key 表示保留现有 Key，清除必须使用独立命令。
5. 服务只绑定回环地址。
6. 文件名在 HTML 中按文本渲染，禁止作为 HTML 注入。

## 错误处理

后端区分：

- 未配置 API Key：HTTP 400。
- 远端拒绝或业务 `status != 1`：HTTP 502，并返回脱敏后的业务消息。
- 远端超时：HTTP 504。
- 无法连接远端：HTTP 502。
- 上传文件为空或存储模式非法：HTTP 422。
- 本地配置无法写入：HTTP 500。

前端在操作位置显示错误，并保留失败队列项供重试。删除、保存设置等命令期间禁用重复点击。网络错误不会清空当前列表。

## 启动方式

`start.bat` 负责：

1. 通过 WSL Ubuntu 进入项目目录。
2. 首次运行时创建 `.venv` 并安装项目依赖。
3. 启动 `127.0.0.1:8765`。
4. 等待健康检查成功后打开默认浏览器。

后端提供 `GET /health`，返回 `{"status":"ok"}`。若 8765 已被占用，启动脚本给出明确提示，不静默切换端口，避免浏览器打开错误服务。

## 测试与验收

自动测试覆盖：

1. API Key 保存、保留、清除和前端脱敏。
2. 日志和本地 API 响应不包含 Key。
3. 钛盘六类动作的请求参数映射。
4. 第三方成功、业务失败、超时和连接失败转换。
5. 上传模式校验和文件转发。
6. 设置、配额、文件列表、直链列表、创建与删除路由。
7. 删除时 `delete_file` 映射。
8. 静态页面和健康检查。

最终验收包括：

- 完整 pytest 通过。
- 本地服务在 127.0.0.1:8765 启动。
- Playwright 在桌面和移动宽度下检查四个页面无重叠。
- 使用测试替身验证上传、创建和删除全流程。
- 用户在设置页自行输入真实 Key 后执行一次连接测试；真实 Key 不由代码或对话预填。
