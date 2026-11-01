# 记账本

手机/电脑可用的每日记账 Web App（单页 HTML），支持 GitHub Gist 云同步。

- 在线地址：<https://sslishy.github.io/expense-tracker/>
- 同步 Gist（私有）：`5c4adbfa0ec1c294f3a63854ff998ee0`

## 云同步

在网页「工具 → 云同步」粘贴 **仅含 gist 权限** 的 GitHub Token 与 Gist ID。  
创建 Token：<https://github.com/settings/tokens/new?scopes=gist&description=expense-tracker>

Token 只保存在本机 `localStorage`（键名 `expense-tracker-sync-v1`），不会进入导出文件。

## CLI（助手记账）

```bash
python3 tools/ledger.py add --type expense --category 交通 --amount 30 --note 电车充电
python3 tools/ledger.py list
python3 tools/ledger.py summary --date 2026-11-01
python3 tools/ledger.py delete --id <entry-id>
python3 tools/ledger.py pull   # 刷新 /workspace/ledger/expenses.csv
```

需要本机已 `gh auth login`（gist 权限），或设置 `GITHUB_TOKEN`。
