"""Every card-editable config path says whether a change to it can widen.

A path a card can write but no class names would read ``normal`` on its card,
so a setting that widens what an agent may do would ask for no acknowledgement.
"""

from __future__ import annotations

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

_CLASSES = {
    "_SETTING_ORDER": set(catalog._SETTING_ORDER),
    "_CODE_EXEC_SETTINGS": set(catalog._CODE_EXEC_SETTINGS),
    "_CANNOT_WIDEN_SETTINGS": set(catalog._CANNOT_WIDEN_SETTINGS),
}


def test_every_editable_path_is_classified():
    classified = set().union(*_CLASSES.values())
    missing = sorted(set(_EDITABLE_CONFIG) - classified)
    assert not missing, (
        "classify each new editable config path in change_card_catalog: "
        "_SETTING_ORDER (with its tightest-to-widest order), _CODE_EXEC_SETTINGS, "
        f"or _CANNOT_WIDEN_SETTINGS: {missing}"
    )


def test_each_path_has_exactly_one_class():
    names = list(_CLASSES)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert not (_CLASSES[a] & _CLASSES[b]), (a, b, sorted(_CLASSES[a] & _CLASSES[b]))


def test_the_cannot_widen_list_names_only_editable_paths():
    assert not (_CLASSES["_CANNOT_WIDEN_SETTINGS"] - set(_EDITABLE_CONFIG))


def test_ordered_values_are_values_the_route_accepts():
    for path, order in catalog._SETTING_ORDER.items():
        spec = _EDITABLE_CONFIG.get(path)
        if spec is None:
            continue
        if spec.get("type") == "bool":
            assert set(order) == {True, False}, path
        elif "values" in spec:
            assert set(order) <= set(spec["values"]), path


@pytest.mark.parametrize(
    "path,before,after,risk",
    [
        ("dashboard.tailscale.pin_scope", "node", "login", "widen"),
        ("dashboard.tailscale.pin_scope", "login", "node", "tighten"),
        ("knowledge.auto_add_documents", False, True, "widen"),
        ("knowledge.auto_add_documents", True, False, "tighten"),
        ("agent.acp_backend", "", "claude", "code_exec"),
        ("telemetry.enabled", False, True, "normal"),
    ],
)
def test_newly_classified_paths_read_their_class_on_the_card(path, before, after, risk):
    p = {"path": path, "value": after}
    assert catalog.build_preview("setting.change", p, {"value": before}, {})["risk"] == risk
