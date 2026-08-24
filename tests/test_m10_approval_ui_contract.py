from pathlib import Path

from nika_core.ui.shell import index_path


def test_html_exposes_semantic_explicit_human_decision_region() -> None:
    html = index_path().read_text(encoding="utf-8")
    assert 'id="approvals-heading"' in html
    assert 'id="approvals-empty" role="status" aria-live="polite"' in html
    assert 'id="approvals-list"' in html
    assert 'aria-label="Запити, що очікують людського рішення"' in html
    assert "Перед схваленням перевірте всі параметри" in html
    assert "не має глобальної комбінації" in html


def test_javascript_sends_only_opaque_request_id_for_human_decision() -> None:
    script = index_path().with_name("app.js").read_text(encoding="utf-8")
    assert '"approval.action.approve"' in script
    assert '"approval.action.deny"' in script
    assert '"approval.review.approve"' in script
    assert '"approval.review.deny"' in script
    assert "approve.dataset.approvalRequestId = item.request_id" in script
    assert "deny.dataset.approvalRequestId = item.request_id" in script
    assert "payload.request_id = approvalRequestId" in script
    assert 'candidate.scope === "app"' in script
    assert 'action.scope === "explicit"' in script
    assert "payload.signature =" not in script
    assert "payload.issuer_id =" not in script
    assert "payload.approved =" not in script
    assert "payload.secret =" not in script


def test_windows_composition_root_keeps_authority_actions_explicit_only() -> None:
    script_path = Path(__file__).parents[1] / "scripts" / "nika_windows.py"
    script = script_path.read_text(encoding="utf-8")
    for action_id in (
        "approval.action.approve",
        "approval.action.deny",
        "approval.review.approve",
        "approval.review.deny",
    ):
        assert action_id in script
    assert script.count('scope="explicit"') == 1
    assert "for action_id, label in" in script
    assert "default_binding=None" in script
    assert '"approval.action.approve": backend.approve_action' in script
    assert '"approval.review.approve": backend.approve_review' in script
    assert "approval_authority = ApprovalAuthority()" in script
