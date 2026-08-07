# 川口市バドミントン予約システム

日常运维请看 **[使用说明.md](./使用说明.md)**（功能概要、输入输出、日志是否增长、如何运行）。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 按使用说明复制 local/*.example.json → 对应本体，并配置日历凭证
```

## 常用入口

```bash
python scripts/scan_daily.py
python scripts/lottery_scan.py
python scripts/auto_book.py
python scripts/sync_calendar.py
pytest -q
```

`data/` 与多数 `local/` 私密文件已 gitignore，勿提交密钥与密码。
