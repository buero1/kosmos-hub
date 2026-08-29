import pytest

from app.services.fresh_update_workbench import FreshUpdateWorkbenchService


def test_temporary_fresh_update_path_rejects_large_selection_before_network_calls():
    service = FreshUpdateWorkbenchService(db=None, cipher=None)

    with pytest.raises(ValueError, match="up to 12 sites"):
        service.refresh_selected_sites(set(range(1, 14)))
