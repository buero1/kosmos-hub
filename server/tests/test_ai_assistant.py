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
    answer = _assistant_service()._answer_supported_update_command(
        "Zeige alle Websites mit einem Smush Update",
        _smush_entries(),
        previous_site_ids=(),
        captured_at=datetime.now(UTC),
        selection_is_explicit=False,
        selection_label="",
    )

    assert answer is not None
    assert len(answer.update_matches) == 3
    assert answer.action is None
    assert "3 gemeldete Updates" in answer.text


def test_assistant_queues_only_directly_ready_plugin_updates_for_explicit_all_sites_scope():
    answer = _assistant_service()._answer_supported_update_command(
        "Aktualisiere Smush auf allen Websites",
        _smush_entries(),
        previous_site_ids=(),
        captured_at=datetime.now(UTC),
        selection_is_explicit=False,
        selection_label="",
    )

    assert answer is not None
    assert answer.action is not None
    assert answer.action.selected_keys == (
        "1|plugin|wp-smushit/wp-smush.php",
        "2|plugin|wp-smushit/wp-smush.php",
    )
    assert answer.action.skipped_count == 1


def test_assistant_reuses_the_previous_result_for_this_websites_scope():
    answer = _assistant_service()._answer_supported_update_command(
        "Aktualisiere Smush auf diesen Websites",
        _smush_entries(),
        previous_site_ids=(2,),
        captured_at=datetime.now(UTC),
        selection_is_explicit=False,
        selection_label="",
    )

    assert answer is not None
    assert answer.action is not None
    assert answer.action.selected_keys == ("2|plugin|wp-smushit/wp-smush.php",)


def test_assistant_queues_themes_and_wordpress_core_with_the_selected_panel_scope():
    checked_at = datetime.now(UTC)
    entries = [
        _update_entry(
            site_id=7,
            domain="selected.example",
            kind="theme",
            name="Hello Elementor",
            identifier="hello-elementor",
            checked_at=checked_at,
        ),
        _update_entry(
            site_id=7,
            domain="selected.example",
            kind="wordpress",
            name="WordPress core",
            identifier="wordpress-core",
            checked_at=checked_at,
        ),
    ]

    theme_answer = _assistant_service()._answer_supported_update_command(
        "Aktualisiere alle Themes",
        entries,
        previous_site_ids=(),
        captured_at=checked_at,
        selection_is_explicit=True,
        selection_label="den im Seitenpanel ausgewaehlten Websites",
    )
    core_answer = _assistant_service()._answer_supported_update_command(
        "Aktualisiere WordPress Core",
        entries,
        previous_site_ids=(),
        captured_at=checked_at,
        selection_is_explicit=True,
        selection_label="den im Seitenpanel ausgewaehlten Websites",
    )

    assert theme_answer is not None and theme_answer.action is not None
    assert theme_answer.action.selected_keys == ("7|theme|hello-elementor",)
    assert core_answer is not None and core_answer.action is not None
    assert core_answer.action.selected_keys == ("7|wordpress|wordpress-core",)


def test_assistant_filters_selected_updates_by_customer_status():
    checked_at = datetime.now(UTC)
    entries = [
        _update_entry(
            site_id=7,
            domain="current.example",
            customer_name="Auto Alpha GmbH",
            customer_status="Aktuell",
            checked_at=checked_at,
        ),
        _update_entry(
            site_id=8,
            domain="former.example",
            customer_name="Bau Beta GmbH",
            customer_status="gekündigt",
            checked_at=checked_at,
        ),
    ]

    answer = _assistant_service()._answer_supported_update_command(
        "Aktualisiere alle Plugins mit Status Aktuell",
        entries,
        previous_site_ids=(),
        captured_at=checked_at,
        selection_is_explicit=True,
        selection_label="allen Websites im Seitenpanel",
    )

    assert answer is not None and answer.action is not None
    assert answer.action.selected_keys == ("7|plugin|wp-smushit/wp-smush.php",)


def test_assistant_filters_updates_by_customer_initial_in_addition_to_status():
    checked_at = datetime.now(UTC)
    entries = [
        _update_entry(
            site_id=7,
            domain="alpha.example",
            customer_name="Auto Alpha GmbH",
            customer_status="Aktuell",
            checked_at=checked_at,
        ),
        _update_entry(
            site_id=8,
            domain="beta.example",
            customer_name="Bau Beta GmbH",
            customer_status="Aktuell",
            checked_at=checked_at,
        ),
    ]

    answer = _assistant_service()._answer_supported_update_command(
        "Aktualisiere alle Plugins fuer aktuelle Kunden, deren Namen mit Buchstabe A anfangen",
        entries,
        previous_site_ids=(),
        captured_at=checked_at,
        selection_is_explicit=True,
        selection_label="allen Websites im Seitenpanel",
    )

    assert answer is not None and answer.action is not None
    assert answer.action.selected_keys == ("7|plugin|wp-smushit/wp-smush.php",)


def test_assistant_selects_sites_without_a_named_plugin_update():
    checked_at = datetime.now(UTC)
    answer = _assistant_service()._answer_site_selection_command(
        "Wähle alle Websites ohne Smush Update",
        [
            _update_entry(site_id=7, domain="up-to-date.example", checked_at=checked_at, update_available=False),
            _update_entry(site_id=8, domain="needs-update.example", checked_at=checked_at),
            _update_entry(
                site_id=9,
                domain="not-checked.example",
                checked_at=checked_at,
                update_available=False,
                update_checked=False,
            ),
        ],
        captured_at=checked_at,
    )

    assert answer.selection_site_ids == (7,)
    assert "1 Website ausgewaehlt" in answer.text
    assert "nicht beruecksichtigt" in answer.text


def _assistant_service() -> HubAssistantService:
    return object.__new__(HubAssistantService)


def _smush_entries() -> list[UpdateWorkbenchEntry]:
    checked_at = datetime.now(UTC)
    return [
        _update_entry(site_id=1, domain="one.example", direct_ready=True, checked_at=checked_at),
        _update_entry(site_id=2, domain="two.example", direct_ready=True, checked_at=checked_at),
        _update_entry(site_id=3, domain="waiting.example", direct_ready=False, checked_at=checked_at),
    ]


def _update_entry(
    *,
    site_id: int,
    domain: str,
    checked_at: datetime,
    direct_ready: bool = True,
    kind: str = "plugin",
    name: str = "Smush",
    identifier: str = "wp-smushit/wp-smush.php",
    customer_name: str = "",
    customer_status: str = "",
    update_available: bool = True,
    update_checked: bool = True,
) -> UpdateWorkbenchEntry:
    site = SimpleNamespace(
        id=site_id,
        domain=domain,
        customer=SimpleNamespace(name=customer_name, zoho_status=customer_status) if customer_name or customer_status else None,
    )
    return UpdateWorkbenchEntry(
        site=site,
        kind=kind,
        name=name,
        identifier=identifier,
        current_version="4.3.0",
        target_version="4.3.2" if update_available else "",
        is_active=True if kind == "plugin" else None,
        update_available=update_available,
        update_checked=update_checked,
        execution_ready=direct_ready,
        execution_note="Provider package is unavailable." if not direct_ready else "",
        captured_at=checked_at,
    )
