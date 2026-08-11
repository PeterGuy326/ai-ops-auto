#!/usr/bin/env bash
# Legacy UI seed：只让 Dashboard 不空白，不是审核/发布/指标闭环或真平台证据。
# 要运行可验证的离线价值链请使用 `ai-ops demo`。

set -euo pipefail
API="${API:-http://127.0.0.1:8000}"
: "${API_KEY:?请通过环境变量 API_KEY 提供与控制面一致的 legacy 管理 key}"

echo "⚠️  legacy UI seed：只写占位数据；完整离线闭环请运行 ai-ops demo"

post_json() {
  # Feed the credential through stdin so it is not exposed in curl's argv.
  printf 'X-API-Key: %s\n' "$API_KEY" | curl -fsS -X POST "$API$1" \
    -H 'Content-Type: application/json' \
    -H @- \
    -d "$2"
}

response_id() {
  python -c 'import json, sys; value = json.load(sys.stdin).get("id"); isinstance(value, int) or sys.exit("response has no id"); print(value)'
}

show_response() {
  printf '%.200s\n' "$1"
}

echo "▎创建主题"
topic_one="$(post_json /topics '{"name":"AI 运营自动化","keywords":["AI","自动化","Python"],"persona":{},"target_platforms":["xiaohongshu","zhihu"],"notes":"demo"}')"
topic_one_id="$(printf '%s' "$topic_one" | response_id)"
show_response "$topic_one"

topic_two="$(post_json /topics '{"name":"小红书爆款选题","keywords":["小红书","选题","内容"],"persona":{},"target_platforms":["xiaohongshu"],"notes":""}')"
topic_two_id="$(printf '%s' "$topic_two" | response_id)"
show_response "$topic_two"

topic_three="$(post_json /topics '{"name":"AI Agent 实战","keywords":["Claude","Agent","SDK"],"persona":{},"target_platforms":["zhihu","github_pages"],"notes":""}')"
topic_three_id="$(printf '%s' "$topic_three" | response_id)"
show_response "$topic_three"

topic_four="$(post_json /topics '{"name":"软件架构","keywords":["架构","DDD","微服务"],"persona":{},"target_platforms":["zhihu","toutiao"],"notes":""}')"
show_response "$topic_four"

echo
echo "▎创建文章"
article_one="$(post_json /articles "{\"topic_id\":$topic_one_id,\"title\":\"AI 运营自动化中台落地手记\",\"body\":\"...\",\"content_type\":\"long_article\",\"target_platforms\":[\"zhihu\",\"github_pages\"],\"target_account_ids\":[]}")"
show_response "$article_one"

article_two="$(post_json /articles "{\"topic_id\":$topic_two_id,\"title\":\"小红书爆款标题 10 条公式\",\"body\":\"...\",\"content_type\":\"image_text\",\"target_platforms\":[\"xiaohongshu\"],\"target_account_ids\":[]}")"
show_response "$article_two"

article_three="$(post_json /articles "{\"topic_id\":$topic_three_id,\"title\":\"用 Claude Code 半天搭一个发布中台\",\"body\":\"...\",\"content_type\":\"long_article\",\"target_platforms\":[\"zhihu\"],\"target_account_ids\":[]}")"
show_response "$article_three"

echo
echo "✅ 种子数据完成。打开 http://127.0.0.1:8000/ui 查看"
