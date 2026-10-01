#!/usr/bin/env bash
# 本轮用法端到端示例: 会务方发布面向作者的匿名反馈快照
#   发布分配 -> 两名评审人确认并各交一份正式评语 -> 会务方携带当前发布序号发布快照 ->
#   持码者凭随机访问码读取 (仅论文编号 + 顺序固定的两份评分/评语) ->
#   相同请求幂等返回原码 -> 序号过期/资料不齐一律拒绝且不留新版本 ->
#   补位发布推进序号后旧码立即失效, 重新发布产生新版本, 旧版本仅供会务方追溯
# 前置: 建议对新库运行 (脚本会自行建数据; 若编号已存在可忽略 409)。
# 用法: ./examples/snapshots.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 4 名不同机构评审人 + 2 篇论文 =="
for spec in 'R1 r1-secret Inst-A' 'R2 r2-secret Inst-B' 'R3 r3-secret Inst-C' 'R4 r4-secret Inst-D'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"$3\", \"capacity\": 3, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]}'
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-Y"]}'

echo "== 2. 预演 + 发布分配 (记下发布序号 serial 与两名评审人) =="
REV=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
SER=$(echo "$PUB" | j 'd["serial"]')
RA=$(echo "$PUB" | j 'd["plan"]["P1"][0]')
RB=$(echo "$PUB" | j 'd["plan"]["P1"][1]')
CA="r$(echo "$RA" | sed 's/R//')-secret"
CB="r$(echo "$RB" | sed 's/R//')-secret"
echo "P1 (serial=$SER) 分配: $RA, $RB"

echo "== 3. 两名评审人均确认, 并各提交一份正式评分与评语 =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RA" -H "X-Reviewer-Credential: $CA" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 4, \"comment\": \"选题重要, 建议补充消融实验。\"}"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER, \"score\": 2, \"comment\": \"实验设计有漏洞, 结论偏强。\"}"

echo "== 4. 资料不齐时发布被拒 (演示: P2 两名评审人均未确认 -> 409, 不留新版本) =="
# P2 在同一发布版中也有两名评审人, 不做任何确认/评语 -> 发布快照 409
curl -s -w '\n-> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P2/feedback-snapshots" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"serial\": $SER}"

echo "== 5. 会务方携带当前发布序号发布 P1 匿名反馈快照 (返回随机访问码) =="
SNAP=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"paper_id\": \"P1\", \"serial\": $SER}")
echo "$SNAP"
CODE=$(echo "$SNAP" | j 'd["access_code"]')
echo "访问码: $CODE"
echo "(响应仅含论文编号与按 1、2 固定排序的评分/评语, 无评审人编号、机构、收据、内部诊断)"

echo "== 6. 相同发布序号 + 两份评语收据重复请求 -> 原快照、原访问码 (created=false) =="
curl -s -X POST "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"paper_id\": \"P1\", \"serial\": $SER}" \
  | j '{"created": d["created"], "same_code": (d["access_code"] == "'"$CODE"'")}'

echo "== 7. 序号过期/超前 -> 409, 且不产生新版本 =="
curl -s -o /dev/null -w '超前序号        -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/feedback-snapshots" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $((SER + 10))}"

echo "== 8. 会务方追溯: P1 的全部快照版本 (含访问码、active 标记) =="
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG"; echo

echo "== 9. 持码者读取当前有效快照 (无需会务方/评审人凭据) =="
curl -s "$BASE/feedback/$CODE"; echo

echo "== 10. 无效码统一 404, 不透露论文是否存在 =="
for bad in "fbk-00000000000000000000000000000000" "not-a-code" "${CODE}xx"; do
  curl -s -w " -> HTTP %{http_code}\n" "$BASE/feedback/$bad"
done

echo "== 11. $RB 声明回避 -> 补位发布推进序号 -> 旧码立即失效 =="
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RB" -H "X-Reviewer-Credential: $CB" \
  -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "recuse", "reason": "近三年与第二作者合作"}'
DRY=$(curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG")
BP=$(curl -s -X POST "$BASE/assignment/backfill/publish" -H "$ORG" \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $(echo "$DRY" | j 'd["revision"]'), \"base_serial\": $(echo "$DRY" | j 'd["serial"]')}")
NSER=$(echo "$BP" | j 'd["serial"]')
echo "补位发布后 serial=$NSER"
curl -s -w '旧码读取        -> HTTP %{http_code}\n' "$BASE/feedback/$CODE"
curl -s -o /dev/null -w '用旧序号发布    -> HTTP %{http_code}\n' -X POST \
  "$BASE/papers/P1/feedback-snapshots" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $SER}"

echo "== 12. 补入评审人确认并交评语, 会务方按新序号发布新版本 (新访问码) =="
RC=$(echo "$BP" | j '[x for x in d["plan"]["P1"] if x != "'"$RA"'"][0]')
CC="r$(echo "$RC" | sed 's/R//')-secret"
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/decision" \
  -H "X-Reviewer-Id: $RC" -H "X-Reviewer-Credential: $CC" \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -X POST "$BASE/reviewer/assignments/P1/review" \
  -H "X-Reviewer-Id: $RC" -H "X-Reviewer-Credential: $CC" \
  -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P1\", \"serial\": $NSER, \"score\": 3, \"comment\": \"补位评审人的评语。\"}"
SNAP2=$(curl -s -X POST "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  -H 'Content-Type: application/json' -d "{\"paper_id\": \"P1\", \"serial\": $NSER}")
echo "$SNAP2"
CODE2=$(echo "$SNAP2" | j 'd["access_code"]')
echo "新版本访问码: $CODE2 (与旧码不同)"

echo "== 13. 新码可读, 旧码仍 404; 会务方可追溯新旧两版 (旧版 active=false) =="
curl -s -w '\n新码读取        -> HTTP %{http_code}\n' "$BASE/feedback/$CODE2"
curl -s -o /dev/null -w '旧码读取        -> HTTP %{http_code}\n' "$BASE/feedback/$CODE"
curl -s "$BASE/papers/P1/feedback-snapshots" -H "$ORG" \
  | j '{"current_serial": d["current_serial"], "versions": [{"serial": s["serial"], "active": s["active"]} for s in d["snapshots"]]}'
