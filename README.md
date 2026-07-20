# 本地直链管理器

一个只监听 `127.0.0.1:8765` 的本地网页，用于上传 TMP.link 文件和管理直链。

## 启动

在 Windows 资源管理器中双击 `start.bat`。首次启动会在 WSL Ubuntu 中创建 `.venv` 并安装依赖，完成后自动打开：

```text
http://127.0.0.1:8765
```

手动启动：

```bash
cd /mnt/d/Users/sunqi39/Desktop/tmp_link_manager
bash scripts/start.sh
```

## 首次设置

打开“设置”，输入新的 API Key 和 `pan.cloudcode.xyz`，保存后点击“测试连接”。已保存的 Key 不会回显给浏览器。

API Key 以明文 JSON 保存在 `.local/settings.json`，该目录已被 Git 忽略。它适用于个人本地工具，但不是加密保险库。此前截图中出现的 Key 没有写入本项目。

## 功能

- 查询配额和服务状态。
- 批量选择、拖拽和顺序上传文件。
- 选择永久、24 小时、3 天或 7 天保存模式。
- 分页查看仓库文件并复制 UKEY。
- 创建、复制、打开和删除直链。
- 删除直链时可选择同时删除源文件。

前端图标来自本地保存的 Lucide 1.24.0，运行时不访问 CDN。

## 云端多用户部署

云端模式使用独立 SQLite 数据库、用户隔离、邀请码注册和服务器永久文件存储。生产部署固定使用 `APP_MODE=cloud`，只在宿主机的 `127.0.0.1:18765` 提供给 Nginx，不会复用其他项目的数据库或 Compose 服务。

部署前请完整阅读 [云端部署与运维手册](docs/cloud-deployment.md)。生产密钥必须由 `deploy/deploy.sh` 在服务器上生成，不得把 `.env`、初始管理员凭据或 API Key 写入 Git。

## 故障排查

- 浏览器显示拒绝连接：确认启动窗口仍在运行。
- 提示端口占用：关闭已有的 8765 服务后重新运行。
- 连接测试失败：检查 API Key、网络和钛盘服务状态。
- 域名链接异常：确认 `pan.cloudcode.xyz` 的 CNAME 和 HTTPS 已生效。

## 测试

```bash
.venv/bin/python -m pytest -q
```
