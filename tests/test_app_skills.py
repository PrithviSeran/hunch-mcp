from hunch.app_skills import document_editing_skill
from hunch.playbook import HUNCH_PLAYBOOK


def test_packaged_document_skill_is_in_shared_runtime_instructions():
    from hunch import server
    skill = document_editing_skill()
    assert skill and not skill.startswith('---')
    assert skill in HUNCH_PLAYBOOK
    assert skill in server.mcp._mcp_server.instructions
