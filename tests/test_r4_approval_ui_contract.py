from pathlib import Path

from nika_core.ui.shell import index_path


def test_html_exposes_semantic_explicit_human_approval_region() -> None:
    html = index_path().read_text(encoding="utf-8")
    assert 'id="approvals-heading"' in html
    assert 'id="approvals-empty" role="status" aria-live="polite"' in html
    assert 'id="approvals-list"' in html
    assert 'aria-label="Дії, що очікують людського підтвердження"' in html
    assert "Підтверджуйте лише ту дію, параметри якої ви перевірили" in html
    assert "не має стандартної глобальної комбінації клавіш" in html


def test_javascript_sends_only_request_id_for_approval_decision() -> None:
    script = index_path().with_name("app.js").read_text(encoding="utf-8")
    assert 'approve.dataset.actionId = "approval.approve"' in script
    assert 'deny.dataset.actionId = "approval.deny"' in script
    assert "approve.dataset.approvalRequestId = item.request_id" in script
    assert "deny.dataset.approvalRequestId = item.request_id" in script
    assert "payload.request_id = approvalRequestId" in script
    assert 'candidate.scope === "app"' in script
    assert 'action.scope === "explicit"' in script
    assert "payload.target =" not in script
    assert "payload.tool_id =" not in script
    assert "payload.signature =" not in script


def test_windows_composition_root_keeps_approval_actions_explicit_only() -> None:
    script_path = Path(__file__).parents[1] / "scripts" / "nika_windows.py"
    script = script_path.read_text(encoding="utf-8")
    assert 'action_id="approval.approve"' in script
    assert 'action_id="approval.deny"' in script
    assert script.count('scope="explicit"') == 2
    assert script.count("default_binding=None") >= 2
    assert '"approval.approve": backend.approve_action' in script
    assert '"approval.deny": backend.deny_action' in script
    assert "approval_authority = ApprovalAuthority()" in script
