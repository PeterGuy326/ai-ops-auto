#!/usr/bin/env bash
# 塞种子数据让 Dashboard 不空白（用 FastAPI HTTP 接口塞，验证端到端可写）。

set -uo pipefail
API="${API:-http://127.0.0.1:8000}"

post() {
  curl -sS -X POST "$API$1" -H 'Content-Type: application/json' -d "$2" | head -c 200
  echo
}

echo "▎创建主题"
post /topics '{"name":"AI 运营自动化","keywords":["AI","自动化","Python"],"persona":{},"target_platforms":["xiaohongshu","zhihu"],"notes":"demo"}'
post /topics '{"name":"小红书爆款选题","keywords":["小红书","选题","内容"],"persona":{},"target_platforms":["xiaohongshu"],"notes":""}'
post /topics '{"name":"AI Agent 实战","keywords":["Claude","Agent","SDK"],"persona":{},"target_platforms":["zhihu","github_pages"],"notes":""}'
post /topics '{"name":"软件架构","keywords":["架构","DDD","微服务"],"persona":{},"target_platforms":["zhihu","toutiao"],"notes":""}'

echo
echo "▎创建文章"
post /articles '{"topic_id":1,"title":"AI 运营自动化中台落地手记","body":"...","content_type":"long_article","target_platforms":["zhihu","github_pages"],"target_account_ids":[]}'
post /articles '{"topic_id":2,"title":"小红书爆款标题 10 条公式","body":"...","content_type":"image_text","target_platforms":["xiaohongshu"],"target_account_ids":[]}'
post /articles '{"topic_id":3,"title":"用 Claude Code 半天搭一个发布中台","body":"...","content_type":"long_article","target_platforms":["zhihu"],"target_account_ids":[]}'

echo
echo "✅ 种子数据完成。打开 http://127.0.0.1:5173 查看"
