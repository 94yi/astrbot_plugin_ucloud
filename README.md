# BUPT UCloud Tasks for AstrBot

AstrBot integration for the BUPT Teaching Cloud API. It follows the direct CAS
authentication and Teaching Cloud request flow used by
[YouXam/ucloud](https://github.com/YouXam/ucloud) and
[byrdocs/bupt-auth](https://github.com/byrdocs/bupt-auth). The staged diagnostics
and current-form compatibility were also checked against
[94yi/MYUCLOUD](https://github.com/94yi/MYUCLOUD), without adding its hosted
backend as a dependency. Requests go directly
to auth.bupt.edu.cn and apiucloud.bupt.edu.cn; no public Worker or proxy is used.

The protocol work is based on GPL-3.0 upstream projects, and this plugin remains
distributed under GPL-3.0. See those projects for their license notices and
source history.

## Commands

- `/ucloud_login <学号> <统一认证密码>`: verify credentials and enable reminders.
- `/ucloud_tasks`: list unfinished tasks with stable sequence numbers cached for 10 minutes.
- `/ucloud_detail <序号>`: view a task from the latest numbered list; use `id:作业ID` for a direct ID.
- `/ucloud_submit`: list unfinished assignments and accept a reply number within 3 minutes.
- `/ucloud_submit <序号> [正文]`: open a 30-minute draft from the latest list; use `id:作业ID` for a direct ID.
  Existing text and attachments are loaded for resubmission, matching the student page.
- `/ucloud_content <正文>`: replace draft text (`-` clears it; max 2000 chars).
- `/ucloud_attach`: enter mobile attachment mode, then send files/images in the next message (max 10).
- `/ucloud_remove <序号|全部>`: remove draft attachments.
- `/ucloud_preview`: review the exact payload and obtain a new one-time code.
- `/ucloud_confirm <确认码>`: recheck server state, upload attachments, and submit.
- `/ucloud_cancel`: discard the in-memory draft without uploading or submitting.
- `/ucloud_push`: toggle new-task reminders for the current chat.
- `/ucloud_logout`: remove locally stored credentials.

## 自然语言与本机下载（0.5.0）

已登录私聊可直接说“列出云邮课程”“查看高数课件”“下载刚才列表的第2个”。
`ucloud_learning_files` 工具支持课程、作业、详情、附件分页查询及下载交付；
课程名称不明确时先查询选择，不猜课程ID。工具不能上传或提交作业。

默认 `download_delivery_mode=direct` 配合 `proxy_enabled=true`：发送1小时有效的服务器流式转发链接。
填写 `proxy_public_base_url` 为电脑能访问的AstrBot管理页根地址，例如 `https://bot.example`。
浏览器先登录同一地址的管理页，再打开下载链接。复用管理页鉴权，不开放新的匿名端口。
服务器只在内存中分块转发，不落盘，最多2路并发；支持单段Range断点续传，
浏览器断开会关闭上游连接。每次请求重新检查账号、资源权限和元数据版本。
临时链接仅保存在内存，重启后失效，重新向机器人获取即可。
若前面配置了反向代理，需关闭代理响应缓存和磁盘缓冲；接口返回 `Cache-Control: no-store`
和 `X-Accel-Buffering: no`。服务端只做传输长度/范围与版本检查，不将完整文件写盘做内容校验。

设置 `proxy_enabled=false` 才直接发送官方 HTTPS 下载地址，需要电脑能访问校园网。
两种无缓存方式都由浏览器保存文件。在浏览器中选择
保存位置即可落到本机；服务器无法直接写入用户电脑任意文件夹，也不能确认用户已保存。
官方链接可能过期，可能需要校园网/VPN；失效后重新查询，不公开转发。
下载链接不会通过模型工具返回值交给模型转述。直链模式下的内容校验和本机去重
需要浏览器或本地下载助手实现，服务器没有读取文件，不能宣称已完成SHA-256校验。

可选 `download_delivery_mode=qq_file` 会先在服务器缓存并校验，再把文件发到当前
QQ私聊；此模式下载/发送期间短暂占用服务器存储，不满足完全零落盘要求。
成功路径不会向聊天输出流式下载、哈希和校验过程：先直接出现QQ文件消息，再按当前
会话选定的人设附带严格两句短回复；失败时仍显示必要错误。技术细节仅写入服务端日志。
`delete_after_qq_delivery=true`（默认）会在QQ成功回执持久化后校验并删除对应临时文件
及下载索引，保留小体积发送记录防止重复发送。回执不明或发送失败时保留副本；
清理失败会单独提示，不把已成功的发送说成失败。只清理本次交付对应的已索引文件，
不批量清理历史文件。QQ平台或客户端自身缓存不在此插件的清理范围。
`send_downloads_to_qq` 默认开启，仅影响此缓存模式。发送前持久化意图，成功回执后
不重复发送相同会话的相同内容；超时保留记录，不假称成功、不自动重发。
QQ接口成功也不等于文件已写入电脑，需在QQ客户端下载/另存为。

## 课件、教案与作业附件命令

在已登录的私聊使用：

- `/ucloud_courses`：查看当前课程及课程 ID。
- `/ucloud_files course:课程ID [页码]`：列出学生可见课程资源树中的文件附件，包括老师发布的课件、教案；不访问教师专属资料或外部网页链接。
- `/ucloud_files 作业序号` 或 `/ucloud_files id:作业ID`：列出作业附件。作业序号来自 `/ucloud_tasks`。
- `/ucloud_download 附件序号` 或 `/ucloud_download all`：下载最近一次附件列表的指定文件或本页全部文件。每页 50 项，列表有效期 10 分钟。

默认提供直链供电脑浏览器保存；可选QQ缓存模式才会生成服务器文件与SHA-256。两种模式均重新检查资源可见性与元数据，账号切换、过期列表、已撤回资源均拒绝交付，不会上传或提交作业。以下缓存、内容校验和容量规则仅适用于QQ缓存模式。

下载索引位于 `data/plugin_data/astrbot_plugin_ucloud/downloads/downloads.sqlite3`，文件位于其下的会话哈希目录。SQLite 的 `resources` 表包含账号/会话范围哈希、资源 ID、版本哈希、SHA-256、字节数与相对文件路径，不存账号密码、令牌或签名链接。

同一资源版本会先核对本地文件大小与 SHA-256，通过后直接复用，不请求文件下载地址。不同资源下载后若 SHA-256 相同，在同一账号/会话范围内复用同一份内容；跨账号或会话不共享副本。同名不同内容不会覆盖旧文件。若没有可信版本字段，重新获取内容后再去重，不能仅凭文件名或资源 ID 跳过网络下载。

校验包括 HTTP 传输长度、教学云 `fileSize`、本地 SHA-256、服务端提供的 SHA-256/MD5（若有）、PDF 首尾标记、ZIP/OOXML CRC 与基本格式。没有服务端校验和时，本地 SHA-256 用于检测后续损坏和去重，不等同于服务端内容真实性证明；格式校验也不是病毒扫描。无法校验的加密压缩包会报错，不假称成功。失败或取消只清理当前临时文件；已有源文件与旧副本不自动删除。

默认单文件 200 MiB，可配置 1–512 MiB；累计下载目录最多 2 GiB，保留 512 MiB 空闲。压缩包校验最多 10000 项、解压总量 512 MiB。串行下载和 SQLite 索引避免同一插件进程内并发请求重复下载。模块卸载会取消并等待下载任务完成清理。

下载仅接受已确认的官方 HTTPS 域名，重定向逐跳检查，不携带 CAS Cookie 或 API Token。当前部署官方 `fileucloud.bupt.edu.cn` 解析为校园节点 `10.3.19.2`，仅为这一精确域名/IP 组合允许内网访问；其它内网、环回、非标准端口均拒绝。校园 DNS 变化时需重新核实映射，不自动放宽规则。

## Account privacy

Direct CAS login requires the unified-authentication credentials. To support
token renewal, later task queries, and reminders, the plugin stores them in
`data/plugin_data/astrbot_plugin_ucloud/accounts.json` on this AstrBot host.
Restrict filesystem access to the AstrBot host, and run `/ucloud_logout` when
you no longer want the credentials retained. Passwords are never written to
plugin logs or the WebUI configuration.

The plugin keeps access tokens in memory only. It stores the rotating refresh
token in the same owner-only `0600` account file as the required CAS fallback
password, allowing restarts to resume through token refresh instead of repeating
a password login. If the refresh token is explicitly rejected, CAS is attempted
once; transient refresh outages never replay credentials. If CAS requests a
captcha, the plugin stops instead of sending the session cookie or captcha to an
external OCR service.

The login client reads CAS hidden fields from the current form instead of
assuming fixed values, validates that the service ticket returns only through
the expected HTTPS UCloud callback, and preserves the previously selected role
across restarts when it is still available. Authentication clients ignore
process proxy environment variables so credentials cannot be silently routed
through an unrelated proxy. Only idempotent GET requests receive one short
connection retry; credential submission and one-time ticket exchange are never
blindly replayed. Authentication failures carry a safe stage name for local
diagnostics without logging passwords, cookies, tickets, or tokens.

## Submission safety and compatibility

The submission flow follows the current UCloud student SPA: it reads
`/ykt-site/work/submit-view`, enforces the page's status/deadline rules and
attachment limits, uploads attachments with `bizType=3`, and posts the same
payload to `/ykt-site/work/submit`. Individual and group assignments are
supported when UCloud exposes the student's group ID.

Nothing is uploaded when a draft is created, edited, previewed, or cancelled.
The irreversible request only runs after `/ucloud_confirm` with the current
one-time code. Immediately before that request the plugin refreshes submission
state and refuses to proceed if the assignment changed in another browser.
