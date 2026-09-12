# BUPT UCloud Tasks for AstrBot

AstrBot integration for the BUPT Teaching Cloud API. It is an independent
Python implementation inspired by [YouXam/ucloud-bot](https://github.com/YouXam/ucloud-bot)
and [YouXam/ucloud](https://github.com/YouXam/ucloud), both GPL-3.0 licensed.
This plugin is distributed under GPL-3.0 as well; see the upstream projects for
the complete license text.

## Commands

- `/ucloud_login <学号> <统一认证密码>`: verify credentials and enable reminders.
- `/ucloud_tasks`: list unfinished tasks.
- `/ucloud_detail <作业ID>`: view homework details for a task ID from the list.
- `/ucloud_push`: toggle new-task reminders for the current chat.
- `/ucloud_logout`: remove locally stored credentials.

## Privacy and security

The UCloud API uses HTTP Basic Authentication. To support later task queries
and reminders, the plugin stores the account and password in
`data/plugin_data/astrbot_plugin_ucloud/accounts.json` on this AstrBot host.
Restrict filesystem access to the AstrBot host, and run `/ucloud_logout` when
you no longer want the credentials retained. Passwords are never written to
plugin logs or the WebUI configuration.

The default API endpoint is `https://ucloud.youxam.workers.dev`, maintained by
the upstream project. You can replace it in the plugin configuration if you
self-host the compatible API.

## Scope

This first version covers login, task lists, task details, and new-task
notifications. It intentionally does not submit coursework: submission may
upload files and is an irreversible action, so it should be designed and
enabled explicitly in a later version.
