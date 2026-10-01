"""本轮功能测试: 面向作者的匿名反馈快照发布与持码读取。

覆盖需求:
- 会务方按论文发布快照, 请求携带当前发布序号; 仅当该版两名评审人均已确认
  且各提交一份正式评语时才能发布; 序号过期/超前或资料不齐一律拒绝, 且不留新版本;
- 相同发布序号及两份评语收据重复请求返回原快照与原随机访问码 (幂等);
  来源变化 (补位/重新发布导致序号变化) 后生成新版本, 旧版本仅供会务方追溯;
- 持码者只能读取当前有效快照: 补位/重新发布推进序号后旧码立即失效;
- 快照只展示论文编号与按固定顺序 1、2 标注的两份评分/评语, 不暴露评审人编号、
  机构、收据及内部诊断;
- 无效码统一 404, 不透露论文是否存在;
- 沿用现有会务方鉴权 (X-Organizer-Key)。
"""
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="review-snapshots-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["ORGANIZER_KEY"] = "test-organizer-key"

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

ORG = {"X-Organizer-Key": "test-organizer-key"}


def reviewer_headers(rid):
    return {"X-Reviewer-Id": rid, "X-Reviewer-Credential": f"cred-{rid}"}


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_for_tests()
    with db.write_txn() as conn:
        for table in (
            "papers", "reviewers", "published",
            "assignment_decisions", "reviewer_recusals", "submitted_reviews",
            "feedback_snapshots",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'revision'")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def add_reviewer(client, rid, institution=None, capacity=3, topics=None):
    return client.post(
        "/reviewers",
        headers=ORG,
        json={
            "reviewer_id": rid,
            "credential": f"cred-{rid}",
            "topics": topics if topics is not None else ["AI"],
            "institution": institution or f"Inst-{rid}",
            "capacity": capacity,
            "avoid_papers": [],
        },
    )


def add_paper(client, pid="P1", topics=None, institutions=None):
    return client.post(
        "/papers",
        headers=ORG,
        json={
            "paper_id": pid,
            "manuscript": f"manuscript-of-{pid}",
            "topics": topics if topics is not None else ["AI"],
            "institutions": institutions if institutions is not None else [f"Author-{pid}"],
        },
    )


def publish(client):
    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    r = client.post("/assignment/publish", headers=ORG, json={"base_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, pid, decision="confirm", reason=None):
    body = {"paper_id": pid, "decision": decision}
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/reviewer/assignments/{pid}/decision",
        headers=reviewer_headers(rid),
        json=body,
    )


def submit_review(client, rid, pid, serial, score, comment):
    return client.post(
        f"/reviewer/assignments/{pid}/review",
        headers=reviewer_headers(rid),
        json={"paper_id": pid, "serial": serial, "score": score, "comment": comment},
    )


def backfill_publish(client):
    dry = client.post("/assignment/backfill/dry-run", headers=ORG).json()
    assert dry["feasible"], dry
    r = client.post(
        "/assignment/backfill/publish",
        headers=ORG,
        json={"base_revision": dry["revision"], "base_serial": dry["serial"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def publish_snapshot(client, pid="P1", serial=None):
    return client.post(
        f"/papers/{pid}/feedback-snapshots",
        headers=ORG,
        json={"paper_id": pid, "serial": serial},
    )


def prepare_paper_with_two_reviews(client, pid="P1", reviewers=("R1", "R2", "R3", "R4")):
    """建论文 -> 发布 -> 两人确认 -> 各交一份评语, 返回 (pub, ra, rb, 两份评语响应)。"""
    for rid in reviewers:
        add_reviewer(client, rid)
    add_paper(client, pid)
    pub = publish(client)
    serial = pub["serial"]
    ra, rb = pub["plan"][pid]
    decide(client, ra, pid, "confirm")
    decide(client, rb, pid, "confirm")
    va = submit_review(client, ra, pid, serial, 4, "第一位评审人的评语").json()
    vb = submit_review(client, rb, pid, serial, 2, "第二位评审人的评语").json()
    return pub, ra, rb, va, vb


# ------------------------------------------------------------ 发布前置条件

def test_snapshot_requires_organizer_key(client):
    r = client.post(
        "/papers/P1/feedback-snapshots",
        json={"paper_id": "P1", "serial": 1},
    )
    assert r.status_code == 401
    r = client.post(
        "/papers/P1/feedback-snapshots",
        headers={"X-Organizer-Key": "wrong"},
        json={"paper_id": "P1", "serial": 1},
    )
    assert r.status_code == 401


def test_snapshot_publish_without_any_publication_is_404(client):
    add_reviewer(client, "R1")
    add_paper(client)
    r = publish_snapshot(client, serial=1)
    assert r.status_code == 404
    # 不留快照
    assert client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"] == []


def test_paper_not_in_current_plan_is_404(client):
    prepare_paper_with_two_reviews(client)
    r = publish_snapshot(client, pid="NOPE", serial=1)
    assert r.status_code == 404


def test_serial_must_match_current_serial(client):
    pub, _, _, _, _ = prepare_paper_with_two_reviews(client)
    serial = pub["serial"]
    # 超前序号 409 (过期序号的情形见补位/重新发布用例)
    for bad in (serial + 1, serial + 99):
        r = publish_snapshot(client, serial=bad)
        assert r.status_code == 409, bad
    # 拒绝不产生新版本
    hist = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert hist["snapshots"] == []


def test_both_reviewers_must_confirm_and_submit(client):
    for rid in ("R1", "R2", "R3", "R4"):
        add_reviewer(client, rid)
    add_paper(client)
    pub = publish(client)
    serial = pub["serial"]
    ra, rb = pub["plan"]["P1"]

    # 全部 pending -> 409
    r = publish_snapshot(client, serial=serial)
    assert r.status_code == 409
    assert r.json()["detail"]["incomplete_slots"] == [
        {"slot": 1, "state": "pending", "submitted": False},
        {"slot": 2, "state": "pending", "submitted": False},
    ]

    decide(client, ra, "P1", "confirm")
    submit_review(client, ra, "P1", serial, 4, "甲评语")
    # 只齐一份 -> 409, 且不留下新版本
    r = publish_snapshot(client, serial=serial)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert [s["slot"] for s in detail["incomplete_slots"]] == [2]
    assert client.get("/papers/P1/feedback-snapshots", headers=ORG).json()["snapshots"] == []

    decide(client, rb, "P1", "confirm")
    # 已确认但未交评语 -> 409
    r = publish_snapshot(client, serial=serial)
    assert r.status_code == 409
    assert r.json()["detail"]["incomplete_slots"] == [
        {"slot": 2, "state": "confirmed", "submitted": False}
    ]
    submit_review(client, rb, "P1", serial, 2, "乙评语")
    r = publish_snapshot(client, serial=serial)
    assert r.status_code == 200, r.text


def test_recused_slot_blocks_snapshot(client):
    pub, ra, rb, _, _ = prepare_paper_with_two_reviews(client)
    serial = pub["serial"]
    # 快照发布前 rb 改声明回避 -> 资料不齐
    decide(client, rb, "P1", "recuse", reason="利益冲突")
    r = publish_snapshot(client, serial=serial)
    assert r.status_code == 409
    assert r.json()["detail"]["incomplete_slots"] == [
        {"slot": 2, "state": "recused", "submitted": True}
    ]


def test_path_paper_id_must_match_body(client):
    prepare_paper_with_two_reviews(client)
    r = client.post(
        "/papers/P9/feedback-snapshots",
        headers=ORG,
        json={"paper_id": "P1", "serial": 1},
    )
    assert r.status_code == 422


def test_invalid_serial_shape_is_422(client):
    prepare_paper_with_two_reviews(client)
    r = client.post(
        "/papers/P1/feedback-snapshots",
        headers=ORG,
        json={"paper_id": "P1", "serial": 0},
    )
    assert r.status_code == 422


# ------------------------------------------------------------ 发布成功内容

def test_successful_publish_returns_code_and_anonymous_content(client):
    pub, ra, rb, va, vb = prepare_paper_with_two_reviews(client)
    r = publish_snapshot(client, serial=pub["serial"])
    assert r.status_code == 200
    j = r.json()
    assert j["created"] is True
    assert j["serial"] == pub["serial"]
    code = j["access_code"]
    assert code.startswith("fbk-") and len(code) == len("fbk-") + 32
    # 快照仅含论文编号与两份评分/评语, 按固定顺序 1、2 标注
    assert j["snapshot"] == {
        "paper_id": "P1",
        "reviews": [
            {"index": 1, "score": 4, "comment": "第一位评审人的评语"},
            {"index": 2, "score": 2, "comment": "第二位评审人的评语"},
        ],
    }
    # 发布响应不暴露评审人编号/机构/收据/内部诊断
    blob = r.text
    for leak in (ra, rb, "Inst-R", va["receipt"], vb["receipt"], "receipt", "diagnostic"):
        assert leak not in blob, leak


def test_repeated_publish_is_idempotent_same_code(client):
    pub, _, _, _, _ = prepare_paper_with_two_reviews(client)
    first = publish_snapshot(client, serial=pub["serial"]).json()
    again = publish_snapshot(client, serial=pub["serial"]).json()
    assert again["created"] is False
    assert again["access_code"] == first["access_code"]
    assert again["created_at"] == first["created_at"]
    assert again["snapshot"] == first["snapshot"]
    # 仍只有一个快照版本
    hist = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert len(hist["snapshots"]) == 1


# ------------------------------------------------------------ 持码读取

def test_code_holder_reads_current_snapshot_without_credentials(client):
    pub, _, _, _, _ = prepare_paper_with_two_reviews(client)
    code = publish_snapshot(client, serial=pub["serial"]).json()["access_code"]
    # 无需任何凭据; 即使带上错误会务方密钥也不影响
    r = client.get(f"/feedback/{code}", headers={"X-Organizer-Key": "wrong-key"})
    assert r.status_code == 200
    j = r.json()
    assert set(j.keys()) == {"paper_id", "reviews"}
    assert j["paper_id"] == "P1"
    assert [x["index"] for x in j["reviews"]] == [1, 2]
    assert [x["score"] for x in j["reviews"]] == [4, 2]
    blob = r.text
    for leak in ("reviewer_id", "institution", "receipt", "rvw-", "serial", "Inst-"):
        assert leak not in blob, leak


def test_invalid_codes_uniformly_404_and_no_existence_leak(client):
    pub, _, _, _, _ = prepare_paper_with_two_reviews(client)
    code = publish_snapshot(client, serial=pub["serial"]).json()["access_code"]
    invalid_codes = [
        "not-a-code",
        "fbk-00000000000000000000000000000000",  # 形态正确但不存在
        code[:-1],                                # 截断
        code + "x",                               # 多余字符
        "fbk-zzzz",                               # 非法字符
    ]
    for bad in invalid_codes:
        r = client.get(f"/feedback/{bad}")
        assert r.status_code == 404, bad
    # 不同无效码返回完全一致的响应体 (不区分论文是否存在)
    bodies = {client.get(f"/feedback/{bad}").text for bad in invalid_codes}
    assert len(bodies) == 1


# ------------------------------------------------------------ 序号变化: 旧码失效, 新版本

def test_backfill_advancing_serial_invalidates_old_code(client):
    pub, ra, rb, va, vb = prepare_paper_with_two_reviews(client)
    old_code = publish_snapshot(client, serial=pub["serial"]).json()["access_code"]
    assert client.get(f"/feedback/{old_code}").status_code == 200

    # rb 回避 -> 补位发布推进序号 (ra 的确认槽位及评语连续保留)
    decide(client, rb, "P1", "recuse", reason="近三年合作")
    out = backfill_publish(client)
    new_serial = out["serial"]
    assert new_serial == pub["serial"] + 1

    # 旧码立即失效; 旧请求序号再发布 409
    assert client.get(f"/feedback/{old_code}").status_code == 404
    assert publish_snapshot(client, serial=pub["serial"]).status_code == 409
    # 旧版本仍只有一份, 未因拒绝而新增
    hist = client.get("/papers/P1/feedback-snapshots", headers=ORG).json()
    assert len(hist["snapshots"]) == 1 and hist["snapshots"][0]["active"] is False

    # 补入评审人确认并交评语后, 按新序号发布出新版本、新访问码
    rc = next(x for x in out["plan"]["P1"] if x != ra)
    decide(client, rc, "P1", "confirm")
    new_review = submit_review(client, rc, "P1", new_serial, 3, "补位评审人的评语").json()
    r = publish_snapshot(client, serial=new_serial)
    assert r.status_code == 200
    j = r.json()
    assert j["created"] is True and j["access_code"] != old_code
    # 沿用的评语内容仍在第 1 位 (槽位顺序按方案对), 补入评语为第 2 位
    assert j["snapshot"]["reviews"] == [
        {"index": 1, "score": 4, "comment": "第一位评审人的评语"},
        {"index": 2, "score": 3, "comment": "补位评审人的评语"},
    ]
    new_code = j["access_code"]
    assert client.get(f"/feedback/{new_code}").status_code == 200
    assert client.get(f"/feedback/{old_code}").status_code == 404
    # 新快照不含评审人/收据信息
    assert rc not in r.text and new_review["receipt"] not in r.text


def test_normal_republish_invalidates_old_code(client):
    pub, ra, _, _, _ = prepare_paper_with_two_reviews(client)
    old_code = publish_snapshot(client, serial=pub["serial"]).json()["access_code"]

    rev = client.post("/assignment/dry-run", headers=ORG).json()["revision"]
    republished = client.post(
        "/assignment/publish", headers=ORG, json={"base_revision": rev}
    ).json()
    assert republished["serial"] == pub["serial"] + 1

    # 旧码立即失效 (新序号槽位全部 pending, 资料不齐也无法补发)
    assert client.get(f"/feedback/{old_code}").status_code == 404
    assert publish_snapshot(client, serial=pub["serial"]).status_code == 409
    assert publish_snapshot(client, serial=republished["serial"]).status_code == 409


# ------------------------------------------------------------ 会务方追溯

def test_organizer_trace_lists_all_versions_with_active_flag(client):
    pub, ra, rb, va, vb = prepare_paper_with_two_reviews(client)
    s1 = publish_snapshot(client, serial=pub["serial"]).json()

    decide(client, rb, "P1", "recuse", reason="冲突")
    out = backfill_publish(client)
    rc = next(x for x in out["plan"]["P1"] if x != ra)
    decide(client, rc, "P1", "confirm")
    submit_review(client, rc, "P1", out["serial"], 5, "新版本评语")
    s2 = publish_snapshot(client, serial=out["serial"]).json()

    r = client.get("/papers/P1/feedback-snapshots", headers=ORG)
    assert r.status_code == 200
    j = r.json()
    assert j["current_serial"] == out["serial"]
    assert [s["serial"] for s in j["snapshots"]] == [pub["serial"], out["serial"]]
    old, new = j["snapshots"]
    assert old["active"] is False and new["active"] is True
    # 追溯视图可查会务方内部信息 (评审人编号、收据、访问码)
    assert old["access_code"] == s1["access_code"]
    assert new["access_code"] == s2["access_code"]
    assert [x["reviewer_id"] for x in old["reviews"]] == [ra, rb]
    assert [x["receipt"] for x in old["reviews"]] == [va["receipt"], vb["receipt"]]
    assert [x["reviewer_id"] for x in new["reviews"]] == [ra, rc]


def test_trace_requires_organizer_key(client):
    prepare_paper_with_two_reviews(client)
    assert client.get("/papers/P1/feedback-snapshots").status_code == 401
    assert client.get(
        "/papers/P1/feedback-snapshots", headers={"X-Organizer-Key": "bad"}
    ).status_code == 401


def test_trace_unknown_paper_returns_empty_list(client):
    prepare_paper_with_two_reviews(client)
    r = client.get("/papers/NOPE/feedback-snapshots", headers=ORG)
    assert r.status_code == 200
    assert r.json()["snapshots"] == []


def test_snapshot_publish_does_not_bump_revision(client):
    pub, _, _, _, _ = prepare_paper_with_two_reviews(client)
    rev_before = client.get("/meta", headers=ORG).json()["revision"]
    publish_snapshot(client, serial=pub["serial"])
    publish_snapshot(client, serial=pub["serial"])  # 幂等再来一次
    assert client.get("/meta", headers=ORG).json()["revision"] == rev_before
