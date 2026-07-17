# 钛盘云端多用户版本设计

## 目标

将现有本地钛盘管理器部署到 `cloud.claudcode.xyz`，增加邀请码注册、账号登录、用户级钛盘 Key、用户隔离和服务器永久文件存储，同时保留本地单用户运行方式。

## 范围

第一版包含：

- 用户名和密码登录。
- 一次性邀请码注册。
- 管理员生成和撤销邀请码、查看及停用用户。
- 每用户加密保存一份钛盘 API Key。
- 钛盘临时文件上传、列表、下载、删除和直链管理。
- 云服务器永久文件上传、列表、下载和删除。
- 用户、文件和全站三层配额。
- HTTPS 部署、数据库备份和基础审计日志。

第一版不包含：

- 邮箱、短信、第三方登录和自助找回密码。
- 云端永久文件公开分享链接。
- 钛盘文件夹创建、删除和移动。
- 付费、套餐和自动扩容。

## 双模式

应用通过 `APP_MODE` 明确选择运行模式：

- `local`：默认值，保留现有 `127.0.0.1:8765` 单用户行为和本地设置文件。
- `cloud`：强制认证、数据库、用户隔离和服务器永久文件存储。

生产容器必须设置 `APP_MODE=cloud`。云模式缺少会话密钥、Key 加密主密钥、数据库路径或存储路径时拒绝启动。模式判断集中在应用启动配置中，路由不得自行猜测模式。

## 服务器架构

部署根目录：

```text
/home/ubuntu/tai-pan-cloud
```

持久数据：

```text
/home/ubuntu/tai-pan-cloud/data/app.db
/home/ubuntu/tai-pan-cloud/data/files/
/home/ubuntu/tai-pan-cloud/data/backups/
/home/ubuntu/tai-pan-cloud/data/secrets/
```

应用使用独立 Docker Compose，容器端口仅映射到 `127.0.0.1:18765`。不得加入、重启或修改 Wireless Debug 的 Compose 项目，不得复用其 PostgreSQL。

Nginx 为 `cloud.claudcode.xyz` 增加独立站点：

- HTTP 仅用于 ACME 和跳转 HTTPS。
- HTTPS 反向代理到 `127.0.0.1:18765`。
- `client_max_body_size 210m`，应用层严格限制文件内容为 200 MiB。
- 内部下载路径使用 `internal` location 和 `X-Accel-Redirect`，由 Nginx 发送大文件，Python 只负责鉴权。
- 修改前备份站点文件，执行 `nginx -t` 成功后才允许 reload。

DNS 部署门禁：公共 DNS 必须能将 `cloud.claudcode.xyz` 解析到 `43.153.137.20`，否则不申请证书、不切换线上入口。

## 数据库

使用 SQLite WAL，单应用进程。数据库访问封装在独立仓储层，所有写操作使用事务。

核心表：

### users

- `id`：随机 UUID。
- `username`：规范化后唯一，长度 3 到 32，只允许字母、数字、下划线和连字符。
- `password_hash`：Argon2id。
- `role`：`admin` 或 `user`。
- `status`：`active` 或 `disabled`。
- `must_change_password`：初始管理员首次登录为真。
- `created_at`、`updated_at`、`last_login_at`。

### invitations

- `id`、`code_hash`、`created_by`、`created_at`。
- `expires_at` 可为空。
- `used_by`、`used_at` 可为空。
- 数据库事务中校验并消费，确保一个邀请码只能成功注册一次。

### sessions

- `id`、`user_id`、`token_hash`、`csrf_hash`。
- `created_at`、`expires_at`、`last_seen_at`、`revoked_at`。
- Cookie 只保存随机明文令牌，数据库只保存 SHA-256 哈希。

### user_settings

- `user_id` 唯一。
- `encrypted_tmp_key`：使用服务器主密钥进行认证加密。
- `custom_domain`：默认 `pan.cloudcode.xyz`。
- `updated_at`。

### cloud_files

- `id`：随机 UUID，用作磁盘文件名。
- `user_id`、`original_name`、`content_type`、`size_bytes`。
- `storage_path`：仅保存受控相对路径，不接受用户输入路径。
- `sha256`、`created_at`。

### automatic_download_links

- `id`、`user_id`、`ukey`、`dkey`、`link`、`expires_at`。
- DKEY 按用户隔离并唯一。
- 用于复用和隐藏自动下载直链；用户主动创建的直链不写入该表。

### audit_events

- `id`、`user_id` 可为空、`event_type`、`target_type`、`target_id`、`created_at`。
- 只记录安全和操作元数据，不记录密码、会话令牌、API Key 或完整下载 URL。

## 认证与授权

密码使用 Argon2id。登录成功生成 256 位随机会话令牌和独立 CSRF 令牌。

Cookie 属性：

```text
HttpOnly; Secure; SameSite=Lax; Path=/
```

所有状态修改 API 必须同时满足：

- 有效会话。
- 用户状态为 `active`。
- `Origin` 与站点域名一致。
- `X-CSRF-Token` 与当前会话匹配。

权限规则：

- 普通用户只能读取和修改自己的设置、文件、直链及会话。
- 文件查询和删除必须同时使用文件 ID 与当前 `user_id`。
- 管理员不能查看用户的明文钛盘 Key。
- 停用用户时撤销其全部会话，但不自动删除文件。

限速：

- 同一账号或 IP 在 15 分钟内登录失败 5 次后暂时拒绝。
- 同一 IP 每小时最多提交 10 次注册请求。
- 邀请码只存哈希，明文只在创建成功时展示一次。

## 管理员引导

部署时通过容器内管理命令创建第一个管理员。命令生成随机临时密码，写入权限为 `0600` 的服务器凭据文件，并设置 `must_change_password=true`。临时密码不得写入 Git、Docker 镜像、Compose 文件、日志或聊天。

管理员登录后必须先修改密码，之后才能生成邀请码。管理页面提供：

- 生成一次性邀请码。
- 查看邀请码状态和撤销未使用邀请码。
- 查看用户、使用空间和状态。
- 停用或恢复普通用户。
- 触发普通用户密码重置并生成一次性临时密码。

## 钛盘 Key

每个用户对应一个钛盘 API Key。使用 `cryptography` 的 Fernet 认证加密，主密钥仅保存在服务器权限为 `0600` 的环境文件中。

- 保存新 Key 时加密后入库。
- API 仅返回 `key_configured`，永不回显 Key。
- 发起钛盘请求时在内存中短暂解密。
- 异常、日志、审计和管理员接口不得包含明文 Key。
- 清除 Key 时删除密文；没有 Key 的用户不能调用钛盘远端操作。

## 文件模型

文件页面统一展示两类记录，并明确标记来源：

- `钛盘`：由当前用户自己的 API Key 管理。
- `云端永久`：存储在本服务器，仅所有者可访问。

保存期限选项：

- `永久（云服务器）`。
- `24 小时（钛盘）`。
- `3 天（钛盘）`，默认。
- `7 天（钛盘）`。

### 云端永久上传

上传请求流式写入当前用户目录下的临时文件，同时计算大小和 SHA-256。不得调用 `await file.read()` 一次性载入完整内容。

限制：

- 单文件内容最大 200 MiB。
- 每用户云端永久文件总量最大 1 GiB。
- 全站云端永久文件总量最大 15 GiB。
- 服务器根分区可用空间低于 8 GiB 时拒绝新的永久上传。

上传前检查配额，写入过程中再次执行单文件限制，最终入库时在事务中重新检查用户和全站配额。失败、断连或事务失败必须删除临时文件。最终文件名为 UUID，用户文件名只作为显示和下载名称。

### 云端永久下载

FastAPI 使用文件 ID 和当前用户 ID 鉴权。成功后返回 `X-Accel-Redirect` 和安全的 `Content-Disposition`，Nginx 从内部目录发送文件。浏览器保持管理页面不变并触发附件下载。

### 云端永久删除

删除时先在事务中标记记录，删除磁盘文件，再删除数据库记录。磁盘文件缺失时仍允许清理孤立元数据并写审计事件。不得根据用户提供的文件名拼接路径。

### 钛盘临时上传

Key 仅在服务器端使用。上传文件通过受限临时文件转发给钛盘，避免大文件常驻内存。沿用 TMP.link 官方 `model`：0、1、2；云模式不向 TMP.link 发送 99。

### 钛盘下载直链

自动下载直链按 `user_id + ukey` 复用，至少剩余 1 小时时才复用。自动 DKEY 从该用户的直链列表中隐藏。用户主动创建的直链正常展示。用户之间的自动直链记录不可共享。

## 前端

未登录时只显示登录和注册界面，不渲染管理数据。

登录后沿用当前工作型布局，并增加：

- 当前用户名和退出按钮。
- 文件来源标记与云端空间使用量。
- 设置页的钛盘 Key 状态。
- 首次登录强制修改密码页面。
- 管理员专属“管理”导航，普通用户完全不渲染。

移动端保持底部导航，所有文本和操作按钮不得横向溢出。危险删除继续使用明确确认弹窗。

## API 边界

主要接口：

```text
POST   /api/auth/register
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me
POST   /api/auth/change-password

GET    /api/settings
PUT    /api/settings
DELETE /api/settings/key
POST   /api/settings/test

GET    /api/files
POST   /api/uploads
POST   /api/files/{id-or-ukey}/download
DELETE /api/files/{id-or-ukey}

GET    /api/links
POST   /api/links
DELETE /api/links/{dkey}

GET    /api/admin/users
PATCH  /api/admin/users/{user_id}
POST   /api/admin/users/{user_id}/reset-password
GET    /api/admin/invitations
POST   /api/admin/invitations
DELETE /api/admin/invitations/{invitation_id}
```

云端文件使用 UUID，钛盘文件使用 UKEY。API 响应包含明确的 `source` 字段，后端根据来源选择处理器，不允许仅凭字符串形状猜测。

## 错误处理

- 未登录返回 401，权限不足或跨用户访问返回 403，不泄露目标是否存在。
- 配额超限返回 413 或 409，并返回可执行的中文提示。
- 钛盘远端业务错误保持当前翻译，不包含用户 Key。
- 磁盘、数据库和加密错误记录关联 ID，前端只显示安全摘要。
- 上传中断、重复提交和并发删除必须有确定结果，不遗留未登记文件。

## 备份与恢复

应用单进程每天使用 SQLite Backup API 创建一致性数据库备份，保留最近 7 份。备份写入独立目录并记录结果，不在备份中保存环境密钥文件。

永久文件不做跨服务器备份，界面和服务条款应明确云端永久表示不自动过期，不代表异地容灾。部署文档包含数据库恢复和文件目录校验步骤。

## 部署流程

1. 公共 DNS 验证 `cloud.claudcode.xyz -> 43.153.137.20`。
2. 只读检查服务器资源、端口和现有服务。
3. 在独立目录上传代码并生成权限受限的生产环境文件。
4. 构建并启动独立 Compose，检查本机健康接口。
5. 创建初始管理员。
6. 备份 Nginx 配置，新增站点，执行 `nginx -t`。
7. 申请并验证 TLS 证书，启用 HTTPS。
8. 执行注册、登录、Key、临时上传和永久文件的冒烟测试。
9. 检查其他 Compose 服务和现有域名保持正常。

## 测试要求

- 密码哈希、Key 加解密、会话和 CSRF 单元测试。
- 邀请码并发消费测试。
- 用户 A 无法读取、下载或删除用户 B 的任何数据。
- 管理员接口角色测试，管理员也不能读取明文 Key。
- 200 MiB 单文件、1 GiB 用户、15 GiB 全站及 8 GiB 磁盘阈值测试。
- 上传中断和临时文件清理测试。
- 自动下载直链按用户复用和过滤测试。
- 本地模式现有测试全部继续通过。
- Playwright 覆盖注册、登录、强制改密、文件操作、退出和移动端布局。
- 部署后验证 HTTPS、Secure Cookie、容器重启持久化、数据库备份和 Nginx 内部下载。
