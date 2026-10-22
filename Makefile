SHELL := /bin/bash
ROOT := $(shell pwd)
VENV := $(ROOT)/.venv/bin

.PHONY: infra api worker web e2e status stop logs

infra: ## 启动 PostgreSQL + Redis（docker）
	cd infra && docker compose up -d
	@sleep 3 && docker compose -f infra/docker-compose.yml ps

api: ## 启动 FastAPI (127.0.0.1:8300)
	cd apps/api && $(VENV)/uvicorn app.main:app --host 127.0.0.1 --port 8300

worker: ## 启动 RQ 渲染 worker
	cd apps/api && $(VENV)/python worker.py

web: ## 启动 Next.js 控制台 (127.0.0.1:3300)
	cd apps/web && npx next dev -p 3300

e2e: ## 本地端到端验证（不经 API/DB：分镜→TTS→组装→check→render）
	$(VENV)/python scripts/e2e_local.py

status: ## 查看服务状态
	@echo "── docker ──"; docker compose -f infra/docker-compose.yml ps 2>/dev/null
	@echo "── 进程 ──"; pgrep -af "uvicorn app.main|worker.py|next dev" | grep -v pgrep || echo "(无)"

logs:
	@tail -f /tmp/opencode/api.log /tmp/opencode/worker2.log /tmp/opencode/web.log 2>/dev/null

stop: ## 停止后台服务（不动 docker 数据）
	@pids=$$(pgrep -f "uvicorn app.main:app --host 127.0.0.1 --port 8300"); [ -n "$$pids" ] && kill $$pids 2>/dev/null; \
	pids=$$(pgrep -f "venv/bin/python worker.py"); [ -n "$$pids" ] && kill $$pids 2>/dev/null; \
	pids=$$(pgrep -f "next dev -p 3300"); [ -n "$$pids" ] && kill $$pids 2>/dev/null; \
	pids=$$(pgrep -f "next-server"); [ -n "$$pids" ] && kill $$pids 2>/dev/null; \
	echo "stopped（docker: cd infra && docker compose stop）"
