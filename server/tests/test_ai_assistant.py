from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.ai_assistant import HubAssistantService
from app.services.fleet_inventory import UpdateWorkbenchEntry


def test_assistant_extracts_responses_output_text():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "First sentence."},
                    {"type": "output_text", "text": "Second sentence."},
                ],
            }
        ]
    }

    assert HubAssistantService._extract_output_text(payload) == "First sentence.\nSecond sentence."


def test_assistant_shows_plugin_updates_without_queuing_them_until_scope_is_explicit():
    answer = _assistant_service()._answer_supported_plugin_command(
        "Zeige alle Websites mit einem Smush Update",
        _smush_entries(),
        previous_site_ids=(),
        captured_at=datetime.now(UTC),
    )

    assert answer is not None
    assert len(answer.update_matches) == 3
    assert answer.action is None
    assert "3 gemeldete Updates" in answer.text


def test_assistant_queues_only_directly_ready_plugin_updates_for_explicit_all_sites_scope():
    answer = _assistant_service()._answer_supported_plugin_command(
        "Aktualisiere Smush auf allen Websites",
        _smush_entries(),
        previous_site_ids=(),
        captured_at=datetime.now(UTC),
    )

    assert answer is not None
    assert answer.action is not None
    assert answer.action.selected_keys == (
        "1|plugin|wp-smushit/wp-smush.php",
        "2|plugin|wp-smushit/wp-smush.php",
    )
    assert answer.action.skipped_count == 1


def test_assistant_reuses_the_previous_result_for_this_websites_scope():
    answer = _assistant_service()._answer_supported_plugin_command(
        "Aktualisiere Smush auf diesen Websites",
        _smush_entries(),
        previous_site_ids=(2,),
        captured_at=datetime.now(UTC),
    )

    assert answer is not None
    assert answer.action is not None
    assert answer.action.selected_keys == ("2|plugin|wp-smushit/wp-smush.php",)


def _assistant_service() -> HubAssistantService:
    return object.__new__(HubAssistantService)


def _smush_entries() -> list[UpdateWorkbenchEntry]:
    checked_at = datetime.now(UTC)
    return [
        _plugin_entry(site_id=1, domain="one.example", direct_ready=True, checked_at=checked_at),
        _plugin_entry(site_id=2, domain="two.example", direct_ready=True, checked_at=checked_at),
        _plugin_entry(site_id=3, domain="waiting.example", direct_ready=False, checked_at=checked_at),
    ]


def _plugin_entry(*, site_id: int, domain: str, direct_ready: bool, checked_at: datetime) -> UpdateWorkbenchEntry:
    site = SimpleNamespace(
        id=site_id,
        domain=domain,
    )
    return UpdateWorkbenchEntry(
        site=site,
        kind="plugin",
        name="Smush",
        identifier="wp-smushit/wp-smush.php",
        current_version="4.3.0",
        target_version="4.3.2",
        is_active=True,
        update_available=True,
        update_checked=True,
        execution_ready=direct_ready,
        execution_note="Provider package is unavailable." if not direct_ready else "",
        captured_at=checked_at,
    )
