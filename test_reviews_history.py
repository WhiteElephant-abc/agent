"""
测试 reviews_history 修复的测试脚本

测试场景：
1. 创建多个 review 批次
2. 在其中一个 review 中触发机器人
3. 验证 reviews_history 包含所有 review
4. 验证 review_comments_batch 只包含相关 review comments
"""

import sys
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TimelineItem:
    id: str
    type: str  # "review", "review_comment", "comment"
    user: str
    body: str = ""
    state: Optional[str] = None  # for review
    review_id: Optional[str] = None  # for review_comment
    path: Optional[str] = None  # for review_comment
    diff_hunk: Optional[str] = None  # for review_comment
    created_at: Optional[str] = None


@dataclass
class TriggerNode:
    id: str
    type: str  # "review", "review_comment", "comment"
    review_id: Optional[str] = None


@dataclass
class Context:
    comments_history: List[dict] = field(default_factory=list)
    reviews_history: List[dict] = field(default_factory=list)
    review_comments_batch: List[dict] = field(default_factory=list)
    is_truncated: bool = False


def build_rich_context(timeline_items: List[TimelineItem], trigger_node: Optional[TriggerNode] = None) -> Context:
    """简化版的 build_rich_context 用于测试"""
    context = Context()
    
    # 获取触发类型
    trigger_type = trigger_node.type if trigger_node else None
    
    # reviews_history: 始终保留所有 review 批次（完整历史）
    for item in timeline_items:
        if item.type == "review":
            context.reviews_history.append({
                "id": item.id,
                "user": item.user,
                "body": item.body,
                "state": item.state,
                "submitted_at": item.created_at
            })
            print(f"Including review {item.id} in reviews_history")
    
    # review批次使用原始timeline_items确保完整保留（不截断）
    if trigger_type in ["review", "review_comment"]:
        # review/review_comment触发：保留所有review批次，同时精确过滤review comments
        trigger_review_id = trigger_node.review_id if trigger_node.review_id else trigger_node.id
        
        for item in timeline_items:
            # review_comments_batch: 只保留与当前触发 review 相关的 review comments
            if item.type == "review_comment" and item.review_id and item.review_id == trigger_review_id:
                context.review_comments_batch.append({
                    "id": item.id,
                    "user": item.user,
                    "body": item.body,
                    "path": item.path,
                    "diff_hunk": item.diff_hunk
                })
                print(f"Including review comment {item.id} for review {trigger_review_id}")
    else:
        # comment触发：使用truncated_items处理评论
        for item in timeline_items:
            if item.type == "comment":
                context.comments_history.append({
                    "id": item.id,
                    "user": item.user,
                    "body": item.body,
                    "created_at": item.created_at,
                    "type": item.type
                })
        
        # review_comments_batch: 只保留最新批次的 review comments
        # 找到最新一次 review 的 ID（按时间顺序，最后一个是最新）
        latest_review_id = None
        for item in timeline_items:
            if item.type == "review":
                latest_review_id = item.id
        
        # 只保留与最新 review 相关的 review comments
        if latest_review_id:
            for item in timeline_items:
                if item.type == "review_comment" and item.review_id == latest_review_id:
                    context.review_comments_batch.append({
                        "id": item.id,
                        "user": item.user,
                        "body": item.body,
                        "path": item.path,
                        "diff_hunk": item.diff_hunk
                    })
    
    return context


def test_review_trigger_with_multiple_reviews():
    """测试：在 review 触发时，reviews_history 应包含所有 review"""
    print("\n=== 测试 1: review 触发，多个 review 批次 ===")
    
    # 创建时间线：3个 review + 一些 review comments
    timeline_items = [
        TimelineItem(id="PRR_1", type="review", user="user1", body="First review", state="COMMENTED", created_at="2026-01-29T01:00:00Z"),
        TimelineItem(id="RC_1", type="review_comment", user="user1", body="Comment on first review", review_id="PRR_1", path="file1.py", diff_hunk="@@ -1 +1 @@"),
        TimelineItem(id="PRR_2", type="review", user="user2", body="Second review", state="COMMENTED", created_at="2026-01-29T02:00:00Z"),
        TimelineItem(id="RC_2", type="review_comment", user="user2", body="Comment on second review", review_id="PRR_2", path="file2.py", diff_hunk="@@ -1 +1 @@"),
        TimelineItem(id="PRR_3", type="review", user="user3", body="Third review (trigger)", state="COMMENTED", created_at="2026-01-29T03:00:00Z"),
        TimelineItem(id="RC_3", type="review_comment", user="user3", body="Comment on third review", review_id="PRR_3", path="file3.py", diff_hunk="@@ -1 +1 @@"),
    ]
    
    # 在第三个 review 上触发
    trigger_node = TriggerNode(id="PRR_3", type="review")
    
    context = build_rich_context(timeline_items, trigger_node)
    
    print(f"\n结果:")
    print(f"  reviews_history 数量: {len(context.reviews_history)}")
    for r in context.reviews_history:
        print(f"    - {r['id']}: {r['body']}")
    
    print(f"  review_comments_batch 数量: {len(context.review_comments_batch)}")
    for rc in context.review_comments_batch:
        print(f"    - {rc['id']}: {rc['body']}")
    
    # 验证
    assert len(context.reviews_history) == 3, f"Expected 3 reviews in history, got {len(context.reviews_history)}"
    assert len(context.review_comments_batch) == 1, f"Expected 1 review comment in batch, got {len(context.review_comments_batch)}"
    assert context.review_comments_batch[0]["id"] == "RC_3", "Should only include comment from triggered review"
    
    print("\n✅ 测试通过：reviews_history 包含所有 3 个 review")
    print("✅ 测试通过：review_comments_batch 只包含触发 review 的评论")


def test_comment_trigger_with_multiple_reviews():
    """测试：在 comment 触发时，reviews_history 应包含所有 review"""
    print("\n=== 测试 2: comment 触发，多个 review 批次 ===")
    
    # 创建时间线：3个 review + 一些 review comments + 普通评论
    timeline_items = [
        TimelineItem(id="PRR_1", type="review", user="user1", body="First review", state="COMMENTED", created_at="2026-01-29T01:00:00Z"),
        TimelineItem(id="RC_1", type="review_comment", user="user1", body="Comment on first review", review_id="PRR_1", path="file1.py", diff_hunk="@@ -1 +1 @@"),
        TimelineItem(id="C_1", type="comment", user="user2", body="Regular comment", created_at="2026-01-29T02:00:00Z"),
        TimelineItem(id="PRR_2", type="review", user="user3", body="Second review", state="COMMENTED", created_at="2026-01-29T03:00:00Z"),
        TimelineItem(id="RC_2", type="review_comment", user="user3", body="Comment on second review", review_id="PRR_2", path="file2.py", diff_hunk="@@ -1 +1 @@"),
        TimelineItem(id="C_2", type="comment", user="user1", body="Another regular comment", created_at="2026-01-29T04:00:00Z"),
        TimelineItem(id="PRR_3", type="review", user="user4", body="Third review (latest)", state="COMMENTED", created_at="2026-01-29T05:00:00Z"),
        TimelineItem(id="RC_3", type="review_comment", user="user4", body="Comment on third review", review_id="PRR_3", path="file3.py", diff_hunk="@@ -1 +1 @@"),
    ]
    
    # 在普通评论上触发
    trigger_node = TriggerNode(id="C_2", type="comment")
    
    context = build_rich_context(timeline_items, trigger_node)
    
    print(f"\n结果:")
    print(f"  reviews_history 数量: {len(context.reviews_history)}")
    for r in context.reviews_history:
        print(f"    - {r['id']}: {r['body']}")
    
    print(f"  comments_history 数量: {len(context.comments_history)}")
    for c in context.comments_history:
        print(f"    - {c['id']}: {c['body']}")
    
    print(f"  review_comments_batch 数量: {len(context.review_comments_batch)}")
    for rc in context.review_comments_batch:
        print(f"    - {rc['id']}: {rc['body']}")
    
    # 验证
    assert len(context.reviews_history) == 3, f"Expected 3 reviews in history, got {len(context.reviews_history)}"
    assert len(context.comments_history) == 2, f"Expected 2 comments in history, got {len(context.comments_history)}"
    assert len(context.review_comments_batch) == 1, f"Expected 1 review comment in batch, got {len(context.review_comments_batch)}"
    assert context.review_comments_batch[0]["id"] == "RC_3", "Should only include comment from latest review"
    
    print("\n✅ 测试通过：reviews_history 包含所有 3 个 review")
    print("✅ 测试通过：comments_history 包含所有 2 个普通评论")
    print("✅ 测试通过：review_comments_batch 只包含最新 review 的评论")


if __name__ == "__main__":
    try:
        test_review_trigger_with_multiple_reviews()
        test_comment_trigger_with_multiple_reviews()
        print("\n" + "="*50)
        print("所有测试通过！✅")
        print("="*50)
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
